"""PostgreSQL checkpoint factory and conversation lifecycle operations.

An empty DSN disables persistence. A configured DSN must connect successfully;
there is no memory fallback. Keep these tables outside the blog business schema.

State writes use synchronous durability. Setup is serialized
with nonblocking advisory-lock polling on autocommit connections: an open
transaction waiting for the setup lock can deadlock CREATE INDEX CONCURRENTLY.

Conversation IDs determine checkpoint threads. Delete and discard operations
verify their result by reading state back; lifecycle changes share the
conversation guard used by active turns."""

import importlib
import logging
import time
import uuid
from contextlib import ExitStack, nullcontext

from config import settings

logger = logging.getLogger(__name__)

# In-memory persistence cannot preserve conversation state across workers.
_BACKENDS: dict[str, tuple[str, str]] = {
    "postgresql": ("langgraph.checkpoint.postgres", "langgraph-checkpoint-postgres"),
    "postgres": ("langgraph.checkpoint.postgres", "langgraph-checkpoint-postgres"),
    "sqlite": ("langgraph.checkpoint.sqlite", "langgraph-checkpoint-sqlite"),
}

_KNOWN = "postgresql:// / postgres://"
_PG_MOD = "langgraph.checkpoint.postgres"

_POOL_MAX = 4
_POOL_WAIT = 10
_SETUP_LOCK = 0x5A0DA6E
_SETUP_POLL = 0.5
_SETUP_WAIT = 60.0


def backend_for(dsn: str) -> tuple[str, str]:
    """Resolve a supported checkpoint backend; unknown schemes fail explicitly."""
    head = dsn.split("://", 1)[0].strip().lower() if "://" in dsn else ""
    scheme = head.split("+", 1)[0]      # `postgresql+psycopg://` 这种带驱动的也认
    if scheme not in _BACKENDS:
        raise RuntimeError(
            f"checkpoint DSN 的 scheme 不受支持。只认 {_KNOWN}。"
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


def _setup_under_lock(saver, *, pool, now=time.monotonic, sleep=time.sleep,
                      budget: float | None = None) -> None:
    """Run saver setup under a bounded advisory lock using its existing autocommit pool."""
    with pool.connection() as conn:
        if not conn.autocommit:
            # An open transaction can deadlock concurrent index creation.
            raise RuntimeError(
                "建表锁要求连接是 autocommit 的（`_make_pool` 的 kwargs 里那一格）。"
                "关掉它会让抢锁轮询退化成一条长事务，与 `CREATE INDEX CONCURRENTLY` "
                "互相等待。")
        wait_for_setup_lock(conn, now=now, sleep=sleep, budget=budget)
        try:
            drop_invalid_indexes(conn)
            saver.setup()
            from agent import idem_pg
            from agent.runtime import checkpoint_store
            checkpoint_store.setup(conn)
            from agent.runtime import task_input_store
            task_input_store.setup(conn)
            idem_pg.initialize(conn)
        finally:
            # `autocommit=True` 下 `pg_try_advisory_lock` 是**会话级**的，必须显式解锁
            # （池会复用连接，不解锁等于把这把锁带进下一个请求）。
            conn.execute("SELECT pg_advisory_unlock(%s)", (_SETUP_LOCK,))


# ── 造 saver ─────────────────────────────────────────────────────────
def build_checkpointer(dsn: str | None = None, *, resources: ExitStack) -> object | None:
    """Build the configured PostgreSQL saver at startup; empty DSN returns None.

    Reject unsupported backends and the configured forbidden business database.
    Register the pool with its application or evaluation resource scope.
    Connection or setup failures close the new pool and abort startup."""
    if dsn is None:
        dsn = settings.checkpoint_dsn
    dsn = dsn.strip()
    if not dsn:
        return None

    # Refuse the configured business database before opening a pool.
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
        # SQLite is recognized for an explicit error, but is not a supported backend.
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

    with ExitStack() as startup:
        pool = _make_pool(dsn)
        startup.callback(pool.close)
        saver = mod.PostgresSaver(pool)
        _setup_under_lock(saver, pool=pool)
        resources.enter_context(startup.pop_all())
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
    config = {"configurable": {"thread_id": tid, "allow_deleted_thread": True}}
    with conversation_turn(saver, config):
        from agent.runtime import checkpoint_store
        checkpoint_store.mark_deleted(saver, tid)
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
        # Unknown state must not authorize write replay.
        logger.warning("[checkpoint] 读图状态失败（thread=%s, %s）⇒ 按上一轮没跑完办", tid, e)
        return True
    return bool(getattr(snap, "next", ()) or ())


# Discard removes the same user turn from persisted model context.
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


def drop_last_user_turn(agent, conversation_id: object, text: str = "") -> dict:
    """Discard under the conversation lock using the runtime's synchronous state API."""
    guard = getattr(agent, "conversation_guard", None)
    config = {"configurable": {"thread_id": conversation_thread(conversation_id)}}
    with guard(config) if guard is not None else nullcontext():
        return _drop_last_user_turn(agent, conversation_id, text)


def _drop_last_user_turn(agent, conversation_id: object, text: str = "") -> dict:
    """Remove the last matching user message and all following messages, then verify.

    An empty or mismatched source text does not modify state. Existing pending
    nodes may remain after discard; normal dict input restarts the next turn."""
    saver = live_saver(agent)
    if saver is None:
        return {"dropped": False, "why": "no_saver"}
    tid = conversation_thread(conversation_id)
    if tid is None:
        return {"dropped": False, "why": "bad_conversation_id"}
    getter = getattr(agent, "get_state", None)
    setter = getattr(agent, "update_state", None)
    if getter is None or setter is None:
        return {"dropped": False, "why": "no_graph_state"}

    from langchain_core.messages import HumanMessage, RemoveMessage

    cfg = {"configurable": {"thread_id": tid}}
    snap = getter(cfg)
    from agent.runtime.recovery import discard_interrupted
    interrupted = discard_interrupted(agent, cfg, getattr(snap, "values", None) or {}, text)
    if interrupted is not None:
        return interrupted
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
    # The store chooses its commit node; verify state after the update.
    try:
        setter(cfg, {"messages": removes})
    except Exception as e:                                  # noqa: BLE001
        logger.warning("[checkpoint] 丢弃那一轮写不进去（%s: %s）conv=%s",
                       type(e).__name__, e, tid)
        return {"dropped": False, "why": "ambiguous_update"}
    after = getter(cfg)
    left = list((getattr(after, "values", None) or {}).get("messages") or [])
    gone = not any(getattr(m, "id", None) == getattr(target, "id", None) for m in left)
    if gone:
        from agent.runtime import checkpoint_store
        checkpoint_store.prune(saver, tid)
    return {"dropped": gone, "why": "" if gone else "still_present"}
