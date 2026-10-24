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


def response(source) -> StreamingResponse:
    return StreamingResponse(
        source, media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no", "X-Agent-Stream-Version": "2"},
    )


async def text_stream(text: str):
    yield frame("text", text)
    yield frame("done", {})
