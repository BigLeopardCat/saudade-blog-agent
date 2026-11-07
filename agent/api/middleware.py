"""HTTP request bounds and trace context propagation."""

import logging
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from agent import delegation
from config.settings import settings
from utils.logging import request_id, reset_trace_id, set_trace_id

MAX_BODY_BYTES = settings.max_body_bytes
_TRACE_ID_HEADER = "X-Request-ID"
logger = logging.getLogger(__name__)


async def trace_id_middleware(request: Request, call_next):
    credential_context = delegation.clear()
    trace_id = request_id(request.headers.get(_TRACE_ID_HEADER))
    set_trace_id(trace_id)
    started = time.monotonic()
    logger.info("http_request_started method=%s", request.method)
    try:
        response = await call_next(request)
        response.headers[_TRACE_ID_HEADER] = trace_id
        route = getattr(request.scope.get("route"), "path", "<unmatched>")
        logger.info("http_response_started method=%s route=%s status=%s headers_ms=%.2f",
                    request.method, route, response.status_code,
                    (time.monotonic() - started) * 1000)
        return response
    except Exception:
        logger.exception("http_request_failed method=%s headers_ms=%.2f", request.method,
                         (time.monotonic() - started) * 1000)
        raise
    finally:
        delegation.reset(credential_context)
        reset_trace_id()


class BodyLimitMiddleware:
    """Bound actual request bytes before routes can perform I/O; stream responses unchanged."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = MAX_BODY_BYTES
        headers = dict(scope.get("headers", ()))
        declared = headers.get(b"content-length", b"").lstrip(b"0")
        if declared.isdigit() and (len(declared) > len(str(limit)) or int(declared) > limit):
            return await self._reject(scope, receive, send, limit)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                return await self._reject(scope, receive, send, limit)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        buffered = bytes(body)
        pending = True

        async def replay():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": buffered, "more_body": False}
            # Once the body is consumed, preserve real disconnect notifications for SSE.
            return await receive()

        return await self.app(scope, replay, send)

    @staticmethod
    async def _reject(scope, receive, send, limit):
        response = JSONResponse({"error": "payload_too_large", "limit": limit}, status_code=413)
        await response(scope, receive, send)
