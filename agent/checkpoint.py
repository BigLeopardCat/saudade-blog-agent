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

## 建表（`setup()`）：要一把锁，而且**必须是 `pg_try_advisory_lock`**

`saver.setup()` 是幂等的，但**不是并发安全的**：它读一次版本号再逐条跑迁移，而每条迁移
末尾都 `INSERT INTO checkpoint_migrations (v)`（**没有 `ON CONFLICT`**）。产线是
`--workers 4`，四个进程同时启动 ⇒ 两个都读到 `version=-1` ⇒ 后到的那个**主键冲突、
启动崩**。所以建表要串行化。**这是"多 worker 同时启动"这一件事的补丁，不是通用迁移方案。**

⚠️ **20261018 上线当天，第一版用的是阻塞式 `pg_advisory_lock`，它造成了一次真停服。**
`pg_advisory_lock` 会一直挂着等，而**一条挂着的语句就是一条没结束的事务**；`setup()` 的
迁移里有 `CREATE INDEX CONCURRENTLY`，它的语义要求**等所有并发事务收尾**才能结束。于是：

  · 拿到锁的那个 worker 在跑 CIC，等"那三个等锁的 worker 的事务"结束；
  · 那三个事务在等它放锁；而它要等 CIC 结束才放锁 ⇒ **环**。

四个 worker 无限期卡死、**一条日志都没有**（`Agent ready` 那行永远打不出来，8010 因此
根本不监听），`systemctl restart` 也卡在"等旧 worker 退出"上（旧 worker 在 PG 上无限期
等待，要等满 `TimeoutStopSec=120` 才被 SIGKILL）。实测现场（`pg_stat_activity`）：

```
1 条 idle 持有 advisory 锁  +  3 条 active 卡在 Lock/advisory
                          +  1 条 active 卡在 Lock/virtualxid 跑 CREATE INDEX CONCURRENTLY
```

**换成 `pg_try_advisory_lock` 轮询就断了这个环**：它立刻返回，每次尝试都是一条**已提交、
已结束**的语句，等锁的进程在两次尝试之间**不持有任何事务**——它拿到的快照比 CIC 的新，
CIC 的 `WaitForOlderSnapshots` 不认它（这也是为什么现场那条 `idle` 的持锁会话没有参与环：
idle 的 autocommit 会话 `xmin` 已清零，不挡 CIC；挡住的是三条**卡在语句里**的会话）。
再加一个总预算 ⇒ 最坏情况是一条点名的启动错误，而不是一次无声的无限期挂起。

⚠️ **这条不变量依赖池是 `autocommit` 的**（`_make_pool` 的 kwargs 里那一格）：关掉它，
每次 `pg_try_advisory_lock` 都会把事务开着，环就回来了。所以 `_setup_under_lock` 里有一条
**显式断言**，判据在 `tests/test_checkpoint_factory.py` ⑨。

## 级联清理（`delete_thread`）

删会话要清掉那个会话的 checkpoint——不清就是**留着别人的对话正文在库里、且没有任何
读取路径能删它**（同 ADR-0011 的级联清理一节）。机制住本模块（`saver.delete_thread`
是 langgraph 的公开 API，表名与结构不进本仓），HTTP 面住 `server.py` 的
`DELETE /checkpoint/{conversation_id}`，**删除动作在父仓** `src/routes/conversation.rs`
（跨仓契约，`tests/test_checkpoint_cascade.py` 有一条父仓守卫盯着）。

## 可验证面（别过度承诺）

判据分四层：
  · **纯函数**（`conversation_thread` 的负例、`redact` 不泄口令、`backend_for` 认不出的
    响亮、抢锁轮询与无效索引清理的形状）在 `tests/test_checkpoint_factory.py`；
  · **接线形状**（三处共用同一个判据）在 `tests/test_checkpoint_adoption.py`；
  · **级联**（no-op 删除要红）在 `tests/test_checkpoint_cascade.py`；
  · **真连一个库、4 个进程同时建表**在 `tests/test_checkpoint_setup_concurrent.py`。

⚠️ **第四层是 20261018 那次停服之后补的，而它才是抓得住这个 bug 的那一层。** 前三层
（包括那两条"连不上要响亮抛"）全都在 `127.0.0.1:1` 这种**必然失败**的 DSN 上判，于是
"真连上一个库、真跑一次迁移、4 个进程真的同时来"这条路径**从来没有被跑过**——死锁因此
在四层判据里最该红的那一层恒绿的情况下发生了。**"前提不可用"与"前提不可用所以没量"是
两件事**：这一层缺库时报「未评估」并退 0，`tests/run_all.py` 的汇总里另有一行**每次运行
都报**这一层量没量。
"""

import importlib
import logging
import time
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
# 抢锁轮询：`_SETUP_POLL` 是两次尝试之间隔多久（**这两次之间不持有任何事务**，见头注），
# `_SETUP_WAIT` 是总预算——**必须有上限**：阻塞版连"卡住了"这件事都读不出来。
_SETUP_POLL = 0.5
_SETUP_WAIT = 60.0

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


def _try_setup_lock(conn) -> bool:
    """试着拿建表锁；拿到 `True`、没拿到 `False`。**一条语句、立即返回**。

    ⚠️ **不许换成阻塞版 `pg_advisory_lock`**：它让"等锁"变成一条没结束的事务，而
    `setup()` 的 `CREATE INDEX CONCURRENTLY` 要等所有并发事务收尾 ⇒ 环 ⇒ 四个 worker
    无限期卡死。现场与完整推理见模块头注《建表》那一节（20261018 真停服）。
    """
    row = conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (_SETUP_LOCK,)).fetchone()
    return bool(row["got"])


def wait_for_setup_lock(conn, *, now=time.monotonic, sleep=time.sleep,
                        budget: float | None = None) -> int:
    """轮询抢锁，拿到就返回**试了几次**；超预算**响亮抛**。

    `now` / `sleep` 可注入是为了判据能离线跑（不真等 60 秒）——同 `_POOL_WAIT` 那条。
    """
    budget = _SETUP_WAIT if budget is None else budget
    deadline = now() + budget
    tries = 0
    while True:
        tries += 1
        if _try_setup_lock(conn):
            return tries
        if now() >= deadline:
            raise RuntimeError(
                f"建表咨询锁 {budget:.0f}s 没抢到（试了 {tries} 次，每 {_SETUP_POLL}s 一次）"
                "——另一个进程正在建表，或者有一把没人放的旧锁。**不静默降级**：带着一个"
                "没建好表的 saver 起来，故障会推迟到第一轮对话的第一次写 checkpoint，"
                "那时报的错与配置没有任何可读的联系。")
        sleep(_SETUP_POLL)


# 无效索引：`indisvalid=false` 的索引在 `current_schema()` 里、挂在 `checkpoint*` 表上。
_INVALID_IDX_SQL = """
SELECT c.relname AS name
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
  JOIN pg_class t ON t.oid = i.indrelid
  JOIN pg_namespace n ON n.oid = t.relnamespace
 WHERE NOT i.indisvalid
   AND n.nspname = current_schema()
   AND t.relname LIKE 'checkpoint%'
"""


def drop_invalid_indexes(conn) -> list[str]:
    """删掉 checkpoint 表上**无效**的索引，返回删掉的名字（可能为空列表）。

    ## 为什么必须有这一步

    `CREATE INDEX CONCURRENTLY` 被打断（进程被 SIGKILL、语句超时）会留下一个
    **存在但无效**的索引，而迁移里写的是 `CREATE INDEX CONCURRENTLY IF NOT EXISTS …`
    ⇒ **名字已经存在** ⇒ 以后每次都跳过它。于是"下一次启动"看着一切正常，那张表
    **永远少一个索引**，而没有任何东西会红。20261018 那次死锁被打断之后
    `checkpoints_thread_id_idx` 正是这副样子（迁移停在 `max(v)=5`、索引无效）。

    删掉它，下一次 `setup()` 就会老老实实重建。

    **限定 `current_schema()` + 表名前缀 `checkpoint`**：那是本 agent 自持的表集
    （DSN 那一节写着"别指错库"），碰不到博客业务表。
    """
    from psycopg import sql
    names = [r["name"] for r in conn.execute(_INVALID_IDX_SQL).fetchall()]
    for name in names:
        conn.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(name)))
        logger.warning("[checkpoint] 删掉一个无效索引（上一次建表被打断留下的）：%s", name)
    return names


def _setup_under_lock(saver, pool=None) -> None:
    """在咨询锁里建表（**轮询式、有预算**，理由见模块头注《建表》）。

    `pool` **由调用方给**（`build_checkpointer` 把 saver 用的那个原样传进来）。
    缺省回落到 `settings.checkpoint_dsn` 只为判据能单独调它——**生产路径不许走缺省**：
    自己去取会让"调用方给的 DSN"与"设置里那一格"不一致时关掉 saver 手里那个池。
    """
    pool = _pool_for(settings.checkpoint_dsn) if pool is None else pool
    with pool.connection() as conn:
        if not conn.autocommit:
            # 这条断言是**死锁不回来的前提**：非 autocommit 下每次 `pg_try_advisory_lock`
            # 都会把事务开着，"等锁"于是又变回"一条没结束的事务"，而 setup() 里的
            # `CREATE INDEX CONCURRENTLY` 要等所有并发事务收尾 ⇒ 环。
            raise RuntimeError(
                "建表锁要求连接是 autocommit 的（`_make_pool` 的 kwargs 里那一格）。"
                "关掉它会让抢锁轮询退化成一条长事务，与 `CREATE INDEX CONCURRENTLY` "
                "互相等 —— 就是 20261018 那次停服的形状。")
        wait_for_setup_lock(conn)
        try:
            drop_invalid_indexes(conn)
            saver.setup()
        finally:
            # `autocommit=True` 下 `pg_try_advisory_lock` 是**会话级**的，必须显式解锁
            # （池会复用连接，不解锁等于把这把锁带进下一个请求）。
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

    # **池只在这里取一次、并原样传给建表那一步**（20261018 修）：`_setup_under_lock`
    # 早先是自己去 `_pool_for(settings.checkpoint_dsn)` 取的，于是"调用方给的 DSN"与
    # "设置里那一格"不一致时，`_pool_for` 会因为 DSN 变了而**把上一个池关掉再建一个**
    # ——关掉的正是 saver 手里那个。显式传参把这两件事钉成一件。
    pool = _pool_for(dsn)
    saver = mod.PostgresSaver(pool)
    _setup_under_lock(saver, pool=pool)
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
