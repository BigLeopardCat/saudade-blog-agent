"""Execute a signed favorite operation after checking identity, scope and binding.

The operation ID identifies retries across restarts. The durable ledger records intent
before the external call and retains the verified receipt for replay."""

import time

from agent import action_text, authz
from agent.runtime import writes
from tools.base import ToolResult, get_all_tools

_WORKFLOWS = {"favorite_add": "add_favorite", "favorite_remove": "remove_favorite"}


def handles(grant) -> bool:
    return isinstance(grant, dict) and grant.get("workflow") in _WORKFLOWS


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    name = _WORKFLOWS[grant["workflow"]]
    specs = grant.get("specs")
    if not isinstance(specs, list) or len(specs) != 1 or not isinstance(specs[0], dict):
        raise ValueError("本路径只接受一件已确认的收藏操作")
    spec = specs[0]
    args = spec.get("args")
    if spec.get("tool") != name or not isinstance(args, dict) or set(args) != {"article_id"}:
        raise ValueError("确认参数与收藏操作不一致")
    if type(args["article_id"]) is not int or args["article_id"] <= 0:
        raise ValueError("文章标识无效")
    if not authz.check(principal, name).allowed:
        raise ValueError("当前身份没有执行这件操作的权限")
    return name, args


def _receipt(grant, name, args, result):
    meta = getattr(result, "meta", {}) or {}
    if not isinstance(meta, dict):
        return None
    target = meta.get("target") or {}
    if (not isinstance(result, ToolResult) or result.kind != "ok"
            or meta.get("op") != grant["workflow"]
            or type(meta.get("changed")) is not bool
            or not isinstance(target, dict) or target.get("kind") != "note"
            or target.get("id") != args["article_id"]):
        return None
    receipt = {"skill": grant["workflow"], "tool": name,
               "args": {"article_id": str(args["article_id"])},
               "result": str(result)[:200], "ts": time.time()}
    if not meta["changed"]:
        receipt["change"] = str(meta.get("change") or "状态已达成，未改动")[:120]
    receipt["action"] = action_text.receipt_action(name, receipt["args"], receipt)
    return receipt


def execute(grant, config, saver, *, tools=None, ledger=None):
    def selected(name):
        return ({tool.name: tool for tool in get_all_tools() if tool.name == name}
                if tools is None else tools)

    return writes.execute_confirmed(grant, config, saver, validate=validate,
                                    receipt=_receipt, tool_factory=selected, ledger=ledger)
