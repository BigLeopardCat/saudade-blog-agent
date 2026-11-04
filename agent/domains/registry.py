"""The migrated confirmation domains; no model intent matching belongs here."""

from agent.domains import article_summary
from agent.domains.account_actions import transactions as account_actions
from agent.domains.announcements import deletion as announcement_deletions
from agent.domains.announcements import transactions as announcements
from agent.domains.article_actions import transactions as article_state
from agent.domains.article_tags import transactions as article_tags
from agent.domains.automations import transactions as automations
from agent.domains.categories import transactions as categories
from agent.domains.favorites import transactions as favorites
from agent.domains.gallery import transactions as gallery
from agent.domains.messages import transactions as messages
from agent.domains.moderation_actions import transactions as moderation
from agent.domains.notices import transactions as notices
from agent.domains.notifications import deletion as notification_deletions
from agent.domains.notifications import transactions as notifications
from agent.domains.quota_actions import transactions as quota_actions
from agent.domains.tags import transactions as tags
from agent.domains.todos import transactions as todos

_DOMAINS = {"announcement_deletions": announcement_deletions,
            "notification_deletions": notification_deletions,
            "favorites": favorites, "notifications": notifications,
            "messages": messages, "todos": todos, "article_state": article_state,
            "moderation": moderation, "quota_actions": quota_actions, "article_tags": article_tags,
            "categories": categories, "tags": tags, "announcements": announcements,
            "account_actions": account_actions, "notices": notices, "automations": automations,
            "editor_summary": article_summary, "gallery": gallery}


def native_domain(grant):
    executor = grant.get("executor") if isinstance(grant, dict) else None
    return _DOMAINS.get(executor) if isinstance(executor, str) else None


def confirmed_domain(grant):
    for domain in _DOMAINS.values():
        if domain.handles(grant):
            return domain
    return None


def operation_names():
    """Permission identities for server-executed operations, not model-callable tools."""
    return frozenset(name for domain in _DOMAINS.values()
                     for name in domain.PERMISSION_OPERATIONS)


def bound_tool_names():
    """Inventory service-bound tools without publishing an unusable global tool instance."""
    from agent.domains.article_editor import NAMES
    from agent.domains.automations.preparation import tools
    from agent.domains.gallery.preparation import tools as gallery_tools
    from agent.domains.gallery.reads import tools as gallery_reads
    return frozenset(tools(None, True)) | NAMES | frozenset(gallery_tools(gallery_reads()))
