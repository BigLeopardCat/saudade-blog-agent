"""Select supported runtimes and isolate their request-level evaluation reports."""

import os

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


def build_agent(arm: str | None = None):
    arm = arm or arm_name()
    if arm == ARM_NATIVE:
        from agent.runtime.native import build
        return build()
    raise RuntimeError(f"{ENV_ARM}={arm!r} 不支持；当前仅 native。")
