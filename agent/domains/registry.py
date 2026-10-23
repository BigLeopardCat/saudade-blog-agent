"""The migrated confirmation domains; no model intent matching belongs here."""

from agent.domains.account_actions import transactions as account_actions
from agent.domains.announcements import transactions as announcements
from agent.domains.article_actions import transactions as article_state
from agent.domains.article_tags import transactions as article_tags
from agent.domains.categories import transactions as categories
from agent.domains.favorites import transactions as favorites
from agent.domains.messages import transactions as messages
from agent.domains.moderation_actions import transactions as moderation
from agent.domains.notifications import transactions as notifications
from agent.domains.quota_actions import transactions as quota_actions
from agent.domains.tags import transactions as tags
from agent.domains.todos import transactions as todos

_DOMAINS = {"favorites": favorites, "notifications": notifications,
            "messages": messages, "todos": todos, "article_state": article_state,
            "moderation": moderation, "quota_actions": quota_actions, "article_tags": article_tags,
            "categories": categories, "tags": tags, "announcements": announcements,
            "account_actions": account_actions}


def native_domain(grant):
    executor = grant.get("executor") if isinstance(grant, dict) else None
    return _DOMAINS.get(executor) if isinstance(executor, str) else None


def confirmed_domain(grant):
    for domain in _DOMAINS.values():
        if domain.handles(grant):
            return domain
    return None
