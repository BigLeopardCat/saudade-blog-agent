"""HTTP request bounds and trace context propagation."""

import logging
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from config.settings import settings
from utils.logging import request_id, reset_trace_id, set_trace_id

MAX_BODY_BYTES = settings.max_body_bytes
_TRACE_ID_HEADER = "X-Request-ID"
logger = logging.getLogger(__name__)


async def trace_id_middleware(request: Request, call_next):
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
        reset_trace_id()


async def body_limit_middleware(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        return JSONResponse(
            {"error": "payload_too_large", "limit": MAX_BODY_BYTES}, status_code=413
        )
    return await call_next(request)
