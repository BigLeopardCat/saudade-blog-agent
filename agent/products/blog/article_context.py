"""Reference identical visible source ranges without editing persisted messages."""

import json

from langchain_core.messages import AIMessage, ToolMessage

from rag.sections import PARSER_VERSION
from rag.source import SourceRange
from utils.trace import record


def _source(message):
    if (not isinstance(message, ToolMessage) or message.name != "get_article_detail"
            or message.status == "error" or not message.tool_call_id
            or (message.artifact or {}).get("kind") != "ok"
            or not isinstance(message.content, str)):
        return None
    try:
        data = json.loads(message.content)
        if not isinstance(data, dict) or data.get("view") != "body":
            return None
        source = SourceRange.model_validate(data.get("source"))
        body = data.get("sectionText") or data.get("noteContent") or data.get("content")
        if (source.type != "note" or type(data.get("noteId")) is not int
                or data["noteId"] != source.id
                or data.get("contentRevision") != source.revision
                or data.get("sectionSchema") != source.section_schema
                or source.section_schema != PARSER_VERSION
                or not isinstance(body, str) or not body
                or len(body) != source.end - source.offset):
            return None
    except (TypeError, ValueError):
        return None
    return source, body


def source_references(messages, *, round_index=None):
    """Reference bodies only within a complete native tool exchange, kept as one history group."""
    seen, pending, result = {}, {}, []
    for message in messages:
        if isinstance(message, AIMessage):
            seen = {}
            pending = {call["id"]: call["name"] for call in message.tool_calls}
        elif not isinstance(message, ToolMessage):
            seen, pending = {}, {}
        if (not isinstance(message, ToolMessage)
                or pending.get(message.tool_call_id) != message.name):
            result.append(message)
            continue
        pending.pop(message.tool_call_id)
        observed = _source(message)
        if observed is None:
            result.append(message)
            continue
        source, body = observed
        key = (*source.model_dump().values(), body)
        original = seen.setdefault(key, message.tool_call_id)
        if original == message.tool_call_id:
            result.append(message)
            continue
        text = json.dumps({"view": "source_reference", "source": source.model_dump(),
                           "body_in_tool_call": original},
                          ensure_ascii=False, separators=(",", ":"))
        if len(text) >= len(message.content):
            result.append(message)
            continue
        record("native", "article_context_reused", round=round_index,
               call_id=message.tool_call_id, source_call_id=original,
               source=source.model_dump(), reused_chars=len(body),
               removed_chars=len(message.content) - len(text))
        result.append(message.model_copy(update={"content": text}))
    return result
