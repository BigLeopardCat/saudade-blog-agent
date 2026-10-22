"""Common admission check for native administrator confirmations."""

from agent import authz
from agent.runtime.writes import bound_context


def available(config):
    try:
        principal, _ = bound_context(config)
    except ValueError:
        return False
    return authz.holds(principal, authz.SCOPE_WRITE_CONSOLE)
