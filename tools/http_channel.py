"""Bounded JSON request channels with typed, non-leaking failure outcomes.

Reads retry only explicit transient failures: connect, read and pool timeouts,
network/protocol errors, and HTTP 408/429/502/503/504. Argument rejection,
missing targets, authorization denial, business rejection and undecodable
responses never retry. Writes never retry: once a request may have left this
process, an unconfirmed outcome stays unknown instead of being replayed.

Exception text never reaches model context or logs; only the exception class
is recorded. Backend business messages are control-stripped and length-capped.
Cancellation is not caught (``asyncio.CancelledError`` is a ``BaseException``),
and a caller-supplied stop predicate suppresses further attempts.

A body-based read is selected explicitly by the caller (``json_body=``); the
channel never infers read or write intent from a URL.
"""

import logging
import re
import time
from typing import Callable

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_fixed

from tools.result import ToolResult, invalid_args, not_found, unavailable

__all__ = ["ERROR_FORBIDDEN", "ERROR_INVALID_ARGS", "ERROR_INVALID_RESPONSE",
           "ERROR_NOT_FOUND", "ERROR_UPSTREAM_REJECTED", "ERROR_UPSTREAM_UNAVAILABLE",
           "ERROR_WRITE_UNKNOWN", "Failure", "MESSAGE_MAX", "READ_ATTEMPTS",
           "READ_TIMEOUT", "RETRY_WAIT_SECONDS", "logger", "read_failure",
           "request_json", "write_failure"]

logger = logging.getLogger(__name__)

# One retry: two total attempts bound both latency and duplicate reads.
READ_ATTEMPTS = 2
READ_TIMEOUT = 15.0
RETRY_WAIT_SECONDS = 0.1
MESSAGE_MAX = 200

# Machine-readable codes carried in ToolResult.meta["error"].
ERROR_INVALID_ARGS = "invalid_args"
ERROR_NOT_FOUND = "not_found"
ERROR_FORBIDDEN = "forbidden"
ERROR_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
ERROR_INVALID_RESPONSE = "invalid_response"
ERROR_UPSTREAM_REJECTED = "upstream_rejected"
ERROR_WRITE_UNKNOWN = "write_unknown"

# Statuses that unambiguously describe a temporary upstream condition.
_TRANSIENT_STATUS = frozenset({408, 429, 502, 503, 504})
_TRANSIENT_ERRORS = (
    httpx.NetworkError,
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

_NOT_FOUND_READ = "没有找到对应的内容（接口返回 404）"
_INVALID_RESPONSE_READ = "接口返回了无法解析的内容（本次未取到数据，不代表没有内容）"
_UNAVAILABLE_READ = "服务暂时不可用，请稍后再试"


class Failure(Exception):
    """Classified request failure; ``message`` is a sanitized backend message.

    ``attempts`` counts requests that actually left the process. The failure is
    raised internally and also returned by ``request_json`` so callers can build
    wording without catching exceptions."""

    __slots__ = ("code", "status", "message", "retryable", "attempts")

    def __init__(self, code: str, *, status: int | None = None, message: str = "",
                 retryable: bool = False, attempts: int = 0):
        super().__init__(code)
        self.code = code
        self.status = status
        self.message = message
        self.retryable = retryable
        self.attempts = attempts


class _Stopped(Exception):
    """The caller's stop signal was raised between attempts."""


def _safe_message(value) -> str:
    """Strip control characters and cap backend text before it reaches a model."""
    return _CONTROL.sub(" ", str(value or "")).strip()[:MESSAGE_MAX]


def _classify_status(status: int, *, write: bool) -> Failure:
    """Map one HTTP status to a typed failure, never retrying writes."""
    if status in (401, 403):
        return Failure(ERROR_FORBIDDEN, status=status)
    if status in (404, 410):
        return Failure(ERROR_NOT_FOUND, status=status)
    if status in (400, 422):
        return Failure(ERROR_INVALID_ARGS, status=status)
    if status in _TRANSIENT_STATUS:
        # Reads may safely repeat; writes may already have taken effect.
        code = ERROR_WRITE_UNKNOWN if write else ERROR_UPSTREAM_UNAVAILABLE
        return Failure(code, status=status, retryable=not write)
    if write:
        return Failure(ERROR_WRITE_UNKNOWN, status=status)
    if status >= 500:
        return Failure(ERROR_UPSTREAM_UNAVAILABLE, status=status)
    return Failure(ERROR_UPSTREAM_REJECTED, status=status)


def _decode(resp: httpx.Response, *, write: bool) -> dict:
    """Decode and validate the shared response envelope."""
    try:
        body = resp.json()
    except ValueError:
        code = ERROR_WRITE_UNKNOWN if write else ERROR_INVALID_RESPONSE
        raise Failure(code, status=resp.status_code) from None
    if not isinstance(body, dict):
        code = ERROR_WRITE_UNKNOWN if write else ERROR_INVALID_RESPONSE
        raise Failure(code, status=resp.status_code) from None
    if body.get("code") != 200:
        raise Failure(ERROR_UPSTREAM_REJECTED, status=resp.status_code,
                      message=_safe_message(body.get("message")))
    return body


def _attempt_once(client: httpx.Client, *, method: str, url: str, headers=None,
                  json_body=None, timeout: float, write: bool) -> dict:
    """Send one request and return the validated envelope or raise ``Failure``."""
    try:
        if method == "GET":
            resp = client.get(url, timeout=timeout, headers=headers)
        elif method == "POST":
            resp = client.post(url, timeout=timeout, headers=headers, json=json_body)
        else:
            resp = client.request(method, url, timeout=timeout, headers=headers,
                                  json=json_body)
    except (httpx.InvalidURL, httpx.UnsupportedProtocol):
        # A malformed endpoint is a caller bug: nothing was sent, nothing to retry.
        raise Failure(ERROR_INVALID_ARGS) from None
    except _TRANSIENT_ERRORS:
        code = ERROR_WRITE_UNKNOWN if write else ERROR_UPSTREAM_UNAVAILABLE
        raise Failure(code, retryable=not write) from None
    except httpx.HTTPError:
        code = ERROR_WRITE_UNKNOWN if write else ERROR_UPSTREAM_UNAVAILABLE
        raise Failure(code) from None
    if resp.status_code != 200:
        raise _classify_status(resp.status_code, write=write)
    return _decode(resp, write=write)


def request_json(client: httpx.Client, *, method: str, url: str, headers=None,
                 json_body=None, timeout: float = READ_TIMEOUT,
                 attempts: int = READ_ATTEMPTS,
                 stop: Callable[[], bool] | None = None,
                 write: bool = False) -> tuple[dict | None, Failure | None]:
    """Send a JSON request and return ``(envelope, failure)``; exactly one is set.

    Reads pass ``attempts=2`` (the default). Writes must pass ``attempts=1``
    and ``write=True`` so a possibly-applied request is never replayed."""
    retryer = Retrying(
        stop=stop_after_attempt(max(1, attempts)),
        wait=wait_fixed(RETRY_WAIT_SECONDS),
        retry=retry_if_exception(lambda exc: isinstance(exc, Failure) and exc.retryable),
        reraise=True,
        sleep=time.sleep,
    )
    sent = 0

    def send():
        nonlocal sent
        if stop is not None and stop():
            raise _Stopped()
        sent += 1
        return _attempt_once(client, method=method, url=url, headers=headers,
                             json_body=json_body, timeout=timeout, write=write)

    try:
        for attempt in retryer:
            with attempt:
                return send(), None
    except _Stopped:
        logger.warning("request stopped before send: method=%s url=%s", method, url)
        return None, Failure(ERROR_WRITE_UNKNOWN if write else ERROR_UPSTREAM_UNAVAILABLE,
                             attempts=sent)
    except Failure as failure:
        failure.attempts = sent
        logger.warning("request failed: method=%s url=%s code=%s status=%s attempts=%s",
                       method, url, failure.code, failure.status, failure.attempts)
        return None, failure
    return None, Failure(ERROR_WRITE_UNKNOWN if write else ERROR_UPSTREAM_UNAVAILABLE,
                         attempts=sent)


def _meta(failure: Failure) -> dict:
    meta = {"error": failure.code, "attempts": failure.attempts,
            "retryable": failure.retryable}
    if failure.status is not None:
        meta["status"] = failure.status
    return meta


def _rejected_text(failure: Failure, *, prefix: str = "", tail: str = "") -> str:
    message = failure.message or "接口拒绝了这次请求"
    return f"{prefix}接口报错: {message}{tail}"


def read_failure(failure: Failure, *, deny_msg: str,
                 not_found_text: str = "") -> ToolResult:
    """Build the read-side ToolResult for a classified failure."""
    meta = _meta(failure)
    if failure.code == ERROR_FORBIDDEN:
        return unavailable(deny_msg, meta)
    if failure.code == ERROR_NOT_FOUND:
        return not_found(not_found_text or _NOT_FOUND_READ, meta)
    if failure.code == ERROR_INVALID_ARGS:
        status = failure.status if failure.status is not None else "参数"
        return invalid_args(f"接口拒绝了请求参数（HTTP {status}），本次没有读取到数据", meta)
    if failure.code == ERROR_UPSTREAM_UNAVAILABLE:
        return unavailable(_UNAVAILABLE_READ, meta)
    if failure.code == ERROR_UPSTREAM_REJECTED:
        return unavailable(_rejected_text(failure), meta)
    return unavailable(_INVALID_RESPONSE_READ, meta)


def write_failure(failure: Failure, *, label: str, deny_msg: str,
                  tail: str) -> ToolResult:
    """Build the write-side ToolResult; unknown outcomes stay explicitly unknown."""
    meta = _meta(failure)
    if failure.code == ERROR_FORBIDDEN:
        return unavailable(deny_msg, meta)
    if failure.code == ERROR_NOT_FOUND:
        return not_found(
            f"{label}目标不存在（HTTP {failure.status}），本次未改动任何内容", meta)
    if failure.code == ERROR_INVALID_ARGS:
        return invalid_args(
            f"{label}请求参数被拒绝（HTTP {failure.status}），本次未改动任何内容", meta)
    if failure.code == ERROR_UPSTREAM_REJECTED:
        return unavailable(_rejected_text(failure, prefix=label, tail=tail), meta)
    return unavailable(
        f"{label}请求结果未知（没有收到可确认的回执）{tail}", meta)
