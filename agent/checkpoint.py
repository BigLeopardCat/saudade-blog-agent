"""PostgreSQL checkpoint factory and conversation lifecycle operations.

An empty DSN disables persistence. A configured DSN must connect successfully;
there is no memory fallback. Keep these tables outside the blog business schema.

Graph calls with persistence use synchronous durability. Setup is serialized
with nonblocking advisory-lock polling on autocommit connections: an open
transaction waiting for the setup lock can deadlock CREATE INDEX CONCURRENTLY.

Conversation IDs determine checkpoint threads. Delete and discard operations
verify their result by reading state back; hybrid lifecycle changes share the
conversation guard used by active turns."""

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
    """Resolve a supported checkpoint backend; unknown schemes fail explicitly."""
    head = dsn.split("://", 1)[0].strip().lower() if "://" in dsn else ""
    scheme = head.split("+", 1)[0]      # `postgresql+psycopg://` 这种带驱动的也认
    if scheme not in _BACKENDS:
        raise RuntimeError(
            f"checkpoint DSN 的 scheme 认不出来：{head or dsn!r}。只认 {_KNOWN}。"
            "**故意不支持内存档**：它在多 worker 下每 worker 各记各的，且不报错。")
    return _BACKENDS[scheme]


def redact(dsn: str) -> str:
    """Remove credentials before including a DSN in logs or errors."""
    if "@" not in dsn:
        return dsn
    head, tail = dsn.split("@", 1)
    scheme = head.split("://", 1)[0] if "://" in head else ""
    return f"{scheme}://***@{tail}" if scheme else f"***@{tail}"


def db_name(dsn: str) -> str:
    """Extract the database name for diagnostics; never use it to build SQL."""
    from urllib.parse import urlparse
    return urlparse(dsn).path.strip("/")


# ── 会话坐标 ─────────────────────────────────────────────────────────
CONV_PREFIX = "conv_"


def conversation_thread(conversation_id: object) -> str | None:
    """Map a conversation ID to its checkpoint thread; missing IDs stay unbound."""
    cid = "" if conversation_id is None else str(conversation_id).strip()
    if cid in ("", "0", "None"):
        return None
    return f"{CONV_PREFIX}{cid}"


def thread_id_for(user_id: int, conversation_id: object, holds_context: bool) -> str:
    """Reuse a conversation thread when persistence is active, otherwise use a fresh ID."""
    if holds_context:
        tid = conversation_thread(conversation_id)
        if tid:
            return tid
    return f"user_{user_id}_{uuid.uuid4().hex[:8]}"


# ── 连接池 ───────────────────────────────────────────────────────────
def _make_pool(dsn: str):
    """Create the saver pool with autocommit, dict rows and eager connection validation."""
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
    """Reuse the process pool; close and replace it when the DSN changes."""
    global _pool, _pool_dsn
    if _pool is None or _pool_dsn != dsn:
        if _pool is not None:
            _pool.close()
        _pool = _make_pool(dsn)
        _pool_dsn = dsn
    return _pool


def _try_setup_lock(conn) -> bool:
    """Try the session advisory lock without leaving an active waiting transaction."""
    row = conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (_SETUP_LOCK,)).fetchone()
    return bool(row["got"])


def wait_for_setup_lock(conn, *, now=time.monotonic, sleep=time.sleep,
                        budget: float | None = None) -> int:
    """Poll for the setup lock within a deadline; clock and sleep are injectable."""
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
    """Remove invalid checkpoint indexes in the current schema before setup retries."""
    from psycopg import sql
    names = [r["name"] for r in conn.execute(_INVALID_IDX_SQL).fetchall()]
    for name in names:
        conn.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(name)))
        logger.warning("[checkpoint] 删掉一个无效索引（上一次建表被打断留下的）：%s", name)
    return names


def _setup_under_lock(saver, pool=None, *, now=time.monotonic, sleep=time.sleep,
                      budget: float | None = None) -> None:
    """Run saver setup under a bounded advisory lock using its existing autocommit pool."""
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
        wait_for_setup_lock(conn, now=now, sleep=sleep, budget=budget)
        try:
            drop_invalid_indexes(conn)
            saver.setup()
        finally:
            # `autocommit=True` 下 `pg_try_advisory_lock` 是**会话级**的，必须显式解锁
            # （池会复用连接，不解锁等于把这把锁带进下一个请求）。
            conn.execute("SELECT pg_advisory_unlock(%s)", (_SETUP_LOCK,))


# ── 造 saver ─────────────────────────────────────────────────────────
def build_checkpointer(dsn: str | None = None) -> object | None:
    """Build the configured PostgreSQL saver at startup; empty DSN returns None.

    Reject unsupported backends and the configured forbidden business database.
    Connection or setup failures abort startup instead of returning an unusable saver."""
    if dsn is None:
        dsn = settings.checkpoint_dsn
    dsn = dsn.strip()
    if not dsn:
        return None

    # 指错库的硬闸（**缺省空 = 不拦**）：库名是运维坐标，不写进本仓（公开仓）。
    # 这里抛 = **启动失败**，不是"第一轮对话的第一次写 checkpoint 才炸"。
    _forbid = (settings.checkpoint_forbid_db or "").strip()
    if _forbid and db_name(dsn) == _forbid:
        raise RuntimeError(
            f"CHECKPOINT_DSN 指到了 CHECKPOINT_FORBID_DB 点名的库（{_forbid}）："
            f"{redact(dsn)}。业务库的结构只许由**编号迁移**改，而 saver 建表走的是 "
            "PostgresSaver.setup()、没有编号迁移 ⇒ 指进去就是在业务库上开一条没人编号、"
            "不在 CI 里的 DDL 通道（表不重名，所以不报错）。把它指到一个独立库"
            "（见本模块头注《DSN 指向哪个库》），或者把这个开关清空。")

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
    """Return the saver attached to the runtime, using the same lookup as health checks."""
    from agent import runtime_probe
    return runtime_probe.saver_of(agent)


def delete_thread(saver, conversation_id: object) -> bool:
    """Delete one conversation under its turn lock and verify that it is unreadable."""
    tid = conversation_thread(conversation_id)
    if tid is None:
        return False
    from agent.runtime.turns import conversation_turn
    config = {"configurable": {"thread_id": tid}}
    with conversation_turn(saver, config):
        saver.delete_thread(tid)
        return saver.get_tuple(config) is None


def thread_is_resuming(agent, thread_id: object) -> bool:
    """Check whether the previous turn has unfinished nodes.

    Unavailable state is treated as unfinished so write replay stays conservative."""
    tid = str(thread_id or "").strip()
    getter = getattr(agent, "get_state", None)
    if not tid or getter is None:
        logger.warning("[checkpoint] 取不到图状态（thread=%r，get_state=%s）⇒ 按上一轮没跑完办",
                       tid, getter is not None)
        return True
    try:
        snap = getter({"configurable": {"thread_id": tid}})
    except Exception as e:
        # 读不到就是判据缺失（不是"没崩"）：按保守侧办 + 留痕，见上面的注。
        logger.warning("[checkpoint] 读图状态失败（thread=%s, %s）⇒ 按上一轮没跑完办", tid, e)
        return True
    return bool(getattr(snap, "next", ()) or ())


# ── 「丢弃那一轮」要同步到图自己的 state（批 E）──────────────────────────
# 现状（20261019 之前）：Rust 从 `chat_history` 删掉那一轮的 user 消息与残缺回复，而
# **图自持的 state 里那条还在**——历史注入已关（`_graph_holds_context()`），DB 不再是
# 上下文来源 ⇒ 「丢弃」这个动作**对 agent 不再生效**：主人点了"停止生成"，agent 下一轮
# 照样记得那句被丢掉的话。本组函数是那一半。
_CUR_Q = "[当前问题]: "


def _user_text_of(msg) -> str:
    """Extract the user text from a plain or multimodal current-turn message."""
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        content = next((b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"), "")
    if not isinstance(content, str):
        content = str(content)
    return content[len(_CUR_Q):] if content.startswith(_CUR_Q) else content


def _same_turn(state_text: str, given: str) -> bool:
    """Match the stored user turn, allowing the image suffix appended by the host."""
    if state_text == given:
        return True
    if not state_text or not given.startswith(state_text):
        return False
    rest = given[len(state_text):]
    return rest == "" or rest.startswith("\n[图片")


async def drop_last_user_turn(agent, conversation_id: object, text: str = "") -> dict:
    """Discard atomically with respect to turns when the runtime exposes a guard."""
    guard = getattr(agent, "conversation_guard", None)
    if guard is None:
        return await _drop_last_user_turn(agent, conversation_id, text)
    import asyncio

    def discard():
        config = {"configurable": {"thread_id": conversation_thread(conversation_id)}}
        with guard(config):
            return asyncio.run(_drop_last_user_turn(agent, conversation_id, text))

    return await asyncio.to_thread(discard)


async def _drop_last_user_turn(agent, conversation_id: object, text: str = "") -> dict:
    """Remove the last matching user message and all following messages, then verify.

    An empty or mismatched source text does not modify state. Existing pending
    nodes may remain after discard; normal dict input restarts the next turn."""
    saver = live_saver(agent)
    if saver is None:
        return {"dropped": False, "why": "no_saver"}
    tid = conversation_thread(conversation_id)
    if tid is None:
        return {"dropped": False, "why": "bad_conversation_id"}
    getter = getattr(agent, "aget_state", None)
    setter = getattr(agent, "aupdate_state", None)
    if getter is None or setter is None:
        return {"dropped": False, "why": "no_graph_state"}

    from langchain_core.messages import HumanMessage, RemoveMessage

    cfg = {"configurable": {"thread_id": tid}}
    snap = await getter(cfg)
    msgs = list((getattr(snap, "values", None) or {}).get("messages") or [])
    idx = next((i for i in range(len(msgs) - 1, -1, -1)
                if isinstance(msgs[i], HumanMessage)), None)
    if idx is None:
        return {"dropped": False, "why": "no_user_message"}
    target = msgs[idx]
    if not text.strip():
        return {"dropped": False, "why": "no_text"}
    if not _same_turn(_user_text_of(target), text):
        return {"dropped": False, "why": "mismatch"}
    removes = [RemoveMessage(id=m.id) for m in msgs[idx:] if getattr(m, "id", None)]
    if not removes:
        return {"dropped": False, "why": "still_present"}
    # **不传 `as_node`**：`aupdate_state` 会从 `versions_seen` 推"最后是谁写的"，
    # 生产形状的图（planner/execute/model/gate）推得出来（实测）。推不出来时它抛
    # `InvalidUpdateError`（"Ambiguous update"）——**如实收成一支回执**，别让它变成父仓
    # discard 路上的一个 500（那边只看 `dropped`，`why` 进日志）。
    try:
        await setter(cfg, {"messages": removes})
    except Exception as e:                                  # noqa: BLE001
        logger.warning("[checkpoint] 丢弃那一轮写不进去（%s: %s）conv=%s",
                       type(e).__name__, e, tid)
        return {"dropped": False, "why": "ambiguous_update"}
    after = await getter(cfg)
    left = list((getattr(after, "values", None) or {}).get("messages") or [])
    gone = not any(getattr(m, "id", None) == getattr(target, "id", None) for m in left)
    return {"dropped": gone, "why": "" if gone else "still_present"}
