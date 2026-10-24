"""HTTP request bounds and trace context propagation."""

import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

from config.settings import settings
from utils.logging import set_trace_id

MAX_BODY_BYTES = settings.max_body_bytes
_TRACE_ID_HEADER = "X-Request-ID"


async def trace_id_middleware(request: Request, call_next):
    trace_id = request.headers.get(_TRACE_ID_HEADER) or uuid.uuid4().hex[:12]
    set_trace_id(trace_id)
    response = await call_next(request)
    response.headers[_TRACE_ID_HEADER] = trace_id
    return response


async def body_limit_middleware(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        return JSONResponse(
            {"error": "payload_too_large", "limit": MAX_BODY_BYTES}, status_code=413
        )
    return await call_next(request)
