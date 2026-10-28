"""One signed approval and one atomic service transaction for an article batch."""

import time

from agent.domains.article_actions import targets
from agent.products.blog.confirmation import proposal
from agent.reports import sanitize_untrusted
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base
from tools.result import fact, ok

NAME = "set_article_status_batch"
MAX_ITEMS = 50


def validate(args):
    if not isinstance(args, dict) or set(args) != {"items", "status", "is_top"}:
        raise ValueError("批量文章参数无效")
    items = args["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError("每批须包含1至50篇文章")
    ids = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "version"}:
            raise ValueError("批量文章目标无效")
        if type(item["id"]) is not int or item["id"] <= 0:
            raise ValueError("批量文章编号无效")
        version = item["version"]
        if (
            not isinstance(version, str)
            or len(version) != 64
            or any(c not in "0123456789abcdef" for c in version)
        ):
            raise ValueError("批量文章版本无效")
        ids.append(item["id"])
    if len(set(ids)) != len(ids):
        raise ValueError("批量文章编号不能重复")
    if (
        args["status"] not in (None, "public", "private", "draft")
        or (
            args["is_top"] is not None
            and (type(args["is_top"]) is not int or args["is_top"] not in (0, 1))
        )
        or (args["status"] is None and args["is_top"] is None)
    ):
        raise ValueError("批量文章目标状态无效")
    return args


def prepare(article_ids, category, status, is_top, config):
    selection = {"articleIds": article_ids} if article_ids is not None else {"category": category}
    rows = base._admin_request("POST", targets.PATH + "batch/prepare", selection, config)
    if isinstance(rows, base.ToolResult):
        return base.unavailable(
            "未完成批量目标核对，本次未执行任何改动。请核对选择范围，或稍后重试。"
        )
    try:
        if not isinstance(rows, list) or len(rows) > MAX_ITEMS:
            raise ValueError("无效批量目标")
        current = [targets.row(row) for row in rows]
        ids = [row["id"] for row in current]
        if len(set(ids)) != len(ids) or (article_ids is not None and set(ids) != set(article_ids)):
            raise ValueError("批量目标与请求不符")
    except (ValueError, TypeError, KeyError):
        return base.unavailable("批量文章范围或版本不完整，本次未准备或执行改动。")
    change = {"status": status, "is_top": is_top}
    changed = [row for row in current if not targets.satisfied(row, change)]
    if not changed:
        text = f"匹配{len(current)}篇文章，均无需改动。"
        return TurnResult(text, {"noop_note": text, "noop_text": text, "receipts": []})
    args = validate(
        {"items": [{"id": row["id"], "version": row["version"]} for row in changed], **change}
    )
    lines = [f"批量修改{len(changed)}篇文章：{targets.description(change)}", ""]
    lines.extend(f"- #{row['id']}《{sanitize_untrusted(row['title'], 100)}》" for row in changed)
    if len(current) != len(changed):
        lines.append(f"\n另有{len(current) - len(changed)}篇已处于目标状态，无需改动。")
    text = "\n".join(lines)
    principal, cid = bound_context(config)
    check_cancelled(config)
    return proposal(
        principal,
        cid,
        "set_status",
        {"tool": NAME, "args": args},
        executor="article_state",
        question=text + "\n\n确认后整批执行。",
        target=text,
    )


class Operation:
    def invoke(self, args, config=None):
        validate(args)
        check_cancelled(config)
        rows = base._admin_request(
            "POST",
            targets.PATH + "batch",
            {"items": args["items"], "status": args["status"], "isTop": args["is_top"]},
            config,
        )
        if isinstance(rows, base.ToolResult):
            return rows
        try:
            if not isinstance(rows, list) or len(rows) != len(args["items"]):
                raise ValueError("不完整批量回执")
            expected = {item["id"]: item["version"] for item in args["items"]}
            states = []
            for row in rows:
                state = targets.row(row["state"])
                if (
                    type(row["changed"]) is not bool
                    or row["beforeVersion"] != expected.get(state["id"])
                    or not targets.satisfied(state, args)
                ):
                    raise ValueError("批量回执与已确认范围不符")
                states.append(state)
            if len({row["id"] for row in states}) != len(expected):
                raise ValueError("重复批量回执")
        except (ValueError, TypeError, KeyError):
            return base.unavailable("批量请求已发出，但回执不完整；结果未知，请先查询实际状态。")
        count = sum(row["changed"] for row in rows)
        return ok(
            f"批量文章操作完成：{count}篇已修改，{len(rows) - count}篇无需改动。",
            meta=fact("set_status", changed=count > 0, confirmed_args=args, states=states),
        )


def receipt(name, args, result):
    if not isinstance(result, base.ToolResult) or result.kind != "ok":
        return None
    meta = result.meta or {}
    if (
        meta.get("op") != "set_status"
        or meta.get("confirmed_args") != args
        or type(meta.get("changed")) is not bool
    ):
        return None
    try:
        states = [targets.row(row) for row in meta["states"]]
        if (
            {row["id"] for row in states} != {row["id"] for row in args["items"]}
            or len(states) != len(args["items"])
            or not all(targets.satisfied(row, args) for row in states)
        ):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    return {
        "skill": "set_status",
        "tool": name,
        "args": args,
        "result": str(result),
        "ts": time.time(),
        "action": str(result),
    }
