"""Resolve received-message IDs and prepare a signed confirmation."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agent.domains.messages.transactions import _domain
from agent.domains.read_state import Identifiers, Selection

available = _domain.available


@tool(return_direct=True)
def prepare_message_read(scope: Selection, config: RunnableConfig,
                         ids: Identifiers = []) -> object:
    """用户明确要求把本人收到的站内信标记已读时准备确认卡，不写入。

    selected 需收件箱具体编号；all_unread 仅用于明确要求全部未读。
    不能改变发件箱的已读状态。单纯查看信件不代表要求标记已读。
    """
    return _domain.prepare(scope, ids, config)


def tools(own_tools):
    return {"list_my_messages": own_tools["list_my_messages"],
            prepare_message_read.name: prepare_message_read}
