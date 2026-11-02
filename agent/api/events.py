"""Named SSE events. Model text is always opaque JSON string data."""

import json

import anyio
from fastapi.responses import StreamingResponse
from fastapi.sse import format_sse_event

from utils.trace import record

SEND_TIMEOUT = 10.0


class _DeadlineResponse(StreamingResponse):
    """Bound ASGI sends and close request ownership even before iteration starts."""

    def __init__(self, source, on_close):
        self._source, self._on_close = source, on_close
        super().__init__(source, media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no", "X-Agent-Stream-Version": "2"})

    async def stream_response(self, send):
        async def bounded_send(message):
            with anyio.fail_after(SEND_TIMEOUT):
                await send(message)

        try:
            await super().stream_response(bounded_send)
        except TimeoutError:
            record("producer", "send_timeout", timeout_s=SEND_TIMEOUT)
            raise
        finally:
            if self._on_close is not None:
                self._on_close()
            with anyio.move_on_after(2, shield=True) as cleanup:
                await self._source.aclose()
            if cleanup.cancel_called:
                record("producer", "stream_cleanup_timeout")


def frame(event: str, data) -> str:
    return format_sse_event(
        event=event, data_str=json.dumps(data, ensure_ascii=False),
    ).decode("utf-8")


def control_event(value: str) -> tuple[str, object]:
    """Translate only trusted producer controls, never model message content."""
    prefix, sep, payload = value.partition(":")
    names = {"__PROCESS__": "progress", "__CONFIRM__": "confirmation",
             "__PENDING__": "pending", "__CMD__": "command", "__EXEC__": "receipts",
             "__INPUT__": "task_input", "__TEXT_RESET__": "text_reset"}
    if not sep or prefix not in names:
        raise ValueError("unsupported producer control event")
    return names[prefix], payload if prefix == "__PROCESS__" else json.loads(payload)


def response(source, *, on_close=None) -> StreamingResponse:
    return _DeadlineResponse(source, on_close)


async def text_stream(text: str):
    yield frame("text", text)
    yield frame("done", {})
