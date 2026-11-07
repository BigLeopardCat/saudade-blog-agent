"""Adapt HTTP chat input to standard messages without embedding pseudo system instructions."""

from langchain_core.messages import AIMessage, HumanMessage

from agent.api.host_context import request_facts
from agent.api.schemas import ChatRequest
from agent.runtime.memory import turn_message_id


def _host_history(req: ChatRequest) -> list:
    scope = req.history_scope or {}
    if (type(req.conversation_id) is not int or req.conversation_id <= 0
            or type(scope.get("conversation_id")) is not int
            or scope["conversation_id"] != req.conversation_id):
        return []
    history = []
    for item in req.history[-20:]:
        if item.content or item.role == "user":
            history.append((item.role, item.content, item.turn_id))
    messages: list = []
    for index, (role, content, task_id) in enumerate(history):
        if role == "user":
            if index + 1 == len(history) or history[index + 1][0] != "assistant":
                continue
            messages.append(HumanMessage(content=content, id=turn_message_id(task_id)))
        else:
            messages.append(AIMessage(content=content))
    actor = {"uid": scope.get("user_id"), "role": scope.get("role")}
    return [message.model_copy(update={"additional_kwargs": {"memory_actor": actor}})
            for message in messages]


def build_messages(req: ChatRequest, *, holds_context: bool = False) -> list:
    """Carry bootstrap candidates as data; the runtime chooses them under its turn lock."""
    context = HumanMessage(content="", additional_kwargs={
        "memory_context": True,
        "page_context": {"url": req.current_url, "title": req.page_title,
                         "current_effects": req.current_effects,
                         "current_darkmode": req.current_darkmode},
        "conversation_summary": req.summary,
        "summary_scope": req.summary_scope,
        "conversation_id": req.conversation_id,
        "host_history": _host_history(req) if holds_context else [],
        "request_context": request_facts(req),
    })
    messages = [context]
    if not holds_context:
        messages.extend(_host_history(req))
    if req.image:
        images = [req.image] if isinstance(req.image, str) else req.image
        content: list = [{"type": "text", "text":
                          f"[当前问题]: {req.message or '请描述这些图片'}"},
                         *({"type": "image_url", "image_url": {"url": url}} for url in images)]
        messages.append(HumanMessage(content=content))
    else:
        messages.append(HumanMessage(content=f"[当前问题]: {req.message}"))
    return messages
