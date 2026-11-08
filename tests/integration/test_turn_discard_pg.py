"""Turn fences and checkpoint removal survive worker reconstruction in PostgreSQL."""

from contextlib import ExitStack

import pytest
from _memory_support import ACTOR, Capture, config, runtime
from langchain_core.messages import HumanMessage

from agent import checkpoint

pytestmark = pytest.mark.enable_socket


def run(selected, task):
    return list(selected.stream_events({"task_id": task,
        "messages": [HumanMessage(content="继续")]}, config()))[-1]


def test_worker_reconstruction_keeps_targeted_discard_and_absent_target_fences(checkpoint_database):
    with ExitStack() as first, ExitStack() as second:
        saver = checkpoint.build_checkpointer(checkpoint_database, resources=first)
        left = runtime(saver, Capture())
        run(left, "first")
        preserved = left.store.load(config()).messages
        run(left, "second")
        assert checkpoint.drop_last_user_turn(left, 42, "second", principal=ACTOR)["dropped"]
        assert checkpoint.drop_last_user_turn(left, 42, "late", principal=ACTOR)["dropped"]
        right_model = Capture()
        rebuilt_saver = checkpoint.build_checkpointer(checkpoint_database, resources=second)
        right = runtime(rebuilt_saver, right_model)
        assert checkpoint.drop_last_user_turn(right, 42, "second", principal=ACTOR)["dropped"]
        assert right.store.load(config()).messages == preserved
        assert run(right, "second").execution["blocked"]
        assert run(right, "late").execution["blocked"]
        assert not right_model.prompts
        assert run(right, "third").reply
