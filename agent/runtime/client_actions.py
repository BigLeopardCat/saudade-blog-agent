"""Expose durable framework interrupts for bounded client actions; never grant authority."""

import hashlib
import json
import time

from langchain_core.tools import ToolException
from langgraph.types import interrupt

from agent.runtime.progress import current_emitter
from agent.runtime.results import TurnResult
from utils.trace import record


def await_result(command, config):
    task_id = config["configurable"]["browser_task"]
    if not isinstance(command.get("request_id"), str) or not command["request_id"]:
        raise ToolException("Client actions require a stable tool invocation ID")
    identity = json.dumps([task_id, command], sort_keys=True, separators=(",", ":"))
    command = {
        **command,
        "task_id": task_id,
        "command_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
        "timeout_ms": 6000,
        "expires_at": time.time() + 60,
    }
    progress = current_emitter(config)
    result = interrupt(
        {
            "kind": "client_action",
            "command": command,
            "progress_keys": progress.emitted_keys if progress is not None else [],
        }
    )
    if result.get("command_id") != command["command_id"]:
        raise ToolException("Browser result does not match the pending action")
    status = result.get("status")
    if status == "completed" and not successful(command, result):
        status = "failed"
    if status not in {"completed", "failed", "timeout"}:
        raise ToolException("Invalid browser outcome")
    return {**result, "status": status}


def paused_result(update):
    for interruption in update.get("__interrupt__", ()):
        value = interruption.value
        if isinstance(value, dict) and value.get("kind") == "client_action":
            return TurnResult("", {"client_action": value["command"]})
    return None


def pending_interrupt(snapshot, kind="client_action"):
    for interruption in snapshot.interrupts:
        value = interruption.value
        if isinstance(value, dict) and value.get("kind") == kind:
            return value
    for task in snapshot.tasks:
        state = task.state
        if hasattr(state, "interrupts"):
            command = pending_interrupt(state, kind)
            if command:
                return command
    return None


def successful(command, value):
    """Client observations prove only the requested local browser state."""
    if value.get("status") != "completed":
        return False
    if command["kind"] == "navigate":
        return value.get("path") == command["path"]
    if command["kind"] == "appearance":
        observed = value.get("settings")
        return isinstance(observed, dict) and all(
            observed.get(key) == mode for key, mode in command["settings"].items()
        )
    if command["kind"] == "article_editor":
        from pydantic import ValidationError

        from agent.contracts.client import EditorObservation

        try:
            document = EditorObservation.model_validate(value.get("document"))
        except ValidationError:
            return False
        return document.matches(command)
    return False


def resume_value(snapshot, task_id, config):
    pending = pending_interrupt(snapshot)
    value = config["configurable"].get("browser_result")
    if pending is None:
        if value is not None:
            return TurnResult("该页面操作已经结束，没有重复执行。", {"blocked": True})
        return None
    command = pending["command"]
    if value is None:
        if time.time() <= command["expires_at"]:
            return TurnResult("", {"client_action": command})
        value = {"command_id": command["command_id"], "status": "timeout",
                 "error": "browser_ack_expired"}
    if (
        command.get("task_id") != task_id
        or value.get("command_id") != command["command_id"]
        or value.get("status") not in {"completed", "failed", "timeout"}
    ):
        return TurnResult("页面回报与当前任务不一致，没有接续。", {"blocked": True})
    status = value["status"]
    if status == "completed" and not successful(command, value):
        status = "failed"
    if time.time() > command["expires_at"]:
        status = "timeout"
    progress = current_emitter(config)
    if progress is not None:
        progress.restore(pending.get("progress_keys", []))
        completed = (
            "页面已到达，正在整理回复…"
            if command["kind"] == "navigate"
            else "编辑器已返回结果，正在整理回复…"
            if command["kind"] == "article_editor"
            else "显示设置已应用，正在整理回复…"
        )
        progress.emit(
            completed if status == "completed" else "页面操作已结束，正在整理结果…",
            key="client_result:" + command["command_id"],
        )
    record("native", "browser_result", status=status, command_id=command["command_id"])
    return {
        "command_id": command["command_id"],
        "status": status,
        "path": value.get("path", ""),
        "settings": value.get("settings"),
        "document": value.get("document"),
        "error": value.get("error"),
    }
