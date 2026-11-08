"""Native PostgreSQL checkpoints enforce owner isolation across independent workers."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier

import pytest
from _memory_support import ACTOR, Capture, config, runtime
from langchain_core.messages import HumanMessage

from agent import checkpoint
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.checkpoint_store import ConversationDeleted
from agent.runtime.conversation_store import ConversationAccessDenied

pytestmark = pytest.mark.enable_socket
FOREIGN = Principal(8, "user", SOURCE_ASSERTION)


def test_owner_is_retained_across_workers_and_foreign_lifecycle_changes(checkpoint_database):
    with ExitStack() as resources:
        first = checkpoint.build_checkpointer(checkpoint_database, resources=resources)
        second = checkpoint.build_checkpointer(checkpoint_database, resources=resources)
        owner, other = runtime(first, Capture()), runtime(second, Capture())
        list(owner.stream_events({"task_id": "kept", "messages": [
            HumanMessage(content="PRIVATE_CANARY")]}, config()))
        before = first.get_tuple(config())
        for action in (
            lambda: other.store.load(config(FOREIGN)),
            lambda: other.update_state(config(FOREIGN), {"run": None}),
            lambda: list(other.stream_events({"resume_task": "kept"}, config(FOREIGN))),
            lambda: other.store.clear_pending(config(FOREIGN), "card"),
            lambda: checkpoint.drop_last_user_turn(other, 42, "kept", principal=FOREIGN),
            lambda: checkpoint.delete_thread(second, 42, principal=FOREIGN),
        ):
            with pytest.raises(ConversationAccessDenied):
                action()
        after = second.get_tuple(config())
        assert after.metadata == before.metadata
        assert after.checkpoint == before.checkpoint
        assert after.pending_writes == before.pending_writes
        changed = config(Principal(7, "admin", SOURCE_ASSERTION))
        with other.conversation_guard(changed):
            other.update_state(changed, {"last_outcome": "responded"})
        assert other.get_state(changed).metadata["user_id"] == ACTOR.uid
        assert checkpoint.delete_thread(second, 42, principal=ACTOR)
        assert first.get_tuple(config()) is None
        with pytest.raises(ConversationDeleted):
            owner.update_state(config(), {"run": None})


def test_concurrent_first_claim_uses_existing_postgres_conversation_lock(checkpoint_database):
    with ExitStack() as resources:
        savers = [checkpoint.build_checkpointer(checkpoint_database, resources=resources)
                  for _ in range(2)]
        barrier = Barrier(2)

        def write(pair):
            saver, principal = pair
            barrier.wait(timeout=5)
            try:
                CheckpointConversationStore(saver).update_state(config(principal), {
                    "messages": [HumanMessage(content=f"owner:{principal.uid}")]})
                return principal.uid
            except ConversationAccessDenied:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(write, zip(savers, [ACTOR, FOREIGN], strict=True)))
        winner, = [uid for uid in outcomes if uid is not None]
        cfg = config(Principal(winner, "user", SOURCE_ASSERTION))
        for saver in savers:
            snapshot = CheckpointConversationStore(saver).get_state(cfg)
            assert snapshot.metadata["user_id"] == winner
            assert [message.content for message in snapshot.values["messages"]] == [
                f"owner:{winner}"]
