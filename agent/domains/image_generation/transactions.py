"""Short approval operations start durable host jobs; paid calls run outside the chat turn."""

import time

from pydantic import BaseModel, ConfigDict, Field, StrictStr
from typing_extensions import Annotated

from agent import authz
from agent.domains.image_generation.tools import PATH
from agent.runtime import writes
from tools import base

OPERATIONS = {
    "start_image_generation": "image_generation",
    "accept_generated_image": "image_acceptance",
}
PERMISSION_OPERATIONS = frozenset(OPERATIONS)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f-]{36}$")]
    version: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


def handles(grant):
    return (
        isinstance(grant, dict)
        and grant.get("executor") == "image_generation"
        and grant.get("workflow") in OPERATIONS.values()
    )


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if (
        not handles(grant)
        or not isinstance(specs, list)
        or len(specs) != 1
        or not isinstance(specs[0], dict)
    ):
        raise ValueError("Invalid image approval")
    name = specs[0].get("tool")
    if (
        name not in OPERATIONS
        or OPERATIONS[name] != grant["workflow"]
        or not authz.check(principal, name).allowed
    ):
        raise ValueError("Image operation not authorized")
    return name, Arguments.model_validate(specs[0].get("args")).model_dump()


def verified(name, args, result):
    if not isinstance(result, dict) or result.get("id") != args["job_id"]:
        return False
    if name == "start_image_generation":
        return result.get("requestHash") == args["version"] and result.get("status") in {
            "generating",
            "review",
            "accepted",
        }
    return (
        result.get("outputHash") == args["version"]
        and result.get("status") == "accepted"
        and type(result.get("imageId")) is int
        and result["imageId"] > 0
    )


class Operation:
    def __init__(self, name):
        self.name = name

    def invoke(self, args, config=None):
        action = "approve" if self.name == "start_image_generation" else "accept"
        value = base._admin_request(
            "POST", f"{PATH}/jobs/{args['job_id']}/{action}", {"version": args["version"]}, config
        )
        if isinstance(value, base.ToolResult):
            return value
        if not verified(self.name, args, value):
            return base.unavailable("生图操作结果尚未核实，请先查询任务状态，不要重复调用。")
        text = (
            "生图任务已开始。完成后可在后台图库的 AI 生图中审阅，确认前不会入库。"
            if action == "approve"
            else f"生成结果已存入图库，图片编号 {value['imageId']}。"
        )
        return base.ok(
            text,
            meta={
                "image_job": {
                    key: value.get(key)
                    for key in ("id", "status", "requestHash", "outputHash", "imageId")
                },
                "approved_args": args,
            },
        )


def receipt(grant, name, args, result):
    if (
        not isinstance(result, base.ToolResult)
        or result.kind != "ok"
        or result.meta.get("approved_args") != args
    ):
        return None
    value = result.meta.get("image_job")
    if not verified(name, args, value):
        return None
    return {
        "skill": grant["workflow"],
        "tool": name,
        "args": args,
        "result": str(result),
        "action": str(result),
        "ts": time.time(),
        "job_id": args["job_id"],
    }


def execute(grant, config, saver, *, ledger=None):
    return writes.execute_confirmed(
        grant,
        config,
        saver,
        validate=validate,
        receipt=receipt,
        tool_factory=lambda name: {name: Operation(name)},
        ledger=ledger,
    )
