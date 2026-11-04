"""Assemble authorized administrator reads and confirmation preparation tools."""

from functools import cache

from agent.authz import SCOPE_ADMIN_CONSOLE
from agent.domains import article_editor
from agent.domains.account_actions import preparation as accounts
from agent.domains.admin_writes import available
from agent.domains.image_generation import tools as image_generation
from agent.domains.announcements import preparation as announcements
from agent.domains.article_actions import preparation as articles
from agent.domains.article_tags import preparation as article_tags
from agent.domains.categories import preparation as categories
from agent.domains.gallery import preparation as gallery_preparation
from agent.domains.gallery import reads as gallery_reads
from agent.domains.moderation_actions import preparation as moderation
from agent.domains.notices import preparation as notices
from agent.domains.quota_actions import preparation as quotas
from agent.domains.tags import preparation as tags
from agent.domains.todos import preparation as todos
from agent.products.blog.read_tools import scoped_reads_enabled

_PREPARATIONS = (
    ("admin_account_actions", accounts),
    ("admin_gallery", gallery_preparation),
    ("admin_image_generation", image_generation),
    ("admin_catalog", articles),
    ("admin_catalog", article_tags),
    ("admin_categories", categories),
    ("admin_tags", tags),
    ("admin_announcements", announcements),
    ("admin_notices", notices),
    ("admin_moderation", moderation),
    ("admin_work_queue", todos),
    ("admin_work_queue", quotas),
)


@cache
def preparation_names():
    """Use the same preparation registry for execution menus and historical access."""
    return frozenset(name for _, preparation in _PREPARATIONS
                     for name in preparation.tools({}))


def domains(config, can_prepare, reads):
    cfg = (config or {}).get("configurable") or {}
    if not scoped_reads_enabled(cfg.get("principal"), SCOPE_ADMIN_CONSOLE, cfg.get("user_id")):
        return {}
    selected = {name: dict(tools) for name, tools in reads.items()}
    selected["admin_gallery"] = gallery_reads.tools()
    selected["admin_image_generation"] = {
        image_generation.get_image_generation.name: image_generation.get_image_generation
    }
    editor_tools = article_editor.tools(config, can_prepare)
    if editor_tools:
        selected["article_editor"] = editor_tools
    if can_prepare and available(config):
        for name, preparation in _PREPARATIONS:
            current = selected.setdefault(name, {})
            current.update(preparation.tools(current))
    from agent.domains import public_catalog

    for domain, read in (("admin_tags", public_catalog.list_tags),
                         ("admin_categories", public_catalog.list_categories)):
        if domain in selected:
            selected[domain][read.name] = read
    return selected
