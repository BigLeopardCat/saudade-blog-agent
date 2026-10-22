"""Fixed-scope read markers shared by notifications and received messages."""

import time
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlencode

from pydantic import Field, StrictInt

from agent import action_text, authz
from agent.runtime import writes
from agent.runtime.confirmation import proposal
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import fact, ok

Selection = Literal["selected", "all_unread"]
Identifiers = Annotated[list[Annotated[StrictInt, Field(gt=0)]], Field(max_length=100)]


@dataclass(frozen=True)
class ReadState:
    executor: str
    workflow: str
    tool: str
    collection: str
    items_key: str
    label: str
    recipient: bool = False

    def handles(self, grant):
        return (isinstance(grant, dict) and grant.get("executor") == self.executor
                and grant.get("workflow") == self.workflow)

    def available(self, config):
        try:
            principal, _ = writes.bound_context(config)
        except ValueError:
            return False
        return authz.check(principal, self.tool).allowed

    def snapshot(self, config, ids=None, unread_only=False):
        path = f"/api/protected/{self.collection}"
        if self.recipient:
            params = {"ids": ",".join(map(str, ids))} if ids else (
                {"unreadOnly": "true"} if unread_only else {})
            if params:
                path += "?" + urlencode(params)
        data = base._own_get(path, config)
        if isinstance(data, base.ToolResult):
            return None, data
        items = data.get(self.items_key) if isinstance(data, dict) else None
        rows = {}
        principal = ((config or {}).get("configurable") or {}).get("principal")
        if isinstance(items, list):
            for row in items:
                if (not isinstance(row, dict) or type(row.get("id")) is not int
                        or row["id"] <= 0 or row["id"] in rows
                        or type(row.get("isRead")) is not bool
                        or (self.recipient and (type(row.get("toUserId")) is not int
                                                or row["toUserId"] != principal.uid))):
                    break
                rows[row["id"]] = row
            else:
                return (data, rows), None
        return None, base.unavailable(f"未能核对本人{self.label}及已读状态，本次未改动。")

    def prepare(self, scope, ids, config):
        check_cancelled(config)
        if not self.available(config):
            return base.unavailable("需要已验签的本人身份和稳定会话，本次未生成确认卡。")
        if (scope == "selected" and not ids) or (scope == "all_unread" and ids):
            return base.unavailable(f"请明确{self.label}范围：提供具体编号，或选择全部未读。")
        value, error = self.snapshot(config, ids=ids if scope == "selected" else None,
                                     unread_only=scope == "all_unread")
        if error is not None:
            return error
        data, rows = value
        selected = sorted(set(ids)) if scope == "selected" else sorted(rows)
        if any(key not in rows for key in selected):
            return base.unavailable(f"部分编号不在本人可核对的{self.label}列表中，本次未改动。")
        unread = [key for key in selected if not rows[key]["isRead"]]
        if scope == "all_unread" and (
                type(data.get("unread")) is not int or data["unread"] != len(unread)
                or (self.recipient and data.get("inboxComplete") is not True)):
            return base.unavailable(f"列表无法证明已覆盖全部未读{self.label}，请指定具体编号。")
        if not unread:
            text = f"所选{self.label}均已读，本次无需改动。"
            return TurnResult(text, {"noop_note": text, "noop_text": text, "receipts": []})
        labels = [f"{key}《{str(rows[key].get('title') or '无标题')[:80]}》" for key in unread]
        target = f"将{self.label} " + "、".join(labels) + " 标记为已读"
        question = target + f"？已读不可撤销；确认后新到的{self.label}不包含在内。"
        if len(question) > 500:
            return base.unavailable("所选内容过多，无法完整展示确认范围，请分批选择。")
        principal, cid = writes.bound_context(config)
        check_cancelled(config)
        return proposal(principal, cid, self.workflow, {
            "tool": self.tool, "args": {"ids": unread, "all": False}},
            executor=self.executor, question=question, target=target,
            reply=question + "\n点「确定」后才会处理，也可以告诉我修改或取消。")

    def validate(self, grant, config):
        principal = writes.validate_binding(grant, config)
        specs = grant.get("specs")
        if not self.handles(grant) or not isinstance(specs, list) or len(specs) != 1:
            raise ValueError("确认凭据不是本领域的单件已读操作")
        spec = specs[0]
        args = spec.get("args") if isinstance(spec, dict) else None
        if (not isinstance(spec, dict) or spec.get("tool") != self.tool
                or not isinstance(args, dict) or set(args) != {"ids", "all"}
                or args["all"] is not False):
            raise ValueError("确认参数必须是固定编号范围")
        ids = args["ids"]
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
                or any(type(key) is not int or key <= 0 for key in ids)
                or ids != sorted(set(ids))):
            raise ValueError("编号无效或不规范")
        if not authz.check(principal, self.tool).allowed:
            raise ValueError("当前身份无权标记已读")
        return self.tool, args

    def invoke(self, args, config=None):
        value, error = self.snapshot(config, ids=args["ids"])
        if error is not None:
            return error
        _, rows = value
        ids = args["ids"]
        if any(key not in rows for key in ids):
            return base.unavailable("部分已确认目标已不在可核对的列表中，本次未改动。")
        unread = [key for key in ids if not rows[key]["isRead"]]
        if unread:
            check_cancelled(config)
            out = base._own_post(f"/api/protected/{self.collection}/read",
                                 {"ids": unread, "all": False}, config)
            if isinstance(out, base.ToolResult):
                return out
            after, error = self.snapshot(config, ids=ids)
            if error is not None or any(
                    key not in after[1] or after[1][key]["isRead"] is not True for key in ids):
                return base.unavailable("标记请求已发出，但未能核对全部目标已读；"
                                        "结果未知，请先查询状态。")
        return ok(f"已核对所选{self.label}均已读，本次标记 {len(unread)} 条。", meta=fact(
            self.workflow, changed=bool(unread), ids=ids,
            change=f"标记已读 {len(unread)} 条" if unread else "本来就读过"))

    def receipt(self, grant, name, args, result):
        meta = getattr(result, "meta", {}) or {}
        if (not isinstance(result, base.ToolResult) or result.kind != "ok"
                or not isinstance(meta, dict) or meta.get("op") != self.workflow
                or meta.get("ids") != args["ids"] or type(meta.get("changed")) is not bool):
            return None
        receipt = {"skill": self.workflow, "tool": name, "args": args,
                   "result": str(result), "ts": time.time()}
        if not meta["changed"]:
            receipt["change"] = meta.get("change", "本来就读过")
        receipt["action"] = action_text.receipt_action(name, args, receipt)
        return receipt

    def execute(self, grant, config, saver, *, tools=None, ledger=None):
        return writes.execute_confirmed(
            grant, config, saver, validate=self.validate, receipt=self.receipt,
            tool_factory=lambda name: {name: self} if tools is None else tools, ledger=ledger)
