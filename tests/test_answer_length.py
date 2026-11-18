"""Visible character targets and complete graph delivery."""

import pytest
from _framework_fixture import model_replies
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


@pytest.mark.parametrize("reply", [
    "完整。",
    "部署、配置、产物、限制。见[原文](https://example.test/article/131)。",
])
def test_runtime_delivers_the_complete_reply_without_a_length_retry(reply):
    with model_replies({"content": reply}) as (model, requests):
        runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
                               application=BlogApplication(), model_factory=lambda **_: model)
        config = {"configurable": {"thread_id": "conv_42", "user_id": 7, "conversation_id": 42,
                                   "principal": Principal(7, "user", SOURCE_ASSERTION)}}
        events = list(runtime.stream_events({
            "messages": [HumanMessage(content="请用10字以内总结。")]}, config))
    visible = "".join(event.content for event in events if isinstance(event, AIMessageChunk))
    final = events[-1]
    assert len(requests) == 1
    assert "用户希望答复控制在 10 个可见字符内" in requests[0]["messages"][0]["content"]
    assert visible == final.reply == reply
    assert not (final.execution or {}).get("blocked")
