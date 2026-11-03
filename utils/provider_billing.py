"""Validate OpenCode's USD billing receipt independently of token estimates.

Contract: https://opencode.ai/v2/docs/console/api/inference#cost
"""

import math

AMOUNT_FIELDS = ("provider_cost_usd", "provider_list_cost_usd", "provider_allowance_usd")
_SOURCES = frozenset({"free", "balance", "go", "go-plus", "byok"})


def amount(value):
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def sanitize(fields):
    if (not isinstance(fields, dict)
            or not isinstance(fields.get("provider_billing_source"), str)
            or fields["provider_billing_source"] not in _SOURCES):
        return {}
    result = {"provider_billing_source": fields["provider_billing_source"]}
    for name in AMOUNT_FIELDS:
        value = amount(fields.get(name))
        if value is not None:
            result[name] = value
    if type(fields.get("provider_cost_is_estimate")) is bool:
        result["provider_cost_is_estimate"] = fields["provider_cost_is_estimate"]
    return result


def from_usage(usage):
    if not isinstance(usage, dict) or not isinstance(usage.get("opencode"), dict):
        return {}
    billing = usage["opencode"]
    return sanitize({"provider_billing_source": billing.get("billing_source"),
                     "provider_cost_usd": usage.get("cost"),
                     "provider_list_cost_usd": billing.get("list_cost"),
                     "provider_allowance_usd": billing.get("allowance"),
                     "provider_cost_is_estimate": billing.get("estimated")})
