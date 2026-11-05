"""Safe SDK failure facts, independent of provider prose and exception payloads."""

import httpx
from openai import APIConnectionError, APIError, APIStatusError


def error_fields(error: BaseException | None) -> dict:
    if error is None:
        return {}
    fields: dict = {"error_type": type(error).__name__}
    if isinstance(error, APIStatusError):
        status = error.status_code
        fields.update(failure_source="provider", http_status=status,
                      retryable=status in {408, 409, 429} or status >= 500)
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
    )
