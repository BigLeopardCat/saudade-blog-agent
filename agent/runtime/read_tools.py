"""Read domains exposed to the migration runtime.

This is a capability boundary: adding a tool here changes what the model can
read directly. Private reads require verified identity and a runtime scope
check. Writes and browser commands stay in the legacy transaction path.
"""

from agent.authz import SCOPE_READ_OWN, SCOPE_READ_PUBLIC, TOOL_SCOPE, holds
from agent.principal import SOURCE_ASSERTION
from tools.base import get_all_tools

ARTICLE_READ_TOOLS = frozenset({"rag_search", "search_notes", "get_article_detail"})
SITE_READ_TOOLS = frozenset({"get_blog_info", "get_social_links", "get_site_map"})
OWN_READ_TOOLS = frozenset({"list_my_favorites", "get_unread_summary",
                            "list_notifications", "list_my_messages"})


def own_reads_enabled(principal) -> bool:
    return (getattr(principal, "source", None) == SOURCE_ASSERTION
            and type(getattr(principal, "uid", None)) is int
            and principal.uid > 0 and holds(principal, SCOPE_READ_OWN))


def _tools_with_scope(names: frozenset[str], scope: str) -> dict:
    wrong_scope = {name: TOOL_SCOPE.get(name) for name in names
                   if TOOL_SCOPE.get(name) != scope}
    if wrong_scope:
        label = "public" if scope == SCOPE_READ_PUBLIC else "own"
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


def own_tools() -> dict:
    return _tools_with_scope(OWN_READ_TOOLS, SCOPE_READ_OWN)
