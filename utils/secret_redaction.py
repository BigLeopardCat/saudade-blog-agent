"""Filter recognizable credentials from diagnostic copies, never from model or business data."""

import json
import re

REDACTED = "[REDACTED]"
_SECRET_FIELDS = frozenset({
    "authorization", "proxy_authorization", "api_key", "apikey", "access_token",
    "refresh_token", "id_token", "confirm_token", "confirm_tokens", "token",
    "password", "passwd", "client_secret", "secret_key", "jwt_secret",
    "agent_assertion_secret", "agent_confirm_secret", "blog_delegation_secret",
})
_BEARER = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]+")
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:(?:[a-z][a-z0-9]*_)?api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"password|passwd|jwt_secret|agent_assertion_secret|agent_confirm_secret|"
    r"blog_delegation_secret)\b[\"']?\s*[:=]\s*[\"']?)([^\s\"'`,;}&<>]+)"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z]+ )?PRIVATE KEY-----",
    re.DOTALL,
)


def redact_credentials(value, *, structured=True):
    """Preserve diagnostic structure and ordinary text; do not claim general PII removal."""
    if isinstance(value, dict):
        return {key: REDACTED if structured and isinstance(key, str) and (
                key.lower().replace("-", "_") in _SECRET_FIELDS or key.lower().endswith("_api_key"))
                else redact_credentials(item, structured=structured) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_credentials(item, structured=structured) for item in value)
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
            filtered = redact_credentials(parsed, structured=structured)
        except RecursionError:
            return REDACTED
        except ValueError:
            pass
        else:
            if filtered != parsed:
                return json.dumps(filtered, ensure_ascii=False, separators=(",", ":"))
    value = _PRIVATE_KEY.sub(REDACTED, value)
    value = _BEARER.sub(lambda match: match[1] + REDACTED, value)
    value = _ASSIGNMENT.sub(lambda match: match[1] + REDACTED, value)
    return _JWT.sub(REDACTED, _API_KEY.sub(REDACTED, value))
