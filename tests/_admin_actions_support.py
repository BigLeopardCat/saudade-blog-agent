"""Exercise real NativeRuntime preparation, checkpointing and confirmed replay."""

import json

from _framework_ledger import Ledger
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import confirm
from agent.runtime import writes
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore


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
                    AIMessage(content="操作完成。"),
                ]
            )

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, prompt):
            self.calls += 1
            if self.calls == 3:
                facts = next(m for m in reversed(prompt)
                             if isinstance(m, ToolMessage) and m.tool_call_id == "prepare")
                assert json.loads(facts.content)["status"] == "executed"
                assert runtime.store.load(config).approval is None
            return next(self.responses)

    model = Model()
    ledger = Ledger(str(tmp_path / "native.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()),
                            model_factory=lambda **_: model)
    prepared = list(runtime.stream_events(
        {"messages": [HumanMessage(content="准备指定操作")]}, config))
    execution = prepared[-1].execution
    assert model.calls == 2
    popup = execution["pending_confirm"]
    cfg = config["configurable"]
    signed = confirm.verify(popup["token"], cfg["principal"].uid, cfg["conversation_id"])
    assert runtime.get_state(config).values["pending_confirm"]["token"] == popup["token"]
    for _ in range(2):
        events = list(runtime.stream_events(
            {"messages": [HumanMessage(content="确认")], "confirm_grant": signed}, config))
        result = events[-1].execution
        assert len(result["receipts"]) == 1 and not result["blocked"]
        assert result["receipts"][0]["tool"] == signed["specs"][0]["tool"]
    assert model.calls == 3
    assert runtime.get_state(config).values["pending_action"] is None
