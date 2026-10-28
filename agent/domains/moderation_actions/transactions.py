"""Execute one confirmed moderation decision through the shared durable ledger."""

import time

from agent import authz
from agent.domains.moderation_actions import targets
from agent.runtime import writes
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import fact, ok, tgt

_OPERATIONS = {
    ("board", False): ("board_audit", "audit_board_comment", "talk_id"),
    ("board", True): ("board_delete", "delete_board_comment", "talk_id"),
    ("comment", False): ("comment_audit", "audit_comment", "comment_id"),
    ("comment", True): ("comment_delete", "delete_comment", "comment_id"),
}


PERMISSION_OPERATIONS = frozenset(value[1] for value in _OPERATIONS.values())


def operation(resource, action):
    return _OPERATIONS[(resource, action == "delete")]


def handles(grant):
    return (
        isinstance(grant, dict)
        and grant.get("executor") == "moderation"
        and any(grant.get("workflow") == value[0] for value in _OPERATIONS.values())
    )


def _selection(grant):
    for (resource, delete), (workflow, name, key) in _OPERATIONS.items():
        if grant["workflow"] == workflow:
            return resource, delete, name, key
    raise ValueError("未知审核操作")


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if not handles(grant) or not isinstance(specs, list) or len(specs) != 1:
        raise ValueError("确认凭据不是单件审核操作")
    _, delete, name, key = _selection(grant)
    spec = specs[0]
    args = spec.get("args") if isinstance(spec, dict) else None
    expected = {key, "version"} | (set() if delete else {"verdict"})
    if (
        not isinstance(args, dict)
        or set(args) != expected
        or spec.get("tool") != name
        or type(args[key]) is not int
        or args[key] <= 0
        or not targets.version(args["version"])
        or (not delete and args["verdict"] not in ("pass", "reject"))
    ):
        raise ValueError("审核确认参数与操作不一致")
    if not authz.check(principal, name).allowed:
        raise ValueError("当前身份无权执行审核操作")
    return name, args


def _write_receipt(data, resource, identifier, action, version, actor_uid):
    if (
        not isinstance(data, dict)
        or data.get("resource") != targets.RESOURCES[resource]
        or type(data.get("id")) is not int
        or data["id"] != identifier
        or data.get("action") != action
        or data.get("confirmedVersion") != version
        or type(data.get("actorId")) is not int
        or data["actorId"] != actor_uid
        or type(data.get("changed")) is not bool
        or type(data.get("isDeleted")) is not bool
        or type(data.get("approved")) is not int
        or data["approved"] not in (0, 1, 2)
        or type(data.get("notifications")) is not int
        or not 0 <= data["notifications"] <= 1
        or (not data["changed"] and data["notifications"] != 0)
    ):
        return False
    return (
        data["isDeleted"]
        if action == "delete"
        else not data["isDeleted"] and data["approved"] == (1 if action == "pass" else 2)
    )


def _verified(resource, action, before, after):
    if action == "delete" and resource == "board":
        return after is None
    if after is None or any(
        after[key] != before[key] for key in ("id", "userId", "content", "articleId")
    ):
        return False
    if action == "delete":
        return after["isDeleted"]
    return not after["isDeleted"] and after["approved"] == (1 if action == "pass" else 2)


class Operation:
    def __init__(self, workflow, resource, action, key):
        self.workflow, self.resource, self.action, self.key = workflow, resource, action, key

    def invoke(self, args, config=None):
        principal, _ = writes.bound_context(config)
        identifier = args[self.key]
        before, error = targets.read(self.resource, identifier, config)
        if error is not None:
            return error
        if before is None or before["version"] != args["version"] or before["contentTruncated"]:
            return base.unavailable("已确认的目标已变化，请重新读取并确认；本次未执行。")
        check_cancelled(config)
        data = base._admin_request(
            "POST",
            f"{targets.PATH}/{targets.RESOURCES[self.resource]}/{identifier}/action",
            {"action": self.action, "version": args["version"]},
            config,
        )
        if isinstance(data, base.ToolResult):
            return data
        if not _write_receipt(
            data, self.resource, identifier, self.action, args["version"], principal.uid
        ):
            return base.unavailable("审核请求已发出，但缺少有效写入回执；结果未知，请先查询状态。")
        after, error = targets.read(self.resource, identifier, config)
        if error is not None or not _verified(self.resource, self.action, before, after):
            return base.unavailable("审核请求已发出，但实际状态未能复核；结果未知，请先查询状态。")
        return self._result(args, before, data)

    def _result(self, args, before, data):
        label = "河灯留言" if self.resource == "board" else "文章评论"
        state = (
            "已删除"
            if self.action == "delete"
            else "已通过审核"
            if self.action == "pass"
            else "已驳回"
        )
        reply = f"{label} {before['id']} {state}。"
        if self.resource == "comment":
            title = before["articleTitle"]
            title = "已不存在" if title is None else title or "无标题"
            reply += f"所属文章 {before['articleId']}《{title}》。"
        if not data["changed"]:
            reply += "本来已处于该状态，本次无需改动。"
        return ok(
            reply,
            meta=fact(
                self.workflow,
                changed=data["changed"],
                confirmed_args=args,
                target=tgt(
                    "board_comment" if self.resource == "board" else "comment", before["id"]
                ),
                action=self.action,
                resource=self.resource,
                state=data,
            ),
        )


def _receipt(grant, name, args, result):
    meta = getattr(result, "meta", {}) or {}
    if (
        not isinstance(result, base.ToolResult)
        or result.kind != "ok"
        or not isinstance(meta, dict)
        or meta.get("op") != grant["workflow"]
        or meta.get("confirmed_args") != args
        or type(meta.get("changed")) is not bool
    ):
        return None
    resource, delete, _, key = _selection(grant)
    action = "delete" if delete else args["verdict"]
    state = meta.get("state")
    if not isinstance(state, dict) or meta["changed"] != state.get("changed"):
        return None
    if not _write_receipt(state, resource, args[key], action, args["version"], grant["uid"]):
        return None
    return {
        "skill": grant["workflow"],
        "tool": name,
        "args": args,
        "result": str(result),
        "ts": time.time(),
        "action": str(result),
        **({"change": "本来已处于该状态"} if not meta["changed"] else {}),
    }


def execute(grant, config, saver, *, tools=None, ledger=None):
    def selected(name):
        resource, delete, _, key = _selection(grant)
        action = "delete" if delete else grant["specs"][0]["args"]["verdict"]
        return (
            {name: Operation(grant["workflow"], resource, action, key)} if tools is None else tools
        )

    return writes.execute_confirmed(
        grant,
        config,
        saver,
        validate=validate,
        receipt=_receipt,
        tool_factory=selected,
        ledger=ledger,
    )
