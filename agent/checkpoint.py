"""checkpointer 的**接线点**（20261017 重写；此前是本模块里那个 `MemorySaver` 存根）。

## 今天恒惰性

`build_checkpointer()` 在没给 DSN 时返回 `None`；图那侧（`agent/graph.py::build_graph`）
**收**这个参数并原样传给 `g.compile(checkpointer=...)`，但它**缺省 `None`**，而生产调用点
（`server.py` → `agent.create_agent()`）**不传** ⇒ 全仓没有任何一处调用本函数，生产行为与
以前逐字节相同（这条"只传不造、缺省 None"由 `tests/test_checkpoint_factory.py` 的接线锁
钉着）。本模块只做一件事：把"接上 saver 那天该在哪儿接"从散落的注释收成一个**有名字、
有判据**的函数。

## 为什么原来的存根必须换掉（不是"不好看"）

它 `return MemorySaver()`，而 `MemorySaver` 是**进程内**的——生产是 4 个 uvicorn worker，
照它接线的人拿到的会是一个"看着能用、实际每 worker 各记各的"上下文，**而且不报错**。
更坏的是它的英文 docstring 写着 "maintain context across multiple turns in a session"，
与 `docs/agent-architecture.md` §4.6 明说的"它从未积累过任何状态"**正相反**。
一个返回**错**对象的存根比没有这个函数更危险：没有它，人会去读 §4.6；有它，人会直接用。

## 接线（= ADR-0011 采纳那一刻）是**三处一起动**，缺一不可

  ① `server._build_messages` 删掉历史注入循环——不删 ⇒ 图自持的上下文与注入的**重复**；
  ② `server.py` 的 `thread_id` 从"每请求随机"改成会话坐标（`conv_<conversation_id>`）——
     不改 ⇒ saver 永远命中不到同一个线程，等于没挂；
  ③ `build_graph(checkpointer=build_checkpointer(dsn))`。

**只做 ③ 会静默产生双份上下文**。所以本模块刻意**不提供**"拨一下开关就生效"的路径：
非空 DSN 今天一律**抛异常**，绝不回落成内存档。依赖装齐、三处同批改完之前，
这个函数的存在意义是**让半成品接法炸得响**，而不是让它悄悄跑起来。

## 接上之后第一件事是查 `durability`

`langgraph/pregel/main.py` 的缺省是 `"async"`（checkpoint 写交给后台线程、主循环**不等**）。
进程被**硬杀**时上一步可能没落盘 ⇒ 恢复点前移 ⇒ 重放那个 superstep 拿到的 `task_id`
与崩前那次不同 ⇒ `agent/idem.py` 那本幂等账的键变了、**账被静默绕过**。
⇒ 接线时必须显式 `compile(..., durability="sync")`（见 ADR-0012 的《触发条件》）。

## 可验证面（别过度承诺）

`langgraph.checkpoint.postgres` / `sqlite` **本机都还没装**（20261017 实测 `.venv` 里只有
`memory`）。所以今天能判的是三条：**没给 DSN ⇒ None**、**scheme 认不出来 ⇒ 响亮**、
**给了 DSN 而依赖没装 ⇒ 响亮且点名包名**。真造 saver 那一段属于 ADR-0011 采纳批，
依赖落地后与它的判据一起补——**本文件不写没跑过的 saver 构造代码**。
"""

import importlib

# 只认这两种 scheme。**故意不认 `memory` / `memory://`**：内存 saver 在 4 个 worker 下
# 每 worker 各记各的，而它**不报错**——本仓最贵的失效模式就是"看着在跑、其实没跑"。
# scheme → (模块路径, 包名)
_BACKENDS: dict[str, tuple[str, str]] = {
    "postgresql": ("langgraph.checkpoint.postgres", "langgraph-checkpoint-postgres"),
    "postgres": ("langgraph.checkpoint.postgres", "langgraph-checkpoint-postgres"),
    "sqlite": ("langgraph.checkpoint.sqlite", "langgraph-checkpoint-sqlite"),
}

_KNOWN = "postgresql:// / postgres:// / sqlite://"


def backend_for(dsn: str) -> tuple[str, str]:
    """DSN → `(模块路径, 包名)`。认不出来**响亮报错**：不猜、不回落。"""
    head = dsn.split("://", 1)[0].strip().lower() if "://" in dsn else ""
    scheme = head.split("+", 1)[0]      # `postgresql+psycopg://` 这种带驱动的也认
    if scheme not in _BACKENDS:
        raise RuntimeError(
            f"checkpoint DSN 的 scheme 认不出来：{head or dsn!r}。只认 {_KNOWN}。"
            "**故意不支持内存档**：它在多 worker 下每 worker 各记各的，且不报错。")
    return _BACKENDS[scheme]


def build_checkpointer(dsn: str = "") -> object | None:
    """空 DSN ⇒ `None`（= 今天的行为）；非空 ⇒ 真 saver。

    非空这一支今天**一定抛异常**，三条分支各有各的话要说（见模块头注的"可验证面"）：
    依赖没装时不静默降级；依赖装齐了也不许只改这一处——那会产生双份上下文。
    """
    if not dsn.strip():
        return None

    mod_path, pkg = backend_for(dsn)
    try:
        importlib.import_module(mod_path)
    except ImportError as e:
        raise RuntimeError(
            f"配了 checkpoint DSN 但后端没装（{pkg}）：{e}。"
            f"装它：`.venv/bin/pip install {pkg}`（见 ADR-0011）。"
            "**不会静默回落成内存档**——那会让 4 个 worker 各记各的上下文而不报错。"
        ) from e

    raise RuntimeError(
        f"{pkg} 已装（{mod_path}），但**造 saver 这一段还没实现**：它属于 ADR-0011 "
        "采纳批，必须与 ①`server._build_messages` 删注入循环、②`thread_id` 改会话坐标 "
        "**同一个提交**落地（只做本处会静默产生双份上下文），并显式给 "
        '`compile(..., durability="sync")`。理由见本模块头注与 ADR-0012《触发条件》。')
