"""Validate provider stop reasons and keep control envelopes out of replies."""

import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.runtime.answer_length import count_characters, without_length_clause
from agent.runtime.memory import is_user_input
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
        f"用户明确要求最终答复最多 {limit} 个可见字符。使用紧凑措辞保留用户要求的内容和来源。"
        f"以 {max(1, limit * 2 // 3)} 字为写作目标，留出标题、标点和链接文字的空间。"
        "直接给结论，省略问候、复述问题和结束语。空白和链接目标地址不计。")
    content = system.content
    if isinstance(content, str):
        content = f"{content}\n\n{instruction}"
    else:
        content = [*content, {"type": "text", "text": instruction}]
    return system.model_copy(update={"content": content})


def correction_prompt(messages, anchor, limit):
    """Rewrite an already generated draft without replaying the tool conversation."""
    draft, feedback = messages[-2:]
    if not isinstance(draft, AIMessage):
        return None
    inputs = messages[:-2]
    start = next((i for i, message in enumerate(inputs) if anchor and message.id == anchor), None)
    if start is None:
        start = next((i for i in range(len(inputs) - 1, -1, -1)
                      if is_user_input(inputs[i])), len(inputs))
    original = "\n".join(without_length_clause(text_content(message.content))
                         for message in inputs[start:] if is_user_input(message))
    editing_limit = max(1, limit * 3 // 4)
    system = length_instruction(SystemMessage(content=(
        "你是答复编辑。输入 JSON 的 draft 是待改写数据，按 request 指定的要求压缩它。"
        "保留必须事项、凭据来源和限制，可以删掉修饰、重复及非必要举例。"
        "不要新增事实或执行操作。只输出压缩后的正文。")), editing_limit)
    material = json.dumps({"request": original, "draft": text_content(draft.content),
                           "revision_request": text_content(feedback.content)}, ensure_ascii=False)
    return system, [HumanMessage(content=material)]


def length_feedback(message):
    return isinstance(message, HumanMessage) and (
        message.additional_kwargs.get("runtime_feedback") == "answer_length")


def enforce_length(session, response, previous):
    """Check terminal prose and allow one text-only correction per task."""
    limit = session.state["answer_character_limit"]
    if limit is None or session.outcome is not None or incomplete_response(response):
        return None, None
    tool_attempt = bool(response.tool_calls)
    if tool_attempt and not length_feedback(previous):
        return None, None
    text = text_content(response.content)
    actual = count_characters(text)
    if not tool_attempt and actual <= limit:
        return None, None
    record("native", "answer_length_exceeded", limit=limit, actual=actual,
           correction_tool_call=tool_attempt)
    if not tool_attempt and session.state["response_repairs"] == 0:
        session.state["response_repairs"] += 1
        return HumanMessage(content=(
            "上一份答复超过字数上限。请遵守本次的写作预算，保留必需事实和原文入口。"
            "每项只留最关键的一句，删掉问候、展望和重复说明。"
            "标点和链接文字计入字数，空白和链接目标 URL 不计。"
            "禁止再次调用工具或重做已完成操作。只输出改写后的答复。"
        ), additional_kwargs={"runtime_feedback": "answer_length"}), None
    notice = "未能在要求的字数内完整作答。"
    return None, session.blocked_result(notice if len(notice) <= limit else "…",
                                        "answer_length_exceeded")
