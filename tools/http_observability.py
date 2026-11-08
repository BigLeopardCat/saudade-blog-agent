"""Observe JSON request attempts without retaining URLs, bodies or credentials."""

import hashlib
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from utils import tool_context
from utils.trace import record

__all__ = ["ResponseObservation", "request_attempt"]


@dataclass
class ResponseObservation:
    status: int | None = None
    response_bytes: int = 0
    response_complete: bool = False
    failure_code: str | None = None


@contextmanager
def request_attempt(method, url, attempt):
    """One event per channel attempt; decoded bytes count only existing reader work."""
    try:
        endpoint = urlsplit(url)
        host = endpoint.hostname
        endpoint_key = hashlib.sha256(endpoint.path.encode()).hexdigest()
        query_key = hashlib.sha256(endpoint.query.encode()).hexdigest()
    except ValueError:
        host = endpoint_key = query_key = None
    call_id, round_index = tool_context.current()
    observation = ResponseObservation()
    began = time.monotonic()
    error = None
    try:
        yield observation
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        record("tools", "http_attempt", call_id=call_id, round=round_index,
               method=method, host=host, endpoint_key=endpoint_key, query_key=query_key,
               attempt=attempt, status=observation.status, error=observation.failure_code or error,
               duration_ms=round((time.monotonic() - began) * 1000, 3),
               response_bytes=observation.response_bytes,
               response_complete=observation.response_complete)
