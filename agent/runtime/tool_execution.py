"""Apply application result policy around the framework's tool handler."""

import json

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent.runtime import memory, tool_evidence
from agent.runtime.budget import native_tool
from agent.runtime.memory_media import tool_messages
from agent.runtime.message_content import text_content
from agent.runtime.progress import emit
from agent.runtime.results import TurnResult
from agent.runtime.tool_outputs import application_result, framework_tool
from utils import trace as trace_mod
from utils.trace import record

MAX_RESULT_CHARS = 8000


def render_result(call, value, limit):
    body = text_content(value.content) if isinstance(value, ToolMessage) else str(value)
    if len(body) > limit:
        marker = "\n[结果截断，请缩小检索范围]"
        body = body[:max(0, limit - len(marker))] + marker
    return body


def append_result(session, request, handler, prompt, round_index, attachments):
    call = request.tool_call
    name = call["name"]
    record("native", "tool_requested", name=name, call_id=call["id"])
    if name in session.loaders:
        domain = session.loaders[name]
        record("native", "tool_discovery", domain=domain, round=round_index,
               already_loaded=domain in session.state["loaded"])
        if domain not in session.state["loaded"]:
            session.tools.update(session.domains[domain])
            session.state["loaded"].append(domain)
            emit(session.config, f"正在加载工具领域：{domain}", key=f"domain:{domain}")
            record("native", "load_domain", domain=domain, call_id=call["id"])
            session.refresh_menu()
        prompt.append(ToolMessage(content=f"{domain} 工具已加载。",
                                  name=name, tool_call_id=call["id"]))
        return None
    if name not in session.tools:
        required = session.tool_owners.get(name)
        reason = "tool_not_loaded" if required else "unknown_tool"
        body = json.dumps({"error": reason, "tool": name,
                           "discovery_tools": required or list(session.loaders)})
        prompt.append(ToolMessage(content=body, name=name,
                                  tool_call_id=call["id"], status="error"))
        record("native", "tool_error", name=name, reason=reason, round=round_index)
        return None
    session.calls_used += 1
    emit(session.config, f"正在调用工具：{name}", key=f"tool:{call['id']}")
    with native_tool(session.budget), tool_evidence.scope(call, round_index):
        output = handler(request.override(tool=framework_tool(session.tools[name])))
    command = output if isinstance(output, Command) else None
    messages = output.update["messages"] if command else [output]
    message = next((m for m in messages if isinstance(m, ToolMessage)
                    and m.tool_call_id == call["id"]), None)
    if message is None:
        raise ValueError("Tool response does not match the framework call ID")
    attachments.extend(m for m in messages if m is not message)
    body = application_result(message)
    if isinstance(body, TurnResult):
        session._prepared_result(body, call, prompt, round_index)
        return command
    artifact = message.artifact if isinstance(message.artifact, dict) else {}
    kind = (getattr(body, "kind", artifact.get("kind", "unknown"))
            if message.status != "error" else "error")
    session.observations.extend(memory.observation(call, body))
    body = session.render_result(call, body, MAX_RESULT_CHARS)
    # Structured data is used by product evidence policy, not duplicated in
    # the durable transcript. Preserve artifacts supplied by standard tools.
    message = message.model_copy(update={"artifact": {
        key: value for key, value in artifact.items() if key != "application_data"}})
    messages = tool_messages(message, body, call["id"])
    prompt.append(messages[0].model_copy(update={"name": name, "artifact": {
        **(messages[0].artifact or {}), "kind": kind}}))
    attachments.extend(messages[1:])
    record("native", "call", name=name, args=call["args"], call_id=call["id"], round=round_index,
           kind=kind, result=trace_mod.tool_result_text(body, name, default_limit=MAX_RESULT_CHARS))
    record("native", "tool_result", name=name, call_id=call["id"], result_chars=len(body))
    return command
