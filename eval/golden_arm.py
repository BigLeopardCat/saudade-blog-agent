"""Select supported runtimes and isolate their request-level evaluation reports."""

import os
from contextlib import ExitStack

ARM_NATIVE = "native"
KNOWN_ARMS = (ARM_NATIVE,)
ENV_ARM = "GOLDEN_ARM"
INTERFACE_LAYER = "native"


def arm_name() -> str:
    name = (os.environ.get(ENV_ARM) or "").strip().lower() or ARM_NATIVE
    if name not in KNOWN_ARMS:
        raise RuntimeError(f"{ENV_ARM}={name!r} 不支持；当前仅 native。")
    return name


def engine_for(arm: str) -> str:
    if arm != ARM_NATIVE:
        raise RuntimeError(f"不支持已退役运行时：{arm}")
    return INTERFACE_LAYER


def reports_dir(arm: str) -> str:
    if arm != ARM_NATIVE:
        raise RuntimeError(f"不支持已退役运行时：{arm}")
    return "eval/report/runs"


def is_baseline_arm(arm: str) -> bool:
    return arm == ARM_NATIVE


def build_agent(arm: str | None = None, *, resources: ExitStack):
    arm = arm or arm_name()
    if arm == ARM_NATIVE:
        from agent.runtime.native import build
        return build(resources=resources)
    raise RuntimeError(f"{ENV_ARM}={arm!r} 不支持；当前仅 native。")
