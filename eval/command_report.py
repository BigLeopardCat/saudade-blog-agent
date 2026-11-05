"""Diagnostic command labels; never used to dispatch production actions."""

import json

from agent.transport import TransportProtocolError


def command_wire(command: dict) -> str:
    """Render a structured command for historical evaluation assertions only."""
    kind = command.get("kind") if isinstance(command, dict) else None
    if kind == "navigate" and command.get("url"):
        prefix = "NAVIGATE:" if str(command.get("mode") or "") == "confirm" else "AUTO_NAVIGATE:"
        return prefix + str(command["url"])
    if kind == "effect" and command.get("effect"):
        return f"EFFECT:{command['effect']}:{command.get('action', 'on')}"
    if kind == "darkmode" and command.get("mode"):
        return "DARKMODE:" + str(command["mode"])
    if kind == "appearance" and command.get("settings"):
        return "APPEARANCE:" + json.dumps(command["settings"], separators=(",", ":"))
    if kind == "article_editor" and command.get("operation") in {
            "read", "propose", "summary", "prepare_publish"}:
        return "ARTICLE_EDITOR:" + json.dumps(command, separators=(",", ":"))
    if kind:
        raise TransportProtocolError(f"unsupported receipt command kind: {kind}")
    raise TransportProtocolError("receipt command kind is missing")

