"""Bounded user-visible sequences compose restoration, approval, pause and replay."""

import pytest
from _own_support import config
from langchain_core.messages import AIMessage
from test_confirmation_continuation import FAVORITE, Model, prepare, run, runtime
from test_confirmation_continuation import backend as backend_fixture

from agent.products.blog.confirmation import cancel, current_card
from agent.runtime.turns import AgentCancelled

backend = backend_fixture


class Paused(Model):
    def stream(self, *args, **kwargs):
        raise AgentCancelled()


@pytest.mark.parametrize("rebuilds", [0, 2])
@pytest.mark.parametrize("ending", ["cancel", "approve", "approve_pause"])
def test_card_recovery_and_late_controls_preserve_one_operation(backend, rebuilds, ending):
    business, _ = backend
    selected = runtime(Model())
    prepared, grant, _ = prepare(selected, FAVORITE)
    task = selected.store.load(config()).run["id"]
    principal = config()["configurable"]["principal"]
    identifier = prepared.execution["pending_action"]["task_id"]

    for _ in range(rebuilds + 1):
        selected = runtime(Model(), selected.saver)
        first = current_card(selected, principal, 42, task)
        second = current_card(selected, principal, 42, task)
        assert first["confirmation"]["id"] == second["confirmation"]["id"] == identifier
        assert first["confirmation"]["token"] == prepared.execution["pending_confirm"]["token"]
        assert not business["writes"]

    if ending == "cancel":
        assert cancel(selected, grant, config())["cancelled"]
        selected = runtime(Model(), selected.saver)
        for _ in range(2):
            assert current_card(selected, principal, 42, task)["confirmation"] is None
            replay = run(selected, grant=grant)
            assert not replay.execution["receipts"]
        assert not business["writes"] and not business["favorites"]
    else:
        selected.model_factory = lambda **_: (
            Paused() if ending == "approve_pause" else Model(AIMessage(content="已收藏文章19。")))
        if ending == "approve_pause":
            with pytest.raises(AgentCancelled):
                run(selected, grant=grant)
            assert business["favorites"] == {19}
            assert current_card(selected, principal, 42, task)["confirmation"] is None
            selected = runtime(Model(AIMessage(content="已收藏文章19。")), selected.saver)
            result = list(selected.stream_events({"resume_task": task}, config()))[-1]
        else:
            result = run(selected, grant=grant)
        assert result.reply == "已收藏文章19。"
        selected = runtime(Model(), selected.saver)
        assert current_card(selected, principal, 42, task)["confirmation"] is None
        for _ in range(2):
            assert run(selected, grant=grant).execution["receipts"]
        assert len(business["writes"]) == 1 and business["favorites"] == {19}
    assert not selected.store.load(config()).unfinished
