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

CopyType = Annotated[Literal['all', 'personal_notice_copies', 'announcement_copies'], Field(
    description='Filter only your received copies. all includes personal notices AND announcement '
                'copies. Keeping global announcements does not exclude your received copies.')]
_COPY_KINDS = {'all': 'all', 'personal_notice_copies': 'notice',
               'announcement_copies': 'announcement'}


class DeletionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: Literal['selected', 'all']
    ids: Identifiers = Field(default_factory=list)
    notification_type: CopyType = 'all'


@tool(args_schema=DeletionRequest, return_direct=True)
def prepare_notification_deletions(scope: Literal['selected', 'all'], config: RunnableConfig,
    ids: Identifiers = [], notification_type: CopyType = 'all') -> object:
    """Prepare deletion from your personal notification list; global announcements stay intact.
    all snapshots the current matching copies without a prior read; selected needs IDs.
    To clear the entire personal list, keep notification_type=all. Global announcement
    management is a separate domain and permission. One approval fixes this recipient's IDs.
    """
    return _domain.prepare(scope, ids, _COPY_KINDS[notification_type], config)
