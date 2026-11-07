"""Fixed notification reads consume transaction-bound host facts."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from agent.domains.read_state import ReadState
from agent.runtime import writes
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import fact, ok

_Id = Annotated[StrictInt, Field(gt=0, le=2_147_483_647)]


class _ReadReceipt(BaseModel):
    model_config = ConfigDict(strict=True)
    scope: Literal["selected"]
    ownerUserId: _Id
    matchedIds: Annotated[list[_Id], Field(min_length=1, max_length=100)]
    changedIds: Annotated[list[_Id], Field(max_length=100)]
    isRead: StrictBool
    unreadCount: Annotated[StrictInt, Field(ge=0)]


class NotificationReads(ReadState):
    def invoke(self, args, config=None):
        if not self.available(config):
            return base.unavailable("需要已验签的本人身份，本次未执行通知变更。")
        principal, _ = writes.bound_context(config)
        check_cancelled(config)
        response = base._own_post("/api/protected/notifications/read", args, config)
        if isinstance(response, base.ToolResult):
            return response
        try:
            receipt = _ReadReceipt.model_validate(response)
        except ValueError:
            return base.unavailable(
                "标记请求已发出，但未收到可核对的事务回执；结果未知，请先查询状态。")
        if (receipt.ownerUserId != principal.uid or receipt.matchedIds != args["ids"]
                or receipt.isRead is not True
                or receipt.changedIds != sorted(set(receipt.changedIds))
                or not set(receipt.changedIds) <= set(receipt.matchedIds)):
            return base.unavailable("通知回执与本人或确认范围不一致；结果未知，请先查询状态。")
        count = len(receipt.changedIds)
        return ok(f"已核对所选通知均已读，本次标记 {count} 条。", meta=fact(
            self.workflow, changed=bool(count), ids=args["ids"], changed_ids=receipt.changedIds,
            unread_count=receipt.unreadCount,
            change=f"标记已读 {count} 条" if count else "本来就读过"))


_domain = NotificationReads("notifications", "notice_read", "read_notifications",
                            "notifications", "items", "通知")
handles = _domain.handles
validate = _domain.validate
execute = _domain.execute

PERMISSION_OPERATIONS = frozenset({_domain.tool})
