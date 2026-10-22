"""Exercise real HybridArm preparation, checkpointing and confirmed replay."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import confirm, idem
from agent.agent import create_agent
from agent.graph import graph_input
from agent.runtime import writes
from agent.runtime.hybrid import HybridArm


def confirmed_roundtrip(monkeypatch, tmp_path, config, domain, prepare_name, arguments):
    class Model:
        calls = 0

        def __init__(self):
            self.responses = iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": f"load_{domain}_info_tools", "args": {}, "id": "load"}
                        ],
                    ),
                    AIMessage(
                        content="",
                        tool_calls=[{"name": prepare_name, "args": arguments, "id": "prepare"}],
                    ),
                ]
            )

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, prompt):
            self.calls += 1
            return next(self.responses)

    model = Model()
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **kwargs: model)
    graph = create_agent(checkpointer=InMemorySaver())
    monkeypatch.setattr(graph, "stream", lambda *a, **kw: pytest.fail("legacy planner invoked"))
    ledger = idem.Ledger(str(tmp_path / "native.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    arm = HybridArm(graph)
    prepared = list(arm.stream(graph_input([HumanMessage(content="准备指定操作")]), config))
    execution = next(event[1]["execute"] for event in prepared if "execute" in event[1])
    assert model.calls == 2
    popup = execution["pending_confirm"]
    cfg = config["configurable"]
    signed = confirm.verify(popup["token"], cfg["principal"].uid, cfg["conversation_id"])
    assert arm.get_state(config).values["pending_confirm"]["token"] == popup["token"]
    for _ in range(2):
        events = list(
            arm.stream(graph_input([HumanMessage(content="确认")], confirm_grant=signed), config)
        )
        result = next(event[1]["execute"] for event in events if "execute" in event[1])
        assert len(result["receipts"]) == 1 and not result["blocked"]
        assert result["receipts"][0]["tool"] == signed["specs"][0]["tool"]
    assert model.calls == 2
    assert arm.get_state(config).values["pending_action"] is None
