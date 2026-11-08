"""Access to blog-specific source observations in conversation history."""

from agent.authz import (
    SCOPE_ADMIN_CONSOLE,
    SCOPE_READ_OWN,
    SCOPE_READ_PUBLIC,
    SCOPE_WRITE_CONSOLE,
    SCOPE_WRITE_OWN,
    TOOL_SCOPE,
    check,
)
from agent.domains import browser, conversation_images
from agent.domains.favorites import preparation as favorites
from agent.domains.messages import preparation as messages
from agent.domains.notifications import preparation as notifications
from agent.domains.notifications.deletion import prepare_notification_deletions
from agent.products.blog.admin_domains import preparation_names
from agent.products.blog.catalog import DOMAIN_DESCRIPTIONS
from agent.runtime import task_planning
from agent.runtime.access import scoped_reads_enabled

_OWN_PREPARATIONS = frozenset(tool.name for tool in (
    favorites.prepare_favorite_change, messages.prepare_message_read,
    notifications.prepare_notification_read, prepare_notification_deletions))


def tool_access(principal, name):
    # Reading historical receipts grants no execution authority; actor/role filtering also applies.
    if name == "get_unread_counts":
        return scoped_reads_enabled(principal, SCOPE_READ_OWN)
    if name in {f"load_{domain}_info_tools" for domain in DOMAIN_DESCRIPTIONS}:
        # Discovery receipts contain capability names, not grants or business records.
        return scoped_reads_enabled(principal, SCOPE_READ_PUBLIC)
    if name == "cancel_pending_operation" or name in _OWN_PREPARATIONS:
        return scoped_reads_enabled(principal, SCOPE_WRITE_OWN)
    if name in preparation_names():
        return scoped_reads_enabled(principal, SCOPE_WRITE_CONSOLE)
    return (browser.history_allowed(principal, name)
            or conversation_images.history_allowed(principal, name)
            or check(principal, name).allowed)


def read_tool(name, catalog, authorized):
    excluded = preparation_names() | _OWN_PREPARATIONS | task_planning.tools().keys()
    tool = catalog.get(name)
    if (tool is None or name in excluded or tool.return_direct or not authorized(name)
            or TOOL_SCOPE.get(name) not in {SCOPE_READ_PUBLIC, SCOPE_READ_OWN,
                                           SCOPE_ADMIN_CONSOLE}):
        return None
    return tool
