"""Confirmed scheduling operations share the existing durable execution ledger."""

import time

from agent import authz
from agent.runtime import writes
from agent.scheduling.models import ControlArguments, CreateArguments
from tools.result import ok

OPERATIONS = {
    "create": "create_automation",
    "pause": "pause_automation",
    "resume": "resume_automation",
    "cancel": "cancel_automation",
}
PERMISSION_OPERATIONS = frozenset(OPERATIONS.values())


def handles(grant):
    return (
        isinstance(grant, dict)
        and grant.get("executor") == "automations"
        and grant.get("workflow") in {"automation_" + key for key in OPERATIONS}
    )


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if not handles(grant) or not isinstance(specs, list) or len(specs) != 1:
        raise ValueError("invalid scheduling confirmation")
    action = grant["workflow"].removeprefix("automation_")
    name = OPERATIONS[action]
    if specs[0].get("tool") != name or not authz.check(principal, name).allowed:
        raise ValueError("scheduling operation is not authorized")
    model = CreateArguments if action == "create" else ControlArguments
    args = model.model_validate(specs[0].get("args")).model_dump(mode="json")
    if action != "create" and args["action"] != action:
        raise ValueError("scheduling action differs from the approved operation")
    return name, args


class Operation:
    def __init__(self, service, action):
        self.service, self.action = service, action

    def invoke(self, args, config=None):
        principal, _ = writes.bound_context(config)
        if self.action == "create":
            result = self.service.create(principal, args["schedule_id"], args["definition"])
        else:
            result = self.service.control(
                principal, args["schedule_id"], self.action, args["version"]
            )
        return ok("计划任务已保存。", meta={"schedule_result": result, "approved_args": args})


def _receipt(grant, name, args, result):
    meta = getattr(result, "meta", {})
    state = meta.get("schedule_result", {})
    row = state.get("schedule", {})
    if (
        meta.get("approved_args") != args
        or row.get("id") != args["schedule_id"]
        or type(state.get("changed")) is not bool
    ):
        return None
    return {
        "skill": grant["workflow"],
        "tool": name,
        "args": args,
        "result": "计划任务已保存。",
        "ts": time.time(),
        "schedule": row,
        "changed": state["changed"],
    }


class BoundDomain:
    validate = staticmethod(validate)

    def __init__(self, service):
        self.service = service

    def execute(self, grant, config, saver):
        action = grant["workflow"].removeprefix("automation_")
        name = OPERATIONS[action]
        return writes.execute_confirmed(
            grant,
            config,
            saver,
            validate=validate,
            receipt=_receipt,
            tool_factory=lambda _: {name: Operation(self.service, action)},
        )
