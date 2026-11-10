"""Reuse verified gallery links for presentation, never for mutation validation."""

from urllib.parse import quote

from langchain_core.messages import ToolMessage

from agent.authz import TOOL_SCOPE, WRITE_SCOPES


def markdown(image_id, url, name=None):
    name = name or f"图库图片 #{image_id}"
    name = "".join("\\" + char if char in "\\[]" else char
                   for char in name.replace("\n", " ").replace("\r", " "))
    return f"![{name}]({quote(url, safe='/:%?=&')})"


def snapshot(messages, image_id):
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            continue
        if TOOL_SCOPE.get(message.name) in WRITE_SCOPES:
            return None
        if message.name not in {"get_gallery_image_metadata", "show_gallery_image",
                                "get_image_generation"}:
            continue
        artifact = message.artifact if isinstance(message.artifact, dict) else {}
        if message.status != "success" or artifact.get("kind") != "ok":
            return None
        value = (artifact.get("data") if message.name == "get_gallery_image_metadata"
                 else (artifact.get("meta") or {}).get("gallery_display"))
        if not isinstance(value, dict) or type(value.get("image_id")) is not int:
            continue
        if value["image_id"] != image_id:
            continue
        url, name = value.get("url"), value.get("name")
        if not isinstance(url, str) or not 1 <= len(url) <= 2048:
            return None
        return {"imageKey": image_id, "imageUrl": url,
                "displayName": name if isinstance(name, str) and len(name) <= 255 else None}
    return None
