"""Named SSE events. Model text is always opaque JSON string data."""

import json

from fastapi.responses import StreamingResponse
from fastapi.sse import format_sse_event


def frame(event: str, data) -> str:
    return format_sse_event(
        event=event, data_str=json.dumps(data, ensure_ascii=False),
    ).decode("utf-8")


def control_event(value: str) -> tuple[str, object]:
    """Translate only trusted producer controls, never model message content."""
    prefix, sep, payload = value.partition(":")
    names = {"__PROCESS__": "progress", "__CONFIRM__": "confirmation",
             "__PENDING__": "pending", "__CMD__": "command", "__EXEC__": "receipts"}
    if not sep or prefix not in names:
        raise ValueError("unsupported producer control event")
    return names[prefix], payload if prefix == "__PROCESS__" else json.loads(payload)


def response(source, *, typed: bool = True) -> StreamingResponse:
    return StreamingResponse(
        source if typed else legacy_stream(source), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no", "X-Agent-Stream-Version": "2" if typed else "1"},
    )


async def legacy_stream(source):
    """Temporary v1 endpoint adapter during the host's v2 deployment."""
    prefixes = {"progress": "__PROCESS__:", "confirmation": "__CONFIRM__:",
                "pending": "__PENDING__:", "command": "__CMD__:",
                "receipts": "__EXEC__:", "summary": "__SUMMARY__:",
                "error": "__ERROR__:"}
    try:
        async for item in source:
            name, encoded = item.split("\n", 1)
            kind = name.removeprefix("event: ")
            payload = encoded.removeprefix("data: ").strip()
            if kind == "done":
                yield "data: __END__\n\n"
            elif kind in ("progress", "confirmation"):
                value = json.loads(payload) if kind == "progress" else payload
                yield "data: " + json.dumps(prefixes[kind] + value, ensure_ascii=False) + "\n\n"
            elif kind == "text":
                yield "data: " + payload + "\n\n"
            else:
                yield "data: " + prefixes[kind] + payload + "\n\n"
    finally:
        await source.aclose()


async def text_stream(text: str):
    yield frame("text", text)
    yield frame("done", {})
