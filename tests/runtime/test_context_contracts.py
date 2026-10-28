"""The multi-turn evaluation rejects plausible replies without execution proof."""

import pytest
from langchain_core.messages import AIMessage

from eval import native_contracts


@pytest.mark.parametrize("reply,passed", [("你好呀～", True), ("主人早上好～", False),
                                         ("杂鱼早上好～", False)])
def test_default_user_address_is_part_of_chat_acceptance(monkeypatch, tmp_path, reply, passed):
    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt, **kwargs):
            return AIMessage(content=reply)

    monkeypatch.setattr("agent.runtime.native.get_llm", lambda **_: Model())
    monkeypatch.setattr(native_contracts.golden_trace, "trace_root",
                        lambda: str(tmp_path / "traces"))
    result = native_contracts.run(["chat"])["cases"][0]
    assert result["evaluated"] and result["passed"] == passed


@pytest.mark.parametrize("failure", [None, "wrong_target", "no_read", "repeat_read"])
def test_short_reply_contract_verifies_target_and_reuse(monkeypatch, tmp_path, failure):
    def call(name, **args):
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])

    responses = [call("load_messages_info_tools"), call("list_my_messages"),
                 AIMessage(content="第一封是《欢迎来信》，要读正文吗？")]
    if failure != "no_read":
        responses.append(call("list_my_messages", message_id=202 if failure == "wrong_target"
                              else 201))
    responses.append(AIMessage(content="取件码是482619。"))
    if failure == "repeat_read":
        responses.append(call("list_my_messages", message_id=201))
    responses.append(AIMessage(content="取件码是482619。"))
    remaining = iter(responses)

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt, **kwargs):
            return next(remaining)

    monkeypatch.setattr("agent.runtime.native.get_llm", lambda **_: Model())
    monkeypatch.setattr(native_contracts.golden_trace, "trace_root",
                        lambda: str(tmp_path / "traces"))
    result = native_contracts.run(["mailbox_short_followup"])["cases"][0]
    assert result["evaluated"] and result["business_writes"] == 0
    assert result["passed"] == (failure is None), result
    if failure is None:
        assert [row["model_calls"] for row in result["rounds"]] == [3, 2, 1]
