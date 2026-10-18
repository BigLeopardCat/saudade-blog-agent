#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两个运行时可观测判定的**纯逻辑**：构建身份（`/version`）与就绪（`/ready`，20261018 批 3）。

**为什么是单独一个模块而不是写在 `server.py` 里**：`server.py` 已经 2549 行、
超文件预算 3 倍，而 `tests/test_module_budget.py` 的规矩是"长文件不许再长，真需要变大
就在同一个提交里 `--update` 并说明为什么"。这两个判定**不需要**住在 app 模块里——它们
是纯函数（输入 `agent` 与 `settings`，输出返回体），住的理由只有"路由函数顺手"。
路由留在 `server.py`（它必须在那儿，`app` 与 `_agent` 都在那儿），逻辑搬到这里。
同族的先例：`agent/factblock.py`、`agent/action_text.py`、`agent/entities.py`。

**这两个端点的分工**（别把它们读成一件事）：
  · `/version` 回答"**这是哪一版**"——静态构建身份，进程一生不变；
  · `/ready`   回答"**能不能接活**"——活体判定，随配置与启动阶段变；
  · `/health`（仍在 `server.py`）回答"**进程活没活**"，并且**刻意**对配置错误
    fail-open。三者不是彼此的子集：`/health` 绿而 `/ready` 红正是本模块要买的那个组合。
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


def version_payload(app_version: str) -> dict:
    """`/version` 的返回体。**只放静态构建身份，不回显活体档位**。

    `app_version` 由调用方给（`server._app_version()`）——不在这里重读一次
    `pyproject.toml`：那是第二个真源，而"版本号只有一个来源"这条纪律的判据已经钉在
    `tests/test_packaging.py`（`_app_version() == app.version`）。

    为什么不回显档位：`/health` 的 `dials` 是那些事实的**唯一**来源（它回的是**这个进程
    的内存取值**），这里再回一份就是第二个真源——本仓最贵的那类失效。想知道"现在跑
    哪一档"读 `/health`；想知道"这是哪一版"读这里。
    """
    sha, src = build_sha()
    return {"version": app_version, "sha": sha, "sha_source": src,
            "started_at": STARTED_AT, "python": platform.python_version()}


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
