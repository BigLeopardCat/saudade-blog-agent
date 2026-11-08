"""Deliver explicitly selected gallery artifacts through standard Agent message hooks."""

from dataclasses import replace
from operator import or_
from typing import Annotated

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.types import Command, Overwrite
from markdown_it import MarkdownIt

from agent.runtime.message_content import text_content


class GalleryDisplayState(AgentState, total=False):
    gallery_displays: Annotated[dict, or_]


class GalleryDisplayMiddleware(AgentMiddleware):
    state_schema = GalleryDisplayState

    def __init__(self, session):
        self.session = session

    def before_agent(self, state, runtime):
        if self.session.recovering or state.get("resume"):
            return None
        return {"gallery_displays": Overwrite({})}

    def wrap_tool_call(self, request, handler):
        result = handler(request)
        if request.tool_call["name"] != "show_gallery_image":
            return result
        messages = result.update.get("messages", []) if isinstance(result, Command) else [result]
        displays = {}
        for message in messages:
            if (not isinstance(message, ToolMessage) or message.status != "success"
                    or message.tool_call_id != request.tool_call["id"]):
                continue
            artifact = message.artifact or {}
            display = (artifact.get("meta") or {}).get("gallery_display")
            if artifact.get("kind") == "ok" and isinstance(display, dict):
                displays[str(display["image_id"])] = display
        if not displays:
            return result
        update = {**(result.update if isinstance(result, Command) else {"messages": messages}),
                  "gallery_displays": displays}
        return (replace(result, update=update) if isinstance(result, Command)
                else Command(update=update))

    def wrap_model_call(self, request, handler):
        result = handler(request)
        response = result.result[-1]
        if response.tool_calls or response.invalid_tool_calls:
            return result
        displays = request.state.get("gallery_displays") or {}
        if not displays:
            return result
        reply = text_content(response.content)
        urls = {child.attrGet("src") for token in MarkdownIt("commonmark").parse(reply)
                for child in token.children or [] if child.type == "image"}
        images = [row["markdown"] for row in displays.values() if row["url"] not in urls]
        if not images:
            return result
        reply = "\n\n".join([reply, *images]).strip()
        return replace(result, result=[*result.result[:-1], response.model_copy(
            update={"content": reply})])
