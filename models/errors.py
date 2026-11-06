"""Safe SDK failure facts, independent of provider prose and exception payloads."""

import math
import re
import time
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx
from openai import APIConnectionError, APIError, APIStatusError

_QUOTA_ERRORS = {"insufficient_quota", "billing_hard_limit_reached"}
_ERROR_TYPES = {"server_error", "rate_limit_error", "insufficient_quota",
                "overloaded_error", "invalid_request_error"}


def retry_after(error: BaseException) -> float | None:
    """Read the provider's delay using HTTP seconds/date or SDK millisecond syntax."""
    if not isinstance(error, APIStatusError):
        return None
    headers = error.response.headers
    try:
        if headers.get("retry-after-ms") is not None:
            delay = float(headers["retry-after-ms"]) / 1000
        elif headers.get("retry-after") is not None:
            value = headers["retry-after"]
            try:
                delay = float(value)
            except ValueError:
                stamp = parsedate_to_datetime(value)
                delay = stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp() - time.time()
        else:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    return max(0.0, delay) if math.isfinite(delay) else None


def error_fields(error: BaseException | None) -> dict:
    if error is None:
        return {}
    fields: dict = {"error_type": type(error).__name__}
    if isinstance(error, APIStatusError):
        status = error.status_code
        code = error.code if isinstance(error.code, str) else None
        error_type = error.type if isinstance(error.type, str) else None
        fields.update(failure_source="provider", http_status=status,
                      retryable=(status in {408, 409, 429} or status >= 500)
                      and code not in _QUOTA_ERRORS and error_type != "insufficient_quota")
        if error_type in _ERROR_TYPES:
            fields["provider_error_type"] = error_type
        if code in _QUOTA_ERRORS:
            fields["provider_error_code"] = code
        delay = retry_after(error)
        if delay is not None:
            fields["retry_after_s"] = delay
        request_id = error.request_id
        if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
            fields["provider_request_id"] = request_id
    elif isinstance(error, (APIConnectionError, httpx.TransportError)):
        fields.update(failure_source="transport", retryable=True)
    elif isinstance(error, APIError):
        fields.update(failure_source="provider", retryable=False)
    else:
        fields.update(failure_source="application", retryable=False)
    return fields


def retryable_model_request(error: Exception) -> bool:
    """Retry rejected HTTP requests, excluding mid-stream failures and business tools."""
    return isinstance(error, APIStatusError) and (
        error.status_code == 429 or 500 <= error.status_code < 600
    ) and error_fields(error)["retryable"]
