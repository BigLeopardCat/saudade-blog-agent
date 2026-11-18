"""Visible character constraints on complete answers, including graph delivery."""

import json

import pytest
from _framework_fixture import call, model_replies
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.runtime.answer_length import count_characters, requested_limit
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime


@pytest.mark.parametrize("prompt,limit", [
    ("请阅读文章131全文，用３００字以内总结，保留原文入口。", 300),
    ("解释原因。回答不超过 42 字符。", 42),
    ("用30字以内，说明限制。", 30),
    ("阅读文章131，并介绍过去300天的趋势。", None),
    ("查找正文不超过 400 字的文章。", None),
    ("用 300 字左右概括。", None),
])
def test_recognizes_a_written_answer_cap(prompt, limit):
    assert requested_limit(prompt) == limit


def test_visible_markdown_counts_punctuation_and_link_labels_without_url_or_markup():
    assert count_characters("## **中 文！**\n[原文链接](https://example.test/long) `x`") == 8
    assert count_characters("甲|乙\n---|---\n丙|丁\n") == 4


@pytest.mark.parametrize("correction,expected", [
    ("这是完整摘要。", "这是完整摘要。"),
    ("甲" * 12, None),
])
def test_runtime_repairs_once_and_never_streams_the_rejected_answer(correction, expected):
    original = "超过上限的内容" * 4
    with model_replies({"content": original}, {"content": correction}) as (model, requests):
        runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
                               application=BlogApplication(), model_factory=lambda **_: model)
        config = {"configurable": {"thread_id": "conv_42", "user_id": 7, "conversation_id": 42,
                                   "principal": Principal(7, "user", SOURCE_ASSERTION)}}
        events = list(runtime.stream_events({
            "messages": [HumanMessage(content="请用10字以内总结。")]}, config))
    visible = "".join(event.content for event in events if isinstance(event, AIMessageChunk))
    final = events[-1]
    assert len(requests) == 2
    assert "最终答复最多 10 个可见字符" in requests[0]["messages"][0]["content"]
    assert [message["role"] for message in requests[1]["messages"]] == ["system", "user"]
    material = json.loads(requests[1]["messages"][1]["content"])
    assert material["draft"] == original
    assert material["request"] == "请简要总结。"
    assert "最终答复最多 7 个可见字符" in requests[1]["messages"][0]["content"]
    assert "禁止再次调用工具" in material["revision_request"]
    assert original not in visible
    assert visible == final.reply
    if expected is None:
        assert final.execution["blocked"] == [{"reason": "answer_length_exceeded"}]
        assert correction not in visible
    else:
        assert final.reply == expected


def test_length_correction_cannot_dispatch_unexpected_tool_calls():
    unexpected = {"tool_calls": [call("load_admin_reports_info_tools")]}
    with model_replies({"content": "甲" * 12}, unexpected) as (model, requests):
        runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
                               application=BlogApplication(), model_factory=lambda **_: model)
        config = {"configurable": {"thread_id": "conv_42", "user_id": 7, "conversation_id": 42,
                                   "principal": Principal(7, "admin", SOURCE_ASSERTION)}}
        events = list(runtime.stream_events({
            "messages": [HumanMessage(content="请用10字以内回答。")]}, config))
    assert len(requests) == 2 and not requests[1].get("tools")
    assert events[-1].execution["blocked"] == [{"reason": "answer_length_exceeded"}]
