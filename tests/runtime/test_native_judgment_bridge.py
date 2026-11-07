"""The shared judgment consumes actual saved events, not a synthetic success flag."""

from langchain_core.messages import AIMessage

from eval import golden_evaluation, native_contracts
from eval.own_fixtures import favorite_backend


def test_native_turn_supplies_saved_events_to_the_shared_judgment(monkeypatch, tmp_path):
    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, *args, **kwargs):
            return AIMessage(content="收到")

    monkeypatch.setattr("agent.runtime.service.get_llm", lambda **_: Model())
    monkeypatch.setattr(native_contracts.golden_trace, "trace_root", lambda: str(tmp_path))

    def contract(turn, state):
        value = turn("只回复收到")
        assert any(event.get("event") == "llm_done" for event in value["native_events"])
        gold = {"forbid_runtime_stop": True, "forbid_business_write": True}
        assert golden_evaluation.check_gold(gold, value) == []
        missing = golden_evaluation.check_gold(gold, {**value, "native_events": None})
        assert missing and "未评估" in missing[0]

    row, _ = native_contracts._sample("shared_judgment", favorite_backend, contract, "bridge", 1)
    assert row["passed"] and row["evaluated"]
