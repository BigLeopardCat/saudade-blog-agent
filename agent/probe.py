#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行时可观测判定的**纯逻辑**：构建身份（`/version`）、就绪（`/ready`，20261018 批 3）、
以及 `/health` 那格 checkpoint 后端（20261018 批 6）。

**为什么是单独一个模块而不是写在 `server.py` 里**：`server.py` 已经 2549 行、
超文件预算 3 倍，而 `tests/test_module_budget.py` 的规矩是"长文件不许再长，真需要变大
就在同一个提交里 `--update` 并说明为什么"。这两个判定**不需要**住在 app 模块里——它们
是纯函数（输入 `agent` 与 `settings`，输出返回体），住的理由只有"路由函数顺手"。
路由留在 `server.py`（它必须在那儿，`app` 与 `_agent` 都在那儿），逻辑搬到这里。
同族的先例：`agent/factblock.py`、`agent/action_text.py`、`agent/entities.py`。

**三个判定的分工**（别把它们读成一件事）：
  · `/version` 回答"**这是哪一版**"——静态构建身份，进程一生不变；
  · `/ready`   回答"**能不能接活**"——活体判定，随配置与启动阶段变；
  · `/health`（仍在 `server.py`）回答"**进程活没活**"，并且**刻意**对配置错误
    fail-open。三者不是彼此的子集：`/health` 绿而 `/ready` 红正是本模块要买的那个组合。
  · `/health` 的 `dials.checkpoint_backend`（`checkpoint_backend`）：这一格**只**回答
    "这个进程挂没挂 saver"，它与前两个都不同族——`/ready` 的三条硬依赖里没有它
    （没挂 saver 的进程完全能接活，今天生产就是），所以它不是"就绪"的一部分，
    是**采纳进度**的可读出口。
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


#: saver 的类住在哪个模块 → 后端短名。**只看模块路径，不 import 那些包**：产线 `.venv`
#: 今天一个 checkpoint 后端都没装（见 `agent/checkpoint.py` 的《可验证面》），为了回一个
#: 名字去 import 它，会把"没装"变成"读不出来"——而这两种要修的东西不一样。
#: 前缀匹配（不是相等）：异步档住在 `langgraph.checkpoint.postgres.aio` 这种子模块里。
_CHECKPOINT_BACKENDS = (
    ("langgraph.checkpoint.postgres", "postgres"),
    ("langgraph.checkpoint.sqlite", "sqlite"),
    ("langgraph.checkpoint.memory", "memory"),
)


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

    `none` 与 `unknown` **都算"不自持"**：前者是"没挂 saver"（今天生产），后者是
    "读不出来"（对象还没建好、或换了一个认不出的壳）。把"读不出来"当成"挂了"会让
    历史注入**静默消失**——那正是本仓最贵的一类失效（看着在跑、其实没跑），
    而反过来（把"读不出来"当"没挂"）最坏只是退回 20260903 以来的行为。

    **三处共用同一个函数是刻意的**：各判各的就会出现半接——同一段历史进图两份，
    且不报错（`agent/checkpoint.py` 头注）。
    """
    return checkpoint_backend(agent) not in ("none", "unknown")


def checkpoint_backend(agent) -> str:
    """`/health` 的 `checkpoint_backend` 那一格：**这个进程到底挂没挂 saver**。

    **为什么必须从活体对象读、不许从 `.env` 推**：`CHECKPOINT_DSN` 填了**不等于**
    图挂上了 saver——采纳（20261018 批 7）之后 `build_checkpointer()` 才第一次有调用方，
    而它返回 `None` 的路径有两条（没配 DSN、以及将来任何一处"配了但没接上"）。
    任何"读配置再回一个档位名"的实现，都会把"配了 DSN"报成"挂上了 saver"，
    而那正是本仓最贵的一类失效（看着在跑、其实没跑）。这里读的是 `server._agent`
    这个对象本身，所以它回的是**事实**。

    取值五档（前四档是"读得出来"，第五档是"读不出来"）：
      · `"none"`      —— 对象在、但它没挂 saver（**今天生产恒这一档**）；
      · `"postgres"` / `"sqlite"` / `"memory"` —— 挂了，回后端短名。`memory` 这一档
        在 4 个 uvicorn worker 下**每 worker 各记各的且不报错**，所以它出现在这里不是
        噪声：它是"这个进程挂了 saver 但挂了错的那种"唯一的出口；
      · `"unknown"`   —— **读不出来**（图还没建起来 / 认不出这个对象）。刻意**不**退回
        `"none"`：把"取不到"报成"没挂"就是上面那条 `readable` 的同形错误。
      · 认不出的 saver 类 ⇒ 回它的**模块路径原文**（不编一个已知名字顶上——现场要读的
        恰恰是"你到底挂的是什么"）。
    """
    if agent is None:
        return "unknown"
    found, saver = _saver_of(agent)
    if not found:
        return "unknown"
    if saver is None:
        return "none"
    mod = type(saver).__module__
    for prefix, name in _CHECKPOINT_BACKENDS:
        if mod == prefix or mod.startswith(prefix + "."):
            return name
    return mod


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
