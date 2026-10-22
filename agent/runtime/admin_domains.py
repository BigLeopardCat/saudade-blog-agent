"""Assemble authorized administrator reads and confirmation preparation tools."""

from agent.authz import SCOPE_ADMIN_CONSOLE
from agent.domains.admin_writes import available
from agent.domains.article_actions import preparation as articles
from agent.domains.moderation_actions import preparation as moderation
from agent.domains.quota_actions import preparation as quotas
from agent.domains.todos import preparation as todos
from agent.runtime.read_tools import scoped_reads_enabled

_PREPARATIONS = (
    ("admin_catalog", articles),
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
            selected[name].update(preparation.tools(selected[name]))
    return selected
