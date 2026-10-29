"""Prepare a fixed notification scope without business writes."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agent.domains.notifications.transactions import _domain
from agent.domains.read_state import Identifiers, Selection

available = _domain.available
snapshot = _domain.snapshot


@tool(return_direct=True)
def prepare_notification_read(scope: Selection, config: RunnableConfig,
                              ids: Identifiers = []) -> object:
    """用户要求将本人通知标记已读时准备确认卡，不写入。

    selected 需具体编号；all_unread 仅用于用户明确要求全部未读。
    准备时固定待标记编号，执行只处理该集合，之后新到的通知不包含在内。
    """
    return _domain.prepare(scope, ids, config)


def tools(own_tools):
    return {"list_notifications": own_tools["list_notifications"],
            prepare_notification_read.name: prepare_notification_read}
