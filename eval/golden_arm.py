"""Select supported runtimes and isolate their request-level evaluation reports."""

import os

ARM_GRAPH = "graph"
ARM_HYBRID = "hybrid"
KNOWN_ARMS = (ARM_GRAPH, ARM_HYBRID)
ENV_ARM = "GOLDEN_ARM"
INTERFACE_LAYER = "native"


def arm_name() -> str:
    name = (os.environ.get(ENV_ARM) or "").strip().lower() or ARM_GRAPH
    if name not in KNOWN_ARMS:
        raise RuntimeError(f"{ENV_ARM}={name!r} 不支持；当前仅 graph/hybrid，旧实验见归档分支。")
    return name


def engine_for(arm: str) -> str:
    return INTERFACE_LAYER if arm == ARM_GRAPH else f"{INTERFACE_LAYER}+{arm}"


def reports_dir(arm: str) -> str:
    # Historical report names remain readable; only build_agent controls executable runtimes.
    return "eval/report/runs" if arm == ARM_GRAPH else f"eval/report/runs_{arm}"


def is_baseline_arm(arm: str) -> bool:
    return arm == ARM_GRAPH


def build_agent(arm: str | None = None):
    arm = arm or arm_name()
    if arm == ARM_GRAPH:
        from agent.agent import create_agent
        return create_agent()
    if arm == ARM_HYBRID:
        from agent.runtime.hybrid import build
        return build()
    raise RuntimeError(f"{ENV_ARM}={arm!r} 不支持；当前仅 graph/hybrid，旧实验见归档分支。")
