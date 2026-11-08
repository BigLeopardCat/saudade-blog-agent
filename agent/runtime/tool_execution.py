"""Apply application result policy around the framework's tool handler."""

import json

from langchain_core.messages import ToolMessage, trim_messages
from langgraph.types import Command

from agent.runtime import memory, tool_evidence, tool_state
from agent.runtime.budget import native_tool
from agent.runtime.memory_media import tool_messages
from agent.runtime.message_content import estimate_tokens, text_content
from agent.runtime.progress import emit
from agent.runtime.tool_outputs import framework_tool, prepared_result
from config.settings import settings
from utils import trace as trace_mod
from utils.trace import record

TRACE_RESULT_CHARS = 8000
_TRUNCATION_MARKER = "\n[结果截断，请缩小检索范围]"


def result_limit(value):
    """Bound one observation; native_call enforces the complete request budget.

    Context exhaustion must not silently discard facts to fund another call.
    Paged product renderers use the returned size to preserve continuation cursors.
    """
    message = value if isinstance(value, ToolMessage) else ToolMessage(
        content=str(value), tool_call_id="result-budget")
    body = text_content(message.content)
    if estimate_tokens([message]) <= settings.agent_tool_result_tokens:
        return len(body)
    marker_tokens = (estimate_tokens([message.model_copy(update={"content": _TRUNCATION_MARKER})])
                     - estimate_tokens([message.model_copy(update={"content": ""})]))
    kept = trim_messages([message.model_copy(update={"content": body})],
        max_tokens=max(0, settings.agent_tool_result_tokens - marker_tokens),
        token_counter=estimate_tokens, strategy="first", allow_partial=True, text_splitter=list)
    return len(kept[0].content) if kept else 0


def render_result(call, value, limit):
    body = text_content(value.content) if isinstance(value, ToolMessage) else str(value)
    if len(body) > limit:
        marker = _TRUNCATION_MARKER
        body = (body[:max(0, limit - len(marker))] + marker)[:limit]
    return value.model_copy(update={"content": body})


def append_result(session, request, handler, prompt, round_index, attachments):
    call = request.tool_call
    name = call["name"]
    record("native", "tool_requested", name=name, call_id=call["id"])
    if name in session.loaders:
        domain = session.loaders[name]
        already = domain in tool_state.loaded(session)
        record("native", "tool_discovery", domain=domain, round=round_index,
               already_loaded=already)
        reason = tool_state.activate(session, domain, prompt)
        if reason:
            prompt.append(ToolMessage(content=json.dumps({"status": "not_loaded",
                "domain": domain, "reason": reason}), status="error",
                name=name, tool_call_id=call["id"]))
        else:
            if not already:
                emit(session.config, f"正在加载工具领域：{domain}", key=f"domain:{domain}")
                record("native", "load_domain", domain=domain, call_id=call["id"])
            prompt.append(ToolMessage(content=f"{domain} 工具已加载。",
                                      name=name, tool_call_id=call["id"]))
        return None
    if name not in session.execution_tools:
        reason = "unknown_tool"
        body = json.dumps({"error": reason, "tool": name,
                           "discovery_tools": list(session.loaders)})
        prompt.append(ToolMessage(content=body, name=name,
                                  tool_call_id=call["id"], status="error"))
        record("native", "tool_error", name=name, call_id=call["id"],
               reason=reason, round=round_index)
        return None
    session.calls_used += 1
    tool_state.use(session, name, prompt)
    emit(session.config, f"正在调用工具：{name}", key=f"tool:{call['id']}")
    with native_tool(session.budget), tool_evidence.scope(call, round_index):
        output = handler(request.override(tool=framework_tool(session.execution_tools[name])))
    command = output if isinstance(output, Command) else None
    messages = output.update["messages"] if command else [output]
    message = next((m for m in messages if isinstance(m, ToolMessage)
                    and m.tool_call_id == call["id"]), None)
    if message is None:
        raise ValueError("Tool response does not match the framework call ID")
    attachments.extend(m for m in messages if m is not message)
    prepared = prepared_result(message)
    if prepared is not None:
        session._prepared_result(prepared, call, prompt, round_index)
        return command
    artifact = message.artifact if isinstance(message.artifact, dict) else {}
    kind = (artifact.get("kind", "ok")
            if message.status != "error" else "error")
    original = message
    message = session.render_result(call, message, result_limit(message))
    if kind in {"invalid_args", "unavailable", "not_found"}:
        message = message.model_copy(update={"status": "error"})
    message = message.model_copy(update={"artifact": {**(message.artifact or {}), "kind": kind}})
    session.observations.extend(memory.observation(call, message))
    body = text_content(message.content)
    source = original.model_copy(update={"artifact": message.artifact, "status": message.status})
    messages = tool_messages(source, body, call["id"])
    prompt.append(messages[0].model_copy(update={"name": name, "artifact": {
        **(messages[0].artifact or {}), "kind": kind}}))
    attachments.extend(messages[1:])
    record("native", "call", name=name, args=call["args"], call_id=call["id"], round=round_index,
           kind=kind, result=trace_mod.tool_result_text(
               body, name, default_limit=TRACE_RESULT_CHARS))
    record("native", "tool_result", name=name, call_id=call["id"], result_chars=len(body))
    return command
