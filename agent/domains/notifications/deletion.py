"""Remove only the authenticated recipient's notification copies."""
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agent.domains.removals import Identifiers, Kind, Removal

_domain = Removal('notification_deletions', 'delete_notifications',
                  '/api/protected/notification-deletions', '本人通知')
handles, validate, execute = _domain.handles, _domain.validate, _domain.execute
PERMISSION_OPERATIONS = frozenset({_domain.operation})


@tool(return_direct=True)
def prepare_notification_deletions(scope: Literal['selected', 'all'], config: RunnableConfig,
    ids: Identifiers = [], kind: Kind = 'all') -> object:
    """Prepare personal-notification deletion, distinct from marking read.
    selected needs IDs; all snapshots the current matching list without a prior read.
    kind filters notice/announcement copies. One approval, only this user's copies.
    Global announcement deletion uses admin_announcements; clarify ambiguous scope.
    """
    return _domain.prepare(scope, ids, kind, config)
