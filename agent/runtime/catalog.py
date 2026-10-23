"""Promote small, frequent UI actions from the already authorized tool catalog."""

_BROWSER_ACTIONS = ("navigate_page", "set_page_appearance")


def initial_tools(tools, domains):
    # These operations remain authorized by the domain builder and again on invocation.
    browser = domains.get("site") or {}
    return {**tools, **{name: browser[name] for name in _BROWSER_ACTIONS if name in browser}}
