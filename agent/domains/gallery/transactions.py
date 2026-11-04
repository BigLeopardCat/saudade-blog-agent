"""Use the standard approval ledger and authoritative Rust mutation responses."""

import time

from agent import authz
from agent.domains.gallery import targets
from agent.runtime import writes
from tools import base
from tools.result import fact, ok, tgt

OPERATIONS = {"update_gallery_image": "gallery_metadata", "set_article_cover": "article_cover"}
PERMISSION_OPERATIONS = frozenset(OPERATIONS)


def handles(grant):
    return (
        isinstance(grant, dict)
        and grant.get("executor") == "gallery"
        and grant.get("workflow") in OPERATIONS.values()
    )


def arguments(name, args):
    model = targets.MetadataArguments if name == "update_gallery_image" else targets.CoverArguments
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
    if (
        not isinstance(value, dict)
        or value.get("beforeVersion") != args["version"]
        or type(value.get("changed")) is not bool
    ):
        raise ValueError("Unverified image mutation")
    if name == "update_gallery_image":
        state = targets.Image.model_validate(value.get("state")).model_dump()
        if state["imageKey"] != args["image_id"] or any(
            state[key] != val for key, val in args["changes"].items()
        ):
            raise ValueError("Metadata outcome differs from approval")
    else:
        state = targets.Cover.model_validate(value.get("state")).model_dump()
        if (
            state["articleId"] != args["article_id"]
            or value.get("imageId") != args["image_id"]
            or state["coverUrl"] != args["image_url"]
            or state["fit"] != args["fit"]
        ):
            raise ValueError("Cover outcome differs from approval")
    if value["changed"] != (state["version"] != args["version"]):
        raise ValueError("Mutation version does not prove its outcome")
    return state


class Operation:
    def __init__(self, name):
        self.name = name

    def invoke(self, args, config=None):
        if self.name == "update_gallery_image":
            path = f"{targets.PATH}/images/{args['image_id']}"
            payload = {"version": args["version"], **args["changes"]}
        else:
            path = f"{targets.PATH}/articles/{args['article_id']}/cover"
            payload = {
                "version": args["version"],
                "imageId": args["image_id"],
                "imageVersion": args["image_version"],
                "fit": args["fit"],
            }
        result = base._admin_request("PUT", path, payload, config)
        if isinstance(result, base.ToolResult):
            return result
        try:
            state = outcome(self.name, args, result)
        except (ValueError, TypeError, KeyError):
            return base.unavailable("请求已发出，但无法确认图片操作结果；请先查询，不要重复执行。")
        text = (
            "图库信息已更新。"
            if self.name == "update_gallery_image"
            else "文章封面已设置，正文和发布状态保持不变。"
        )
        return ok(
            text,
            meta=fact(
                OPERATIONS[self.name],
                changed=result["changed"],
                target=tgt(
                    "note" if self.name == "set_article_cover" else "image",
                    args.get("article_id", args["image_id"]),
                ),
                confirmed_args=args,
                outcome=result,
                state=state,
            ),
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
