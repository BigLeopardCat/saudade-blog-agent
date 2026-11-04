"""Delete a fixed administrator-selected announcement set and its linked copies."""
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agent.domains.removals import Identifiers, Removal

_domain = Removal('announcement_deletions', 'delete_announcements',
                  '/api/protected/announcement-deletions', '全站公告', admin=True)
handles, validate, execute = _domain.handles, _domain.validate, _domain.execute
PERMISSION_OPERATIONS = frozenset({_domain.operation})


@tool(return_direct=True)
def prepare_announcement_deletions(scope: Literal['selected', 'all'], config: RunnableConfig,
    ids: Identifiers = []) -> object:
    """Prepare deletion of site-wide dashboard announcements. Admin only.
    selected needs IDs; all snapshots current announcements without a prior read.
    One approval includes linked notification copies removed for all recipients.
    Personal inbox clearing uses notifications; clarify ambiguous scope.
    """
    return _domain.prepare(scope, ids, 'all', config)
