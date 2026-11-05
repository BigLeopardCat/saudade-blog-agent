"""Remove only the authenticated recipient's notification copies."""
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from agent.domains.removals import Identifiers, Removal

_domain = Removal('notification_deletions', 'delete_notifications',
                  '/api/protected/notification-deletions', '本人通知')
handles, validate, execute = _domain.handles, _domain.validate, _domain.execute
PERMISSION_OPERATIONS = frozenset({_domain.operation})

Selection = Annotated[Literal['all_received', 'announcement_copies', 'system_notices',
                              'selected_ids'], Field(description=
    'all_received includes every received notification type, including announcement '
    'copies. announcement_copies/system_notices select one type; selected_ids uses exact IDs. '
    'Every mode preserves global announcement originals.')]
_SELECTION = {'all_received': ('all', 'all'), 'announcement_copies': ('all', 'announcement'),
              'system_notices': ('all', 'notice'), 'selected_ids': ('selected', 'all')}


class DeletionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    selection: Selection
    ids: Identifiers = Field(default_factory=list)


@tool(args_schema=DeletionRequest, return_direct=True)
def prepare_notification_deletions(selection: Selection, config: RunnableConfig,
                                   ids: Identifiers = []) -> object:
    """准备删除本人收到的通知副本；任何选集都保留全站公告原文和其他人的通知。

    清空本人通知列表选择all_received，包含个人通知与公告副本，无需先读列表。
    仅删公告副本用announcement_copies，仅删系统/直接通知用system_notices；
    按明确编号删除用selected_ids并提供ids。一次确认固定本次选集。
    """
    scope, kind = _SELECTION[selection]
    return _domain.prepare(scope, ids, kind, config)
