"""The migrated confirmation domains; no model intent matching belongs here."""

from agent.domains.favorites import transactions as favorites
from agent.domains.notifications import transactions as notifications


def confirmed_domain(grant):
    for domain in (favorites, notifications):
        if domain.handles(grant):
            return domain
    return None
