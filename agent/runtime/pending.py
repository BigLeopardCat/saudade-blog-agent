"""Lifecycle of one signed proposal, shared across migrated domains."""

from dataclasses import dataclass

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agent import confirm, idem
from agent.domains.registry import confirmed_domain, native_domain
from agent.runtime import writes
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from tools.base import ToolResult, unavailable


@dataclass(frozen=True)
class PendingOperation:
    grant: dict
    action: dict

    def context(self):
        return {"spec": self.grant["specs"][0], "target": self.action.get("target", "")}

    def _revoke(self, config, saver):
        check_cancelled(config)
        domain = confirmed_domain(self.grant)
        name, args = domain.validate(self.grant, config)
        store = writes.ledger_for(saver)
        key = writes.operation_key(self.grant, name, args)
        state, row = store.claim(key, name, self.grant["jti"], resuming=True)
        if state == "new":
            store.finish(key, ToolResult("这张确认卡已撤销或替换，未执行改动。", "unavailable",
                                         {"reason": "confirmation_revoked"}))
        elif state != "done" or (
                idem.replay_result(row).meta.get("reason") != "confirmation_revoked"):
            return unavailable("旧操作已执行或结果未知，不能将其声明为取消。请先查询实际状态。")
        return {"event": "cancelled", "task_id": self.action["task_id"],
                "jti": self.grant["jti"]}

    def transition(self, result, config, saver):
        execution = result.execution or {}
        if not (execution.get("pending_confirm") or execution.get("noop_note")):
            return result
        event = self._revoke(config, saver)
        if isinstance(event, ToolResult):
            return TurnResult(str(event))
        return TurnResult(result.reply, {"pending_confirm": None, "pending_action": None,
                                        **execution, "pending_event": event})

    def cancel_tool(self, saver):
        @tool(return_direct=True)
        def cancel_pending_operation(config: RunnableConfig) -> object:
            """用户明确要求放弃当前待确认操作时调用；撤销旧确认卡，不执行业务改动。"""
            return self.cancel(config, saver)
        return cancel_pending_operation

    def cancel(self, config, saver):
        event = self._revoke(config, saver)
        if isinstance(event, ToolResult):
            return TurnResult(str(event))
        return TurnResult("已取消这项待确认操作，没有执行改动。", {
            "pending_confirm": None, "pending_action": None,
            "pending_event": event, "receipts": []})


def restore(prior, config):
    popup, action = prior.get("pending_confirm"), prior.get("pending_action")
    if not isinstance(popup, dict) or not isinstance(action, dict):
        return None
    try:
        principal, cid = writes.bound_context(config)
        grant = confirm.verify(popup.get("token", ""), principal.uid, cid)
        domain = confirmed_domain(grant)
        if native_domain(grant) is None:
            return None
        domain.validate(grant, config)
        if (action.get("jti") != grant["jti"]
                or action.get("task_id") != f"pa_{grant['jti']}"):
            return None
    except ValueError:
        return None
    return PendingOperation(grant, action)
