"""Validate provider stop reasons and keep control envelopes out of replies."""

from agent.runtime.message_content import text_content
from agent.runtime.visible_text import has_control_text


def incomplete_response(response):
    """A provider stop or invalid call cannot masquerade as a finished answer."""
    metadata = getattr(response, "response_metadata", {}) or {}
    calls = getattr(response, "tool_calls", None)
    if (getattr(response, "invalid_tool_calls", None)
            or has_control_text(text_content(response.content))
            or (getattr(response, "additional_kwargs", {}) or {}).get("function_call")
            or metadata.get("finish_reason") in {"tool_calls", "function_call"} and not calls):
        return "invalid_tool_call", "本轮工具请求未完整，未执行操作。"
    if metadata.get("finish_reason") == "content_filter":
        return "answer_blocked", "这次回复未能完整生成，没有继续执行操作。"
    if not calls and metadata.get("finish_reason") == "length":
        return "answer_truncated", "答复达到长度上限，尚未完整生成。请缩小问题范围后再试。"
    return None
