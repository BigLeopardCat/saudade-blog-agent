"""Tool observations use framework token accounting without discarding task facts."""

from langchain_core.messages import ToolMessage

from agent.runtime.message_content import estimate_tokens
from agent.runtime.tool_execution import render_result, result_limit
from config.settings import settings


def test_a_normal_article_chunk_is_not_clipped_at_eight_thousand_characters():
    source = "正文" * 6000
    cap = result_limit(source)
    assert cap == len(source)
    assert render_result({}, source, cap) == source


def test_large_cjk_result_respects_the_configured_token_ceiling():
    source = "内容" * 50000
    cap = result_limit(source)
    result = ToolMessage(content=render_result({}, source, cap), tool_call_id="read")
    assert 0 < cap < len(source)
    assert estimate_tokens([result]) <= settings.agent_tool_result_tokens
    assert "结果截断" in result.content


def test_zero_limit_does_not_add_a_truncation_marker_beyond_the_limit():
    assert render_result({}, "body", 0) == ""


def test_structured_tool_message_uses_its_content_not_python_representation():
    assert result_limit(ToolMessage(content="正文", tool_call_id="private-id")) == len("正文")
