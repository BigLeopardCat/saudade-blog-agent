"""Manage one verified pending favorite proposal without executing site writes."""

from dataclasses import dataclass
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent import confirm, idem
from agent.domains.favorites import preparation, transactions
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from tools.base import ToolResult, unavailable


@dataclass(frozen=True)
class PendingFavorite:
    grant: dict
    action: dict

    def context(self):
        spec = self.grant["specs"][0]
        return {"tool": spec["tool"], "article_id": spec["args"]["article_id"],
                "target": self.action.get("target", "")}

    def _revoke(self, config, saver):
        check_cancelled(config)
        name, args = transactions._bound_spec(self.grant, config)
        store = transactions._ledger(saver)
        key = transactions.operation_key(self.grant, name, args)
        state, row = store.claim(key, name, self.grant["jti"], resuming=True)
        if state == "new":
            result = ToolResult("这张确认卡已撤销或替换，未执行收藏改动。", "unavailable",
                                {"reason": "confirmation_revoked"})
            store.finish(key, result)
        elif state != "done" or (
                idem.replay_result(row).meta.get("reason") != "confirmation_revoked"):
            return unavailable("旧操作已执行或结果未知，不能将其声明为取消。请先查询收藏状态。")
        return {"event": "cancelled", "task_id": self.action["task_id"],
                "jti": self.grant["jti"]}

    def tools(self, own_tools, saver):
        @tool(return_direct=True)
        def cancel_pending_favorite(config: RunnableConfig) -> object:
            """用户明确要求放弃当前待确认的收藏操作时调用；撤销旧卡，不修改收藏。"""
            event = self._revoke(config, saver)
            if isinstance(event, ToolResult):
                return TurnResult(str(event))
            return TurnResult("已取消这项待确认操作，没有修改收藏。", {
                "pending_confirm": None, "pending_action": None,
                "pending_event": event, "receipts": []})

        @tool(return_direct=True)
        def prepare_favorite_change(
            action: Literal["add", "remove"],
            article_id: Annotated[StrictInt, Field(gt=0)],
            config: RunnableConfig,
        ) -> object:
            """用户要求更换待确认收藏的文章或动作时调用；核对新目标，撤销旧卡，再准备新卡。"""
            result = preparation.prepare_favorite_change.invoke(
                {"action": action, "article_id": article_id}, config=config)
            if not isinstance(result, TurnResult):
                return result
            event = self._revoke(config, saver)
            if isinstance(event, ToolResult):
                return TurnResult(str(event))
            execution = {"pending_confirm": None, "pending_action": None,
                         **(result.execution or {}), "pending_event": event}
            return TurnResult(result.reply, execution)

        return {"list_my_favorites": own_tools["list_my_favorites"],
                cancel_pending_favorite.name: cancel_pending_favorite,
                prepare_favorite_change.name: prepare_favorite_change}


def restore(prior, config):
    """Only signed, identity-bound favorite proposals belong to this domain."""
    popup, action = prior.get("pending_confirm"), prior.get("pending_action")
    if not isinstance(popup, dict) or not isinstance(action, dict):
        return None
    try:
        principal, cid = transactions.bound_context(config)
        grant = confirm.verify(popup.get("token", ""), principal.uid, cid)
        if not transactions.handles(grant) or grant.get("executor") != "favorites":
            return None
        transactions._bound_spec(grant, config)
        if (action.get("jti") != grant["jti"]
                or action.get("task_id") != f"pa_{grant['jti']}"):
            return None
    except ValueError:
        return None
    return PendingFavorite(grant, action)
