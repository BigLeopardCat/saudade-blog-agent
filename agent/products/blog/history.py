"""Access to blog-specific source observations in conversation history."""

from agent.authz import check
from agent.domains import browser, conversation_images


def tool_access(principal, name):
    return (browser.history_allowed(principal, name)
            or conversation_images.history_allowed(principal, name)
            or check(principal, name).allowed)
