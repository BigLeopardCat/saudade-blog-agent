"""Access to blog-specific source observations in conversation history."""

from agent.authz import SCOPE_WRITE_CONSOLE, SCOPE_WRITE_OWN, check
from agent.domains import browser, conversation_images
from agent.domains.favorites import preparation as favorites
from agent.domains.messages import preparation as messages
from agent.domains.notifications import preparation as notifications
from agent.products.blog.admin_domains import preparation_names
from agent.runtime.access import scoped_reads_enabled

_OWN_PREPARATIONS = frozenset(tool.name for tool in (
    favorites.prepare_favorite_change, messages.prepare_message_read,
    notifications.prepare_notification_read))


def tool_access(principal, name):
    # Historical facts grant no execution authority. Source/role matching is also
    # required by memory.history before any tool observation reaches the model.
    if name == "cancel_pending_operation" or name in _OWN_PREPARATIONS:
        return scoped_reads_enabled(principal, SCOPE_WRITE_OWN)
    if name in preparation_names():
        return scoped_reads_enabled(principal, SCOPE_WRITE_CONSOLE)
    return (browser.history_allowed(principal, name)
            or conversation_images.history_allowed(principal, name)
            or check(principal, name).allowed)
