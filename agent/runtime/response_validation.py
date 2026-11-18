"""Validate provider stop reasons and keep control envelopes out of replies."""

from langchain_core.messages import HumanMessage

from agent.runtime.message_content import text_content
from agent.runtime.visible_text import has_control_text
from utils.trace import record


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


def repair(session, response):
    """Correct one unexecuted protocol failure, durably bounded per task."""
    issue = incomplete_response(response)
    malformed = issue and issue[0] == "invalid_tool_call"
    empty = not issue and not response.tool_calls and not text_content(response.content).strip()
    if not (malformed or empty) or session.state["response_repairs"] >= 1:
        return None
    session.state["response_repairs"] += 1
    record("native", "response_repair", reason="invalid_tool_call" if malformed else "empty",
           attempt=session.state["response_repairs"])
    # Do not replay malformed provider control text or transform it into actions.
    cleaned = response.model_copy(update={"content": "Previous model response was invalid; "
        "no calls from that response were executed.", "tool_calls": [],
        "invalid_tool_calls": [], "additional_kwargs": {}, "response_metadata": {}})
    feedback = HumanMessage(content="Runtime protocol feedback: the preceding model output was "
        "empty or had malformed tool-call data. Continue the original user task. Use native "
        "tool calls with complete JSON arguments when needed, or provide the requested answer. "
        "Previously recorded receipts remain authoritative; do not replay completed writes.",
        additional_kwargs={"runtime_feedback": "response_repair"})
    return [cleaned, feedback]


def withdraw(session):
    """Invalidate provisional prose across storage and live consumers."""
    session.reply_parts.clear()
    session.state["output_revision"] += 1
    record("native", "reply_withdrawn", revision=session.state["output_revision"])


def length_instruction(system, limit):
    if system is None or limit is None:
        return system
    instruction = (
        f"用户希望答复控制在 {limit} 个可见字符内。"
        f"先以约 {max(1, limit * 2 // 3)} 字起草，给标点和链接文字留出空间。"
        "直接作答，删掉问候、复述和重复说明。每个必答主题只写最关键的短句，"
        "用简短的 Markdown 链接文字保留所需来源。空间不足时交付最短的完整答复。"
        "空白和链接目标地址不计。")
    content = system.content
    if isinstance(content, str):
        content = f"{content}\n\n{instruction}"
    else:
        content = [*content, {"type": "text", "text": instruction}]
    return system.model_copy(update={"content": content})
