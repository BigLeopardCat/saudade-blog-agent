"""Deliver explicitly selected gallery artifacts through standard Agent message hooks."""

from dataclasses import replace
from operator import or_
from typing import Annotated

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.types import Command, Overwrite
from markdown_it import MarkdownIt

from agent.runtime.message_content import text_content
from utils.trace import record


def _image_urls(text):
    return {child.attrGet("src") for token in MarkdownIt("commonmark").parse(text)
            for child in token.children or [] if child.type == "image"}


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
        displays = request.state.get("gallery_displays") or {}
        if displays:
            # Validate media before it reaches the client, including SDK streaming deltas.
            self.session.suppress_output = True
        result = handler(request)
        response = result.result[-1]
        if response.tool_calls or response.invalid_tool_calls:
            return result
        if not displays:
            return result
        reply = text_content(response.content)
        urls = _image_urls(reply)
        if urls - _image_urls("\n\n".join(row["markdown"] for row in displays.values())):
            record("native", "media_delivery", reason="unverified_image_reference",
                   image_ids=[row["image_id"] for row in displays.values()])
            reply, urls = "", set()
        images = [row["markdown"] for row in displays.values()
                  if not _image_urls(row["markdown"]) <= urls]
        if not images:
            return result
        reply = "\n\n".join([reply, *images]).strip()
        return replace(result, result=[*result.result[:-1], response.model_copy(
            update={"content": reply})])
