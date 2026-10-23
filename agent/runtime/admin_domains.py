"""Assemble authorized administrator reads and confirmation preparation tools."""

from agent.authz import SCOPE_ADMIN_CONSOLE
from agent.domains.admin_writes import available
from agent.domains.article_actions import preparation as articles
from agent.domains.article_tags import preparation as article_tags
from agent.domains.categories import preparation as categories
from agent.domains.moderation_actions import preparation as moderation
from agent.domains.quota_actions import preparation as quotas
from agent.domains.todos import preparation as todos
from agent.runtime.read_tools import scoped_reads_enabled

_PREPARATIONS = (
    ("admin_catalog", articles),
    ("admin_catalog", article_tags),
    ("admin_categories", categories),
    ("admin_moderation", moderation),
    ("admin_work_queue", todos),
    ("admin_work_queue", quotas),
)


def domains(config, can_bridge, reads):
    cfg = (config or {}).get("configurable") or {}
    if not scoped_reads_enabled(cfg.get("principal"), SCOPE_ADMIN_CONSOLE, cfg.get("user_id")):
        return {}
    selected = {name: dict(tools) for name, tools in reads.items()}
    if can_bridge and available(config):
        for name, preparation in _PREPARATIONS:
            current = selected.setdefault(name, {})
            current.update(preparation.tools(current))
    return selected
