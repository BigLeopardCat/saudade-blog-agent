"""Authorized read domains used by the agent tool loop.

This is a capability boundary: adding a tool here changes what the model can
read directly. Private reads require verified identity and a runtime scope
check. Writes use separate domain confirmation tools; browser commands have their own scope checks.
"""

from agent.authz import SCOPE_ADMIN_CONSOLE, SCOPE_READ_OWN, SCOPE_READ_PUBLIC, TOOL_SCOPE
from agent.runtime.access import own_reads_enabled as own_reads_enabled
from agent.runtime.access import scoped_reads_enabled
from tools.base import get_all_tools

ARTICLE_READ_TOOLS = frozenset({"rag_search", "search_notes", "get_article_detail"})
SITE_READ_TOOLS = frozenset({"get_blog_info", "get_social_links", "get_site_map"})
OWN_READ_TOOLS = frozenset({"list_my_favorites", "get_inbox_overview",
                            "list_notifications", "list_my_messages",
                            "list_my_board", "get_chat_history"})
ADMIN_READ_TOOLS = frozenset({"get_server_status", "get_service_health", "get_user_stats",
                              "get_note_stats", "get_note_periods"})
MODERATION_READ_TOOLS = frozenset({"get_moderation_status", "list_admin_board",
                                   "list_admin_comments"})

CATALOG_READ_TOOLS = frozenset({"list_admin_notes"})
ACCOUNT_READ_TOOLS = frozenset({"list_accounts"})

QUEUE_READ_TOOLS = frozenset({"list_quota_requests", "list_dashboard_todos"})


def _validate_scope(names: frozenset[str], scope: str):
    wrong_scope = {name: TOOL_SCOPE.get(name) for name in names
                   if TOOL_SCOPE.get(name) != scope}
    if wrong_scope:
        label = scope.removeprefix("read.")
        raise RuntimeError(f"Read tools must remain {label} reads: {wrong_scope}")


def _tools_with_scope(names: frozenset[str], scope: str) -> dict:
    _validate_scope(names, scope)
    selected = {tool.name: tool for tool in get_all_tools()
                if tool.name in names}
    missing = names - selected.keys()
    if missing:
        raise RuntimeError(f"Read tools missing from registry: {sorted(missing)}")
    return selected


def article_tools() -> dict:
    return _tools_with_scope(ARTICLE_READ_TOOLS, SCOPE_READ_PUBLIC)


def site_tools() -> dict:
    return _tools_with_scope(SITE_READ_TOOLS, SCOPE_READ_PUBLIC)


def admin_tools() -> dict:
    return _tools_with_scope(ADMIN_READ_TOOLS, SCOPE_ADMIN_CONSOLE)


def _domain_tools(tools, scope):
    selected = {item.name: item for item in tools}
    _validate_scope(frozenset(selected), scope)
    return selected


def moderation_tools() -> dict:
    from agent.domains import moderation

    return _domain_tools((moderation.get_moderation_status, moderation.list_admin_board,
                          moderation.list_admin_comments), SCOPE_ADMIN_CONSOLE)


def catalog_tools() -> dict:
    from agent.domains import catalog

    return _domain_tools((catalog.list_admin_notes,), SCOPE_ADMIN_CONSOLE)


def account_tools() -> dict:
    from agent.domains import catalog

    return _domain_tools((catalog.list_accounts,), SCOPE_ADMIN_CONSOLE)


def queue_tools() -> dict:
    from agent.domains import work_queue

    return _domain_tools((work_queue.list_quota_requests, work_queue.list_dashboard_todos),
                         SCOPE_ADMIN_CONSOLE)


def own_tools() -> dict:
    from agent.domains import own_activity, own_reads

    selected = _tools_with_scope(OWN_READ_TOOLS, SCOPE_READ_OWN)
    selected.update({tool.name: tool for tool in (
        own_reads.list_my_messages,
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


def public_domains() -> dict:
    from agent.domains import activity_search, public_activity, public_catalog
    from tools.site import get_current_time

    groups = {"public_catalog": (public_catalog.list_notes, public_catalog.get_top_notes,
                                  public_catalog.list_categories, public_catalog.list_tags),
              "public_activity": (activity_search.search_public_activity,
                                   public_activity.get_announcements,
                                   public_activity.list_guestbook,
                                   public_activity.list_talks),
              "utilities": (get_current_time,)}
    domains = {}
    for domain, tools in groups.items():
        names = frozenset(tool.name for tool in tools)
        _validate_scope(names, SCOPE_READ_PUBLIC)
        domains[domain] = {tool.name: tool for tool in tools}
    return domains


def device_domain(config) -> dict:
    from agent.domains import device_actions, device_reads

    cfg = (config or {}).get("configurable") or {}
    if (not device_reads.enabled() or type(cfg.get("user_id")) is not int
            or not scoped_reads_enabled(
            cfg.get("principal"), SCOPE_READ_OWN, cfg.get("user_id"))):
        return {}
    _tools_with_scope(frozenset({"list_devices"}), SCOPE_READ_OWN)
    return {"devices": {"list_devices": device_reads.list_devices, **device_actions.tools(config)}}
