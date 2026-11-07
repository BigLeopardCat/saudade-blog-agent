"""Approval continuation must request a new answer, never extend an old assistant answer."""

import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from test_confirmation_continuation import FAVORITE, LOAD, NOTICE, Model, call, prepare, run
from test_confirmation_continuation import backend as backend_fixture
from test_confirmation_continuation import runtime as selected_runtime

from agent import confirm
from agent.runtime.results import ReplyReset
from agent.runtime.session import ToolSession

backend = backend_fixture


def assert_tool_pairs(messages):
    awaiting = []
    for message in messages:
        if isinstance(message, ToolMessage):
            assert awaiting and message.tool_call_id == awaiting.pop(0)
        else:
            assert not awaiting, "assistant narration split an incomplete tool exchange"
            if isinstance(message, AIMessage):
                awaiting = [item["id"] for item in message.tool_calls]
    assert not awaiting


def test_three_approvals_preserve_each_explanation_and_end_with_fresh_execution_evidence(backend):
    state, _ = backend
    selected = selected_runtime(Model())
    second = call("prepare_favorite_change", "favorite-20", action="add", article_id=20)
    _, grant, original = prepare(selected, FAVORITE, second, NOTICE)
    previous_reply = original.review_prompts[-1][-1]
    assert isinstance(previous_reply, ToolMessage)
    for index in range(3):
        model = Model(AIMessage(content="三项均已完成。"))
        model.approval_reply = AIMessage(content=f"第{index + 2}项正在等待确认。")
        selected.model_factory = lambda **_: model
        result = run(selected, grant=grant)
        assert len(state["writes"]) == index + 1
        prompt = model.review_prompts[-1] if index < 2 else model.prompts[-1]
        assert_tool_pairs(prompt)
        assert isinstance(prompt[-1], ToolMessage), "must not end in assistant prefill"
        assert json.loads(prompt[-1].content)["approval"] == "user_confirmed"
        assert json.loads(prompt[-1].content)["status"] == "executed"
        original_call = next(message for message in prompt if isinstance(message, AIMessage)
                             and any(c["id"] == FAVORITE["id"] for c in message.tool_calls))
        assert original_call.tool_calls == [FAVORITE, second, NOTICE]
        if index < 2:
            assert result.execution["pending_confirm"]
            grant = confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)
    assert result.reply == "三项均已完成。"
    assert state["favorites"] == {19, 20} and state["read"]


def test_empty_approval_retry_uses_same_tool_tail_without_adding_empty_assistant(backend):
    class Replies(Model):
        @property
        def approval_reply(self):
            return next(self.replies)

    model = Replies(LOAD, AIMessage(content="", tool_calls=[FAVORITE]))
    model.replies = iter([AIMessage(content=""), AIMessage(content="等待你确认收藏。")])
    selected = selected_runtime(model)
    result = run(selected, "准备收藏文章19")
    assert result.reply == "等待你确认收藏。"
    first, retried = model.review_prompts
    assert first == retried
    assert isinstance(retried[-1], ToolMessage)
    assert not backend[0]["writes"]


def test_separate_provider_reasoning_never_enters_visible_chunks_or_final_reply():
    class Channels(Model):
        def stream(self, *_args, **_kwargs):
            yield AIMessageChunk(content="", additional_kwargs={
                "reasoning_content": "private-reasoning-channel-canary"})
            yield AIMessageChunk(content="这是最终答复。")

    session = ToolSession(Channels(), {}, {}, {})
    runner = session.stream([HumanMessage(content="你好")])
    visible = []
    while True:
        try:
            chunk = next(runner)
        except StopIteration as done:
            result = done.value
            break
        if not isinstance(chunk, ReplyReset):
            visible.append(chunk.content)
    assert "".join(visible) == result.reply == "这是最终答复。"
    assert "private-reasoning-channel-canary" not in str(visible)


@pytest.mark.parametrize("text", [
    '    <function_call>{"name":"example","arguments":{}}</function_call>\n',
    '请核对确认卡。`<function_call>example</function_call>` 是文档示例。\n',
])
def test_approval_narration_preserves_markdown_and_does_not_repeat_model_call(backend, text):
    model = Model(LOAD, AIMessage(content="", tool_calls=[FAVORITE]))
    model.approval_reply = AIMessage(content=text)
    selected = selected_runtime(model)
    result = run(selected, "准备收藏文章19")
    assert result.reply == text
    assert result.execution["pending_confirm"]
    assert len(model.review_prompts) == 1 and not backend[0]["writes"]
