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

# 进程启动时刻（**import 期取一次**，不是每次请求现取）：`/version` 要回答的是"这一版
# 跑了多久"，不是"现在几点"。带时区偏移的本地钟面——本仓日志一律本地钟面（见
# CLAUDE.md《时区约定》），回 UTC 会让人对着 `logs/agent/agent.log` 做 8 小时换算。
STARTED_AT = datetime.now().astimezone().isoformat(timespec="seconds")


def build_sha(env: dict | None = None, git_dir: Path | None = None) -> tuple[str, str]:
    """构建身份里的那个 sha，**以及它是从哪来的**。

    取值顺序 `env → git → "unknown"`，**不许手抄**：假 sha 比没有更坏，它会让人以为
    两份对上了（同 `server._app_version` 那段"假版本号比没有版本号更坏"）。父仓那套
    「只信 build-info 的 sha」在这里对应的一格就是这个值。

    为什么把来源一起回（第二个返回值）：`"unknown"` 与一个真 sha 长得一样权威，只回
    sha 的话读的人分不出"这份部署包确实钉在这个提交上"与"这台机器上取不到"。三档：
    `env`（`DEPLOY_SHA` 设了——父仓 `deploy_from_r2.sh` 用的就是这个名字）/ `git`
    （仓里 `rev-parse HEAD`）/ `unknown`。

    `env` 与 `git_dir` 只给判据注入用（不传就是 `os.environ` 与本文件所在目录）。
    git 取不到**不是异常**：本仓可能被拷成一份没有 `.git` 的部署包，那时 `unknown`
    才是正确答案——所以这里吞掉 `OSError`/超时，不让它把 `/version` 打成 500。
    """
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


def arm_name(agent):
    from langgraph.graph.state import CompiledStateGraph

    from agent.runtime.hybrid import HybridArm
    from agent.runtime.native import NativeRuntime

    if isinstance(agent, NativeRuntime):
        return "native"
    if isinstance(agent, HybridArm):
        return "hybrid"
    return "graph" if isinstance(agent, CompiledStateGraph) else "unknown"


def request_context(agent):
    from config import settings

    return {"arm": arm_name(agent), "build_sha": BUILD_IDENTITY[0],
            "provider": settings.llm_provider, "model": settings.active_llm_model}


def version_payload(app_version: str) -> dict:
    """`/version` 的返回体。**只放静态构建身份，不回显活体档位**。

    `app_version` 由调用方给（`server._app_version()`）——不在这里重读一次
    `pyproject.toml`：那是第二个真源，而"版本号只有一个来源"这条纪律的判据已经钉在
    `tests/test_packaging.py`（`_app_version() == app.version`）。

    为什么不回显档位：`/health` 的 `dials` 是那些事实的**唯一**来源（它回的是**这个进程
    的内存取值**），这里再回一份就是第二个真源——本仓最贵的那类失效。想知道"现在跑
    哪一档"读 `/health`；想知道"这是哪一版"读这里。
    """
    sha, src = BUILD_IDENTITY
    return {"version": app_version, "sha": sha, "sha_source": src,
            "started_at": STARTED_AT, "python": platform.python_version()}


def _saver_of(agent) -> tuple[bool, object]:
    """从活体对象上找 saver，返回 `(读得出来吗, saver)`。

    **两个返回值不是啰嗦**：`getattr(x, "checkpointer", None)` 把"这个对象根本没有这个
    属性"与"属性就是 `None`"压成同一个结果，而这两件事要修的东西完全不同——前者说明
    我读错了对象（该回 `unknown`），后者才是"这个进程没挂 saver"（该回 `none`）。
    本仓在 `hostinfo.trace_stats` 那条 `readable` 上栽过同形的跟头（把"读不到"报成 0）。

    查找顺序 `checkpointer` → `saver` → 内层 `graph`：`CompiledStateGraph` 用前者
    （`langgraph.pregel.main.Pregel.__init__` 的 `self.checkpointer`），
    `agent/checkpoint_arm.py` 那类外壳用后者、并把图套在 `.graph` 里。只往里钻一层。

    ⚠️ **属性在场但是 `None` 不算"没挂"**：外壳完全可能自己写一个 `saver = None` 的占位，
    而真正落盘的那个在它套着的内层图上（`_Shell` 那一格就是照这个形状造的）。所以先
    一路找**非 None** 的那个，全找完才敢下结论——"没找到"与"找到的是 None"仍然分开。
    """
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
    """活体对象上那个 saver（没有 ⇒ `None`）。

    与 `checkpoint_backend` **同一取值实现**（同一个 `_saver_of`）：级联删除要删的必须是
    "图上真正在用的那个 saver"，而"图有没有挂 saver"这件事只此一处判过——各写一份
    就会在两处之间产生"health 说挂了、删除说没挂"这种谁也看不懂的分歧。
    """
    return _saver_of(agent)[1]


def holds_context(agent) -> bool:
    """**图自不自持上下文**——采纳（批 7）那三处（历史注入 / `thread_id` / 持久化档）
    的**唯一判据**，也是 `checkpoint_backend` 的布尔投影。

    `none` 与 `unknown` **都算"不自持"**：前者是"没挂 saver"，后者是"读不出来"
    （对象还没建好、或换了一个认不出的壳）。把"读不出来"当成"挂了"会让
    历史注入**静默消失**——那正是本仓最贵的一类失效（看着在跑、其实没跑），
    而反过来（把"读不出来"当"没挂"）最坏只是退回 20260903 以来的行为。

    **三处共用同一个函数是刻意的**：各判各的就会出现半接——同一段历史进图两份，
    且不报错（`agent/checkpoint.py` 头注）。
    """
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
    """`/health` 的 `checkpoint_db` 那一格：**这一格的 DSN 指到哪个库**。

    取值三档（与 `checkpoint_backend` 同一条纪律——"读不出来"与"没有"分开）：
      · `""`        —— 没配 DSN ⇒ 图是裸的，"指到哪个库"这件事不存在；
      · 库名        —— 配了，这是它指的库；
      · `"unknown"` —— 配了但解析不出库名（形如 `postgresql://host`，路径段是空的）。
        刻意**不**退回 `""`：把"写了个解析不出来的 DSN"报成"没配 DSN"就是
        `checkpoint_backend` 那条注里骂过的同形错误（把"取不到"报成"没有"）。

    ⚠️ **它是这一块里唯一一格读配置的**，而 `checkpoint_backend` 读的是**活体对象**
    ——两格不是一回事，合起来才读得通：`checkpoint_db=<某库>` + `checkpoint_backend=none`
    正是"配了 DSN 而图没挂上 saver"。**别只看这一格**就以为接上了。

    它存在的理由只有一个：把**"指错库"变成一眼可见**（业务库不该有 checkpoint 表，
    因为 saver 建表没有编号迁移 —— 见 `agent/checkpoint.py` 头注《DSN 指向哪个库》）。
    取值实现只有一处（`agent.checkpoint.db_name`），与 `CHECKPOINT_FORBID_DB`
    那条硬闸共用。

    **只回库名**：不回主机、不回用户名、不回口令 —— 与整块 dials 同一条纪律。
    """
    if not (dsn or "").strip():
        return ""
    # 懒 import：本模块被 `server` 与 `agent/checkpoint` **两边**引（后者的 `live_saver`
    # 反过来调本模块的 `saver_of`），模块级引回来会绕成一个环。
    from agent import checkpoint
    return checkpoint.db_name(dsn) or "unknown"


def ready_payload(agent, settings) -> tuple[int, dict]:
    """`/ready` 的 `(状态码, 返回体)`：**这个进程能不能真的接一轮对话**。

    与 `/health` 的"进程活没活"是两件事。`/health` 刻意对配置错误 fail-open（见
    `server.health` 的注：一个拼写错误不该被报成"agent 挂了"，那是**指错方向**的告警）。
    代价是 20260927 那次的形状至今没有出口——`.env` 里换了 key 之后新键四个端点一律
    401，而 `/health` 照旧 `agent_ready: true`：**进程活着、每一轮对话都错**。
    这个函数就是那个缺掉的第二问。

    三条硬依赖，任一不满足 ⇒ 503 + `reasons` 点名：
      · `agent_not_built` —— 图还没建起来（启动中 / 启动失败）；
      · `unknown_provider` —— `LLM_PROVIDER` 认不出。启动时它会**拒绝启动**（见
        `Settings.check_provider`），这里是给"运行期有人改了内存里的 settings"兜底，
        也让这一类在 `/health` 之外可见；
      · `missing_credentials` —— 名字认得、当前 provider 的 key 是空的。这一种进程能
        起来、`/health` 也绿，但每一条请求都会以 401 收场（词表与向量路的
        `degraded_reason()` 同一套）。
    `provider` 那一格：认得出就回**归一化后**的名字（"这一轮是谁在跑"），认不出就如实
    回 env 原文——不编一个已知的名字顶上，现场要读的恰恰是"你到底写成了什么"。

    ⚠️ **它现在是建议性的**：没有任何探针在读它（父仓 `scripts/healthcheck.sh` 读的是
    `/health`）。它把"能接活"变成一个**可被读的**事实，但**没有**接进任何自动通道——
    把它接进探针是父仓那侧的活。别把这一套读成"生产已经在按 /ready 摘流量了"。
    """
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
