"""Use the standard approval ledger and authoritative Rust mutation responses."""

import time

from agent import authz
from agent.domains.gallery import image_actions, targets
from agent.runtime import writes
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import fact, ok, tgt

OPERATIONS = {"manage_gallery_images": "gallery_images", "set_article_cover": "article_cover"}
PERMISSION_OPERATIONS = frozenset(OPERATIONS)


def handles(grant):
    return (
        isinstance(grant, dict)
        and grant.get("executor") == "gallery"
        and grant.get("workflow") in OPERATIONS.values()
    )


def arguments(name, args):
    model = image_actions.Arguments if name == "manage_gallery_images" else targets.CoverArguments
    return model.model_validate(args).model_dump(exclude_none=True)


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if (
        not handles(grant)
        or not isinstance(specs, list)
        or len(specs) != 1
        or not isinstance(specs[0], dict)
    ):
        raise ValueError("Invalid image operation approval")
    name = specs[0].get("tool")
    if (
        name not in OPERATIONS
        or OPERATIONS[name] != grant["workflow"]
        or not authz.check(principal, name).allowed
    ):
        raise ValueError("Image operation is not authorized")
    return name, arguments(name, specs[0].get("args"))


def outcome(name, args, value):
    if name == "manage_gallery_images":
        return image_actions.outcome(value, args)
    if (
        not isinstance(value, dict)
        or value.get("beforeVersion") != args["version"]
        or type(value.get("changed")) is not bool
    ):
        raise ValueError("Unverified cover mutation")
    state = targets.Cover.model_validate(value.get("state")).model_dump()
    if (
        state["articleId"] != args["article_id"]
        or value.get("imageId") != args["image_id"]
        or state["coverUrl"] != args["image_url"]
        or state["fit"] != args["fit"]
        or value["changed"] != (state["version"] != args["version"])
    ):
        raise ValueError("Cover outcome differs from approval")
    return state


def _image_result(value):
    matched, changed = len(value["matchedIds"]), len(value["changedIds"])
    if value["action"] == "delete":
        return f"已从图库移除 {changed} 张图片，存储清理已排队，尚未核实物理文件清除。"
    return f"已核对 {matched} 张图片，更新 {changed} 张，其余已符合目标状态。"


class Operation:
    def __init__(self, name):
        self.name = name

    def invoke(self, args, config=None):
        check_cancelled(config)
        if self.name == "manage_gallery_images":
            method, path, payload = "POST", image_actions.PATH, args
        else:
            method = "PUT"
            path = f"{targets.PATH}/articles/{args['article_id']}/cover"
            payload = {
                "version": args["version"],
                "imageId": args["image_id"],
                "imageVersion": args["image_version"],
                "fit": args["fit"],
            }
        result = base._admin_request(method, path, payload, config)
        if isinstance(result, base.ToolResult):
            return result
        try:
            state = outcome(self.name, args, result)
        except (ValueError, TypeError, KeyError):
            return base.unavailable("请求已发出，但无法确认图片操作结果；请先查询，不要重复执行。")
        text = (_image_result(state) if self.name == "manage_gallery_images"
                else "文章封面已设置，正文和发布状态保持不变。")
        target = (tgt("note", args["article_id"]) if self.name == "set_article_cover"
                  else {"kind": "image", "ids": state["matchedIds"]})
        return ok(
            text,
            meta=fact(OPERATIONS[self.name], changed=result["changed"], target=target,
                      confirmed_args=args, outcome=result, state=state),
        )


def receipt(grant, name, args, result):
    if not isinstance(result, base.ToolResult) or result.kind != "ok":
        return None
    meta = result.meta
    if meta.get("op") != OPERATIONS[name] or meta.get("confirmed_args") != args:
        return None
    try:
        outcome(name, args, meta.get("outcome"))
    except (ValueError, TypeError, KeyError):
        return None
    return {
        "skill": grant["workflow"],
        "tool": name,
        "args": args,
        "result": str(result),
        "action": str(result),
        "ts": time.time(),
    }


def execute(grant, config, saver, *, tools=None, ledger=None):
    return writes.execute_confirmed(
        grant,
        config,
        saver,
        validate=validate,
        receipt=receipt,
        tool_factory=lambda name: {name: Operation(name)} if tools is None else tools,
        ledger=ledger,
    )
