#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime identity, readiness and persistence diagnostics.

Build identity is captured when the process imports this module. Health checks
inspect the live runtime; a later git update must not change the reported build.
"""

from __future__ import annotations

import os
import platform
import subprocess
from datetime import datetime
from pathlib import Path

# Captured once so build identity does not follow subsequent checkout changes.
STARTED_AT = datetime.now().astimezone().isoformat(timespec="seconds")


def build_sha(env: dict | None = None, git_dir: Path | None = None) -> tuple[str, str]:
    """Return the loaded build SHA and its source (environment, Git or unknown)."""
    e = os.environ if env is None else env
    got = (e.get("DEPLOY_SHA") or "").strip()
    if got:
        return got, "env"
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"],
                           cwd=str(git_dir or Path(__file__).resolve().parent),
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "unknown", "unknown"
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip(), "git"
    return "unknown", "unknown"


BUILD_IDENTITY = build_sha()


def runtime_name(agent):
    from agent.runtime.native import NativeRuntime

    return "native" if isinstance(agent, NativeRuntime) else "unknown"


def request_context(agent):
    from config import settings

    return {"name": runtime_name(agent), "build_sha": BUILD_IDENTITY[0],
            "provider": settings.llm_provider, "model": settings.active_llm_model,
            "reasoning_effort": settings.agent_reasoning_effort}


def version_payload(app_version: str) -> dict:
    """Return static process identity without rereading changed source files."""
    sha, src = BUILD_IDENTITY
    return {"version": app_version, "sha": sha, "sha_source": src,
            "started_at": STARTED_AT, "python": platform.python_version()}


def _saver_of(agent) -> tuple[bool, object]:
    """Inspect saver-bearing objects without treating an unknown shape as disabled."""
    found = False
    inner = getattr(agent, "graph", None)
    if inner is agent:              # 自引用 ⇒ 别把自己当"内层"再看一遍
        inner = None
    for obj in (agent, inner):
        if obj is None:
            continue
        for attr in ("checkpointer", "saver"):
            if hasattr(obj, attr):
                found = True
                v = getattr(obj, attr)
                if v is not None:
                    return True, v
    return found, None


def saver_of(agent) -> object | None:
    """Return the same live saver used by persistence diagnostics and deletion."""
    return _saver_of(agent)[1]


def holds_context(agent) -> bool:
    """Whether the live runtime owns a configured conversation saver."""
    return checkpoint_backend(agent) not in ("none", "unknown")


def checkpoint_backend(agent) -> str:
    """Report the live saver backend through official framework types."""
    if agent is None:
        return "unknown"
    found, saver = _saver_of(agent)
    if not found:
        return "unknown"
    if saver is None:
        return "none"
    from agent.runtime.checkpoint_store import backend

    return backend(saver)


def checkpoint_db(dsn: str) -> str:
    """Return only the configured database name, never connection credentials."""
    if not (dsn or "").strip():
        return ""
    # Import lazily because checkpoint helpers also use this module.
    from agent import checkpoint
    return checkpoint.db_name(dsn) or "unknown"


def ready_payload(agent, settings) -> tuple[int, dict]:
    """Check runtime construction and credentials without a paid model request."""
    reasons: list[str] = []
    if agent is None:
        reasons.append("agent_not_built")
    try:
        prov = settings.check_provider()
    except Exception:                               # noqa: BLE001
        # 认不出 ⇒ 回 env 原文，**不**在这里 raise：这个端点存在的意义就是让这一类在
        # `/health` 之外**看得见**，它自己再炸掉就白加了（`/health` 那处同款取向）。
        prov = getattr(settings, "llm_provider", "")
        reasons.append("unknown_provider")
    else:
        # 只在认得出时才问凭据：`is_api_key_configured` 按 provider 前缀 `getattr`，
        # 认不出的名字会让它抛 AttributeError（`Settings._provider_prefix` 那段注里的
        # 同形坑：校验放行了、取值却取不到）。
        if not settings.is_api_key_configured:
            reasons.append("missing_credentials")
    return (200 if not reasons else 503), {"ready": not reasons, "reasons": reasons,
                                           "provider": prov}
