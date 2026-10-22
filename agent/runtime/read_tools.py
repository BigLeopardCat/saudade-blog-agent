"""Read domains exposed to the migration runtime.

This is a capability boundary: adding a tool here changes what the model can
read directly. Private reads require verified identity and a runtime scope
check. Writes use separate domain confirmation tools; browser commands remain in the legacy path.
"""

from agent.authz import SCOPE_ADMIN_CONSOLE, SCOPE_READ_OWN, SCOPE_READ_PUBLIC, TOOL_SCOPE, holds
from agent.principal import SOURCE_ASSERTION
from tools.base import get_all_tools

ARTICLE_READ_TOOLS = frozenset({"rag_search", "search_notes", "get_article_detail"})
SITE_READ_TOOLS = frozenset({"get_blog_info", "get_social_links", "get_site_map"})
OWN_READ_TOOLS = frozenset({"list_my_favorites", "get_unread_summary",
                            "list_notifications", "list_my_messages",
                            "list_my_board", "get_chat_history"})
ADMIN_READ_TOOLS = frozenset({"get_server_status", "get_service_health", "get_user_stats",
                              "get_note_stats", "get_note_periods"})
MODERATION_READ_TOOLS = frozenset({"get_moderation_status", "list_admin_board",
                                   "list_admin_comments"})

CATALOG_READ_TOOLS = frozenset({"list_admin_notes", "list_accounts"})

QUEUE_READ_TOOLS = frozenset({"list_quota_requests", "list_dashboard_todos"})

_ADMIN_DESCRIPTIONS = {
    "get_server_status": "Read server CPU, load, memory, disk usage and uptime. Admin only.",
    "get_service_health": "Read service health, restarts and recent diagnostic counts. Admin only.",
    "get_user_stats": "Read total users, role distribution and recent activity. Admin only.",
    "get_note_stats": "Read visible articles' aggregate traffic, rankings and trend. Admin only.",
    "get_note_periods": "Read article traffic by week, month or year, including partial periods. "
                        "Choose kind from the user's requested period. Admin only.",
}


def scoped_reads_enabled(principal, scope: str, user_id=None) -> bool:
    return (getattr(principal, "source", None) == SOURCE_ASSERTION
            and type(getattr(principal, "uid", None)) is int
            and principal.uid > 0 and holds(principal, scope)
            and (user_id is None or (type(user_id) is int and user_id == principal.uid)))


def own_reads_enabled(principal) -> bool:
    return scoped_reads_enabled(principal, SCOPE_READ_OWN)


def _tools_with_scope(names: frozenset[str], scope: str) -> dict:
    wrong_scope = {name: TOOL_SCOPE.get(name) for name in names
                   if TOOL_SCOPE.get(name) != scope}
    if wrong_scope:
        label = scope.removeprefix("read.")
        raise RuntimeError(f"Hybrid tools must remain {label} reads: {wrong_scope}")
    selected = {tool.name: tool for tool in get_all_tools()
                if tool.name in names}
    missing = names - selected.keys()
    if missing:
        raise RuntimeError(f"Hybrid tools missing from registry: {sorted(missing)}")
    return selected


def article_tools() -> dict:
    return _tools_with_scope(ARTICLE_READ_TOOLS, SCOPE_READ_PUBLIC)


def site_tools() -> dict:
    return _tools_with_scope(SITE_READ_TOOLS, SCOPE_READ_PUBLIC)


def admin_tools() -> dict:
    selected = _tools_with_scope(ADMIN_READ_TOOLS, SCOPE_ADMIN_CONSOLE)
    return {name: tool.model_copy(update={"description": _ADMIN_DESCRIPTIONS[name]})
            for name, tool in selected.items()}


def moderation_tools() -> dict:
    from agent.domains import moderation

    selected = _tools_with_scope(MODERATION_READ_TOOLS, SCOPE_ADMIN_CONSOLE)
    selected.update({tool.name: tool for tool in (moderation.get_moderation_status,
                    moderation.list_admin_board, moderation.list_admin_comments)})
    return selected


def catalog_tools() -> dict:
    from agent.domains import catalog

    selected = _tools_with_scope(CATALOG_READ_TOOLS, SCOPE_ADMIN_CONSOLE)
    selected.update({tool.name: tool for tool in (catalog.list_admin_notes, catalog.list_accounts)})
    return selected


def queue_tools() -> dict:
    from agent.domains import work_queue

    selected = _tools_with_scope(QUEUE_READ_TOOLS, SCOPE_ADMIN_CONSOLE)
    selected.update({tool.name: tool for tool in (work_queue.list_quota_requests,
                                                 work_queue.list_dashboard_todos)})
    return selected


def own_tools() -> dict:
    from agent.domains import own_activity, own_reads

    selected = _tools_with_scope(OWN_READ_TOOLS, SCOPE_READ_OWN)
    selected.update({tool.name: tool for tool in (
        own_reads.get_unread_summary, own_reads.list_my_messages,
        own_activity.list_my_board, own_activity.get_chat_history)})
    return selected


def own_domains(tools: dict) -> dict:
    names = {"favorites": "list_my_favorites", "notifications": "list_notifications",
             "messages": "list_my_messages"}
    domains = {domain: {name: tools[name]} for domain, name in names.items() if name in tools}
    activity = {name: tools[name] for name in ("list_my_board", "get_chat_history")
                if name in tools}
    if activity:
        domains["own_activity"] = activity
    return domains
