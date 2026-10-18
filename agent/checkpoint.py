"""checkpointer 的**接线点**与**级联清理**（20261017 重写；20261018 批 7 落地采纳）。

## 现在真接了（ADR-0011 采纳，20261018）

`build_checkpointer()` 读 `settings.checkpoint_dsn`（缺省空）：**空 ⇒ `None`**
（图不挂 saver，行为与 20260903 以来逐字节相同）；**非空 ⇒ 造 PG saver 并建表**，
由 `server._build_arm()` 交给 `create_agent(checkpointer=...)`。

## 采纳是**三处一起动**，缺一不可

  ① `server._build_messages` 的**历史注入循环**要停——不停 ⇒ 图自持的上下文与注入的
     `req.history[-20:]` **两份都在**，第二轮起重复；
  ② `server.py` 的 `thread_id` 要从"每请求随机"改成**会话坐标**（`conv_<conversation_id>`）
     ——不改 ⇒ saver 永远命中不到同一条线程，等于没挂；
  ③ 本模块造的 saver 交给图（`server._build_arm`）。

**只做 ③ 会静默产生双份上下文**，而此前没有任何判据会红。所以三处共用**同一个判据**
`server._graph_holds_context()`——它读的是**活体对象上有没有 saver**（`agent/probe.py`），
不是配置。于是"配了 DSN 但没挂上"与"没配 DSN"在源码形状上是**同一个形态**，
**半接在结构上不可能**（`tests/test_checkpoint_adoption.py` 钉着这件事）。

## 持久化档：`durability="sync"`，不是性能选项

`langgraph/pregel/main.py` 的缺省是 `"async"`（checkpoint 写交给后台线程、主循环**不等**）。
进程被**硬杀**时上一步可能没落盘 ⇒ 恢复点前移 ⇒ 重放那个 superstep 拿到的 `task_id`
与崩前那次不同 ⇒ `agent/idem.py` 那本幂等账的键变了、**账被静默绕过**。
⚠️ **它是 `stream()` 的参数，不是 `compile()` 的参数**（langgraph 1.2.7 实测：
`StateGraph.compile` 签名里没有它）——20261017 的头注在这一点上写错了，已改。
真正的不变式是"**凡有 saver 在场的那次调用必须显式给同步档**"。

## 建表（`setup()`）为什么加了一把咨询锁

`saver.setup()` 是幂等的，但**不是并发安全的**：它读一次版本号再逐条跑迁移，而每条迁移
末尾都 `INSERT INTO checkpoint_migrations (v)`（**没有 `ON CONFLICT`**）。产线是
`--workers 4`，四个进程同时启动 ⇒ 两个都读到 `version=-1` ⇒ 后到的那个**主键冲突、
启动崩**。所以建表包在 `pg_advisory_lock` 里（同一把钥匙，先到者建完、后到者看到版本号
已经是 9，直接跳过）。**这是"多 worker 同时启动"这一件事的补丁，不是通用迁移方案。**

## 级联清理（`delete_thread`）

删会话要清掉那个会话的 checkpoint——不清就是**留着别人的对话正文在库里、且没有任何
读取路径能删它**（同 ADR-0011 的级联清理一节）。机制住本模块（`saver.delete_thread`
是 langgraph 的公开 API，表名与结构不进本仓），HTTP 面住 `server.py` 的
`DELETE /checkpoint/{conversation_id}`，**删除动作在父仓** `src/routes/conversation.rs`
（跨仓契约，`tests/test_checkpoint_cascade.py` 有一条父仓守卫盯着）。

## 可验证面（别过度承诺）

**没有真 PG 可连**，所以判据分三层：**纯函数**（`conversation_thread` 的负例、
`redact` 不泄口令、`backend_for` 认不出的响亮）在 `tests/test_checkpoint_factory.py`；
**接线形状**（三处共用同一个判据）在 `tests/test_checkpoint_adoption.py`；
**级联**（no-op 删除要红）在 `tests/test_checkpoint_cascade.py`。
**真连上库之后才成立的那几条不在这三份里**——它们要的是产线上跑一轮对话再读
`/health` 的 `checkpoint_backend`，见本批交接的运维步骤。
"""

import importlib
import logging
import uuid

from config import settings

logger = logging.getLogger(__name__)

# 只认这两种 scheme。**故意不认 `memory` / `memory://`**：内存 saver 在 4 个 worker 下
# 每 worker 各记各的，而它**不报错**——本仓最贵的失效模式就是"看着在跑、其实没跑"。
# scheme → (模块路径, 包名)
_BACKENDS: dict[str, tuple[str, str]] = {
    "postgresql": ("langgraph.checkpoint.postgres", "langgraph-checkpoint-postgres"),
    "postgres": ("langgraph.checkpoint.postgres", "langgraph-checkpoint-postgres"),
    "sqlite": ("langgraph.checkpoint.sqlite", "langgraph-checkpoint-sqlite"),
}

_KNOWN = "postgresql:// / postgres://"
_PG_MOD = "langgraph.checkpoint.postgres"

# 连接池与建表锁。**`_POOL_WAIT` 是个模块常量而不是字面量**：判据要在"连不上"这一支上
# 断言"响亮抛、绝不返回一个坏 saver"，而默认的 10 秒会让那条判据跑成一次真等待。
_POOL_MAX = 4
_POOL_WAIT = 10
_SETUP_LOCK = 0x5A0DA6E          # 咨询锁的钥匙（任意常量，只要全仓唯一）

_pool = None
_pool_dsn = None


def backend_for(dsn: str) -> tuple[str, str]:
    """DSN → `(模块路径, 包名)`。认不出来**响亮报错**：不猜、不回落。"""
    head = dsn.split("://", 1)[0].strip().lower() if "://" in dsn else ""
    scheme = head.split("+", 1)[0]      # `postgresql+psycopg://` 这种带驱动的也认
    if scheme not in _BACKENDS:
        raise RuntimeError(
            f"checkpoint DSN 的 scheme 认不出来：{head or dsn!r}。只认 {_KNOWN}。"
            "**故意不支持内存档**：它在多 worker 下每 worker 各记各的，且不报错。")
    return _BACKENDS[scheme]


def redact(dsn: str) -> str:
    """DSN 去掉口令，只留 `scheme://***@host/db`。

    **异常文本会进日志与 trace**（`build_checkpointer` 的报错就是这么用的），而 DSN 里
    通常带着库口令——不脱敏等于把口令写进 `logs/agent/agent.log`。没带 `@` 的原样返回。
    """
    if "@" not in dsn:
        return dsn
    head, tail = dsn.split("@", 1)
    scheme = head.split("://", 1)[0] if "://" in head else ""
    return f"{scheme}://***@{tail}" if scheme else f"***@{tail}"


# ── 会话坐标 ─────────────────────────────────────────────────────────
CONV_PREFIX = "conv_"


def conversation_thread(conversation_id: object) -> str | None:
    """会话 id → `thread_id`；**没有会话身份时返回 `None`**。

    调用方（`server._thread_id_for`）拿到 `None` 就退回"每请求随机"。

    ⚠️ **绝不把 `None`/`0`/空串拼成一个 `conv_None`**：那会让所有"没有会话"的请求
    （新会话还没落库、请求体缺这一格、评测直连）**共用同一条线程** ⇒ 图把别人的历史
    当自己的 —— 那是**跨用户上下文串台**，比"没挂 saver"坏得多，而且不报错。
    缺席的三种形态（`None` / `""` / `"0"`）与 `agent/checkpoint_arm.py::_thread_for`
    同一把尺子（那边是实验臂，这是生产）。
    """
    cid = "" if conversation_id is None else str(conversation_id).strip()
    if cid in ("", "0", "None"):
        return None
    return f"{CONV_PREFIX}{cid}"


def thread_id_for(user_id: int, conversation_id: object, holds_context: bool) -> str:
    """这一轮的 `thread_id`（`server._thread_id_for` 的纯函数本体）。

    **自持上下文且会话坐标在场** ⇒ `conv_<conversation_id>`：会话内复用同一条线程
    （这是挂 saver 的**全部意义**）。**否则** ⇒ 每请求随机（`user_<uid>_<8位>`，
    = 20260903 以来的行为）：连续性由请求体里的 DB 历史承担。

    会话 id 缺席时**刻意退回随机**而不是编一个——编一个（比如按 uid 拼）会让不同会话、
    乃至刚建的空会话共用线程，也就是跨会话串台。

    `holds_context` 由调用方传（`probe.holds_context(server._agent)`）：本模块**不读**
    `server._agent`，否则 `agent → server` 就是一个反向 import，而那个方向的环在本仓
    有前科（`eval/trace_files.py` 那条注）。判据的**取值**仍只有一处实现。
    """
    if holds_context:
        tid = conversation_thread(conversation_id)
        if tid:
            return tid
    return f"user_{user_id}_{uuid.uuid4().hex[:8]}"


# ── 连接池 ───────────────────────────────────────────────────────────
def _make_pool(dsn: str):
    """PG 连接池。参数照 `agent/checkpoint_arm.py::_make_pool`——那是 `PostgresSaver`
    要求的形状：`autocommit`（它自己管事务）+ `prepare_threshold=0`（避免 prepared
    statement 在 PgBouncer 那类中间件后面失效）+ `dict_row`（迁移代码按列名取值）。

    **连不上就响亮抛，绝不返回一个坏 saver**：坏 saver 的失效时刻是"第一轮对话的第一次
    checkpoint"，那时已经没有任何东西指向配置了。这里抛的话，是**启动失败**。
    """
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=dsn, min_size=1, max_size=_POOL_MAX, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    try:
        pool.open(wait=True, timeout=_POOL_WAIT)
    except Exception as e:                                  # 连不上/超时/认证失败
        pool.close()
        raise RuntimeError(
            f"checkpoint DSN 连不上（{redact(dsn)}）：{type(e).__name__}: {e}。"
            "**不静默降级**：挂一个连不上的 saver 会把故障推迟到第一轮对话的第一次"
            "写 checkpoint，而那时报的错与配置没有任何可读的联系。") from e
    return pool


def _pool_for(dsn: str):
    """进程级单例池。DSN 换了就重建（判据会拨 DSN，不重建会拿到上一个库的池）。"""
    global _pool, _pool_dsn
    if _pool is None or _pool_dsn != dsn:
        if _pool is not None:
            _pool.close()
        _pool = _make_pool(dsn)
        _pool_dsn = dsn
    return _pool


def _setup_under_lock(saver) -> None:
    """在咨询锁里建表。**为什么不是直接 `saver.setup()`**：见模块头注《建表》一节
    ——四个 worker 同时启动时，后到的那条 `INSERT INTO checkpoint_migrations` 会主键
    冲突、进程启动崩。`autocommit=True` 下 `pg_advisory_lock` 是**会话级**的，必须显式
    解锁（池会复用连接，不解锁等于把这把锁带进下一个请求）。
    """
    pool = _pool_for(settings.checkpoint_dsn)
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_SETUP_LOCK,))
        try:
            saver.setup()
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_SETUP_LOCK,))


# ── 造 saver ─────────────────────────────────────────────────────────
def build_checkpointer(dsn: str | None = None) -> object | None:
    """不给 DSN（`None`）⇒ 读 `settings.checkpoint_dsn`；空 ⇒ `None`（图不挂 saver）。

    **缺省值读设置而不是写死空串**（20261017）：写死空串的话 `CHECKPOINT_DSN` 就成了
    一个"填了不生效、也不报错"的幽灵旋钮（本仓在配置面上栽过五次，见
    `tests/test_settings_surface.py` 头注）。现在那一格的唯一读方就是本函数，
    而**填了真的会生效**。

    ⚠️ 只在**启动期**调（`server._build_arm` 经 lifespan）——它开池、建表，都是慢操作。
    """
    if dsn is None:
        dsn = settings.checkpoint_dsn
    if not dsn.strip():
        return None

    mod_path, pkg = backend_for(dsn)
    if mod_path != _PG_MOD:
        # sqlite 这一支**刻意不落地**：产线是 4 个 worker 共用一个文件库，并发写没验过，
        # 而"没验过的分支"正是本模块最不该写的那类代码（同头注的"别过度承诺"）。
        raise RuntimeError(
            f"{pkg} 这一支本仓没有落地（只落地了 postgres）：产线是 4 个 worker 共用"
            "一个文件库，并发写没有实测过。要 sqlite 就先补一条真跑过的判据。")
    try:
        mod = importlib.import_module(mod_path)
    except ImportError as e:
        raise RuntimeError(
            f"配了 checkpoint DSN 但后端没装（{pkg}）：{e}。"
            f"装它：`.venv/bin/pip install {pkg}`（见 ADR-0011）。"
            "**不会静默回落成内存档**——那会让 4 个 worker 各记各的上下文而不报错。"
        ) from e

    saver = mod.PostgresSaver(_pool_for(dsn))
    _setup_under_lock(saver)
    logger.info("[checkpoint] saver 就绪：backend=postgres dsn=%s", redact(dsn))
    return saver


def live_saver(agent) -> object | None:
    """从活体对象上取 saver（没有就 `None`）。

    走 `agent/probe.py::saver_of` —— 与 `/health` 的 `checkpoint_backend` **同一个
    取值实现**。级联删除要删的必须是"图上真正在用的那个 saver"，不是另造一个：
    另造一个连的是同一张表、看着也对，但它与"图有没有挂"这件事**没有任何关系**
    （配了 DSN 而图没挂时，它会老老实实删一个空线程、返回"删掉了 0 条"）。
    """
    from agent import probe
    return probe.saver_of(agent)


def delete_thread(saver, conversation_id: object) -> bool:
    """清掉某个会话的 checkpoint；返回**删完确认读不到了吗**。

    返回值刻意是"读回来确认"，不是"调用没抛"——`saver.delete_thread` 是
    `DELETE ... WHERE thread_id = %s`，删一个不存在的线程**同样不抛**
    ⇒ 只看"没抛"的话，一个恒不生效的删除与真删除**长得一模一样**
    （`tests/test_checkpoint_cascade.py` 的反控就是钉这件事）。

    没有会话身份（`conversation_thread` 返回 `None`）⇒ 直接 `False`：**没有坐标就没有
    可删的东西**，此时"删了个空线程"不是成功，是"这个请求根本不该走到这儿"。
    """
    tid = conversation_thread(conversation_id)
    if tid is None:
        return False
    saver.delete_thread(tid)
    return saver.get_tuple({"configurable": {"thread_id": tid}}) is None
