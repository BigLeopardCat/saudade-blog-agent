"""Durable at-most-once ledger for external writes.

Record intent before calling a tool and store its result afterward. An unfinished
intent has an unknown outcome and must not be retried automatically. Ledger
failure blocks new writes; it cannot undo a write already sent to a remote API.

Legacy graph calls derive an operation key from the conversation, tool and
argument fingerprint. Their attempt identity distinguishes replay from a new
request; the caller supplies whether the previous turn was unfinished.
Confirmed hybrid transactions instead use the signed operation ID directly.

PostgreSQL savers use the same database for the ledger and checkpoints. Other
legacy contexts use SQLite. Keep ledger retention aligned with checkpoints so a
retained checkpoint cannot replay a write after its deduplication record expires.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time

from agent import authz
from config import settings
from tools.base import ToolResult, unavailable
from tools.result import error_frame
from utils.trace import record

logger = logging.getLogger(__name__)

# 原因码：**三处同批改**（本文件的帧与取回、`agent/block_reasons.py` 的 REASONS、
# `server.py` 的 `_REASON_CN`）。前两处只改一处会当场红（既有判据对着比这两张表）。
REASON_OUTCOME_UNKNOWN = "write_outcome_unknown"

# 账本在场时**必须**的 checkpoint 档位。理由与实测对照见 `require_sync_durability`。
REQUIRED_DURABILITY = "sync"


class ReplayMisconfigured(RuntimeError):
    """账本在场而档位不对。**刻意用异常**：配置错不该被降级成一条回执。"""

# 记账时留存的返回文本上限。写操作的返回几乎都是短确认；这一格只防"某个写工具将来
# 返回一大坨"。撞顶时**不静默**：存 NULL + 一条 trace，重放拿到的是"已执行过、
# 原始返回未留档"的如实帧（是降级，不是假装没发生）。
_RESULT_MAX = 20000

_REASON_RE = re.compile(rf"\[{REASON_OUTCOME_UNKNOWN}\]")

# 撞顶时重放拿到的帧。**不是**错误帧——写确实发生了，这一点是事实，`_check_spec`
# 应当 PASS 并产回执；只是原始返回没留档。它刻意写得不像正常返回，好让"重放拿到了
# 降级帧"在下游（引用取值、narrator）留得下痕迹而不是被当成一次正常结果。
_NOREPLAY_FRAME = ToolResult(
    "（这一步已经执行过：重放命中幂等账。原始返回超过留档上限，未留存，"
    "如需确切结果请重新读一次）", "ok", {})


def require_sync_durability(config) -> None:
    """账本在场时，这一轮**必须**是 `durability="sync"`，否则响亮抛。

    ## 为什么这条断言住在这里，而不是文档里

    本账的全部依据是"**幂等键在崩溃恢复前后逐字节相同**"，而键取自
    `config["configurable"]["checkpoint_ns"]`（= `f"{节点名}:{task_id}"`）。这个前提
    **只在 `durability="sync"` 下成立**，实测三行对照：

    | 档位 / 收场方式 | 恢复后 `__pregel_task_id` |
    |---|---|
    | `sync` + 硬杀 | 与崩前**逐字相同** ⇒ 账认得出来 |
    | `async`（缺省）+ 硬杀 | **变了** ⇒ 键变 ⇒ 账**被静默绕过**（读数回到 2×） |
    | 任意档 + 进程内抛异常 | 不变（异常路径本来就会刷盘） |

    ⚠️ 缺省就是 `async`：**什么都不做就会踩中**，而踩中之后的表面现象是"账在、代码在、
    一切正常，只是没起作用"。这种失效模式靠读代码看不出来，只能靠一条会响的断言。

    放在 `guarded_invoke` 的 `try` **外面**：这是配置错，不是"服务不可用"，不该被
    降级成一条 `unavailable` 回执（那会让一整轮的写静悄悄地全都不执行）。
    """
    got = None
    try:
        got = ((config or {}).get("configurable") or {}).get("__pregel_durability")
    except AttributeError:
        got = None
    if got != REQUIRED_DURABILITY:
        raise ReplayMisconfigured(
            f"写操作幂等账要求 durability={REQUIRED_DURABILITY!r}，当前是 {got!r}。"
            f"缺省档 async 下 checkpoint 由后台线程写，硬杀时可能没落盘 ⇒ 恢复后 "
            f"`__pregel_task_id` 变了 ⇒ 幂等键变了 ⇒ 账被静默绕过（写会重放）。"
            f"调用处必须传 `durability={REQUIRED_DURABILITY!r}`。")


def active(config) -> bool:
    """这一轮有没有挂 checkpointer（= 重放这条路通不通电）。

    没有 saver ⇒ LangGraph 给的是随机 uuid4 的 `checkpoint_ns`，重放**不可能**发生
    ⇒ 账本一行都不该记。这个函数就是那个闸，也是"今天生产行为零改动"的依据。
    `config` 可以是 `None`（`execute_node(state, config=None)` 的默认值）。
    """
    try:
        return (config or {}).get("configurable", {}).get(
            "__pregel_checkpointer") is not None
    except AttributeError:      # 认不出的 config 形状 ⇒ 当作没挂（不猜）
        return False


def key_for(config, name: str, fingerprint: str) -> str:
    """幂等键 = sha256([**会话身份**, 工具名, 参数指纹])。

    三个成分缺一不可：`thread_id` 把**不同会话**分开（同一个会话里的同一件写才是"同一件"）；
    工具名把同一轮里的不同工具分开；参数指纹把**同一工具两次不同调用**分开——少了最后
    那一个，"先建标签 A 再建标签 B"会被合成一次，那是"至多一次"退化成"从不执行"的
    地方，而且**不报错**。

    ⚠️ **这一格 20261019 换过**：原先是 `checkpoint_ns`（= `f"{节点名}:{task_id}"`）。它
    确实能把同一 thread 的不同轮分开，但**生产每轮传 dict 输入 ⇒ langgraph 丢弃未完成
    任务、从 START 重启 ⇒ 全新的 checkpoint 血缘 ⇒ 它逐字变** ⇒ 账在生产那条路上恒不
    命中（读数与机制见头注）。换成 `thread_id` 之后，"哪一轮的哪一次"没有丢——它降级成
    `attempt_for` 存进行里，用来分"重放"与"主人又要了一次"。

    用 sha256 而不是"三段用分隔符拼起来"：分隔符一旦长在某个值里，一条键会被读成
    两条（本仓踩过，见 `saudade-agent-plan-text-separator`）。定长摘要没有这个面。
    """
    try:
        tid = ((config or {}).get("configurable") or {}).get("thread_id") or ""
    except AttributeError:
        tid = ""
    raw = json.dumps([tid, name, fingerprint], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def attempt_for(config, fingerprint: str) -> str:
    """**这一次尝试**的身份 = sha256([checkpoint_ns, 参数指纹])。

    与 `key_for` 是两个角色，别合并：键回答"这是不是**同一件**写"（跨重启要稳定），
    本函数回答"这是不是**同一次**尝试"（同一 superstep 的重放要稳定、换一轮要变）。
    两者相等 ⇒ 重放；不相等 + 上一轮没跑完 ⇒ 也是重放；不相等 + 上一轮跑完了 ⇒
    主人又要了一次。

    `checkpoint_ns` 逐轮重编这件事在这里**不是缺陷而是特性**（见头注《修法》）：它正是
    "换了一轮"的那个信号。
    """
    try:
        ns = ((config or {}).get("configurable") or {}).get("checkpoint_ns") or ""
    except AttributeError:
        ns = ""
    raw = json.dumps([ns, fingerprint], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resuming(config) -> bool:
    """这一轮开始时**上一轮跑没跑完**（服务端算好，走 `configurable.idem_resuming`）。

    服务端那一侧的算法与理由见 `agent/checkpoint.py::thread_is_resuming`。**缺省 `True`**
    是刻意的：读不到那一格就按"上一轮没跑完"办 —— 失效姿态落在**多拦一次**（主人会看到
    `write_outcome_unknown` 那条帧，看得见）而不是**静默放过一次重做**（看不见）。所以
    "调用点忘了设"是响的，不是哑的。
    """
    try:
        got = ((config or {}).get("configurable") or {}).get("idem_resuming")
    except AttributeError:
        return True
    return True if got is None else bool(got)


def replay_reason(text: str) -> str | None:
    """从错误帧取回原因码（非本族帧 → None），供 `_check_spec` 的 or 链接上。

    与 `authz.scope_error_reason` / `adminops.target_error_reason` 同形：**只认自己的
    那个码**，别族帧一律 None。
    """
    return REASON_OUTCOME_UNKNOWN if _REASON_RE.search(str(text or "")) else None


def unknown_frame(name: str) -> ToolResult:
    """「上一次发出去了、但没拿到返回」的错误帧（checker 判 BLOCK）。"""
    return error_frame(f"__ERROR__: 写操作结果未知[{REASON_OUTCOME_UNKNOWN}]"
            f"（{name} 上一次已经发出，进程在拿到返回之前中断了——**不要重试**，"
            f"重试可能把它做第二遍。先读一次确认它到底成没成，再把结果如实告诉主人）",
            REASON_OUTCOME_UNKNOWN)


def _call(tool, args):
    """今天那条调用路径，逐字搬进来（含 `__ERROR__` 的兜底形态）。

    搬进本模块是为了让 `agent/graph.py` 里**只剩一个出口**——`tool.invoke(` 从此只
    出现在这里，`tests/test_write_idempotency.py` 的接线锁按这条钉。
    """
    try:
        return tool.invoke(args)
    except Exception as e:
        return f"__ERROR__: {type(e).__name__}: {e}"


def _rehydrate(row) -> str:
    """重放：把记下的那份**逐字**还给下游（文本 + kind + meta 三样都要）。

    `meta` 必须还原：命令类写工具（`navigate_to` / `toggle_effect` / `toggle_dark_mode`）
    的连线命令就住在 `meta["cmd"]`，`_check_spec` 的 `cmd_shape` 按它校验——只还文本
    会让重放出来的命令帧判 BLOCK，那是一次**凭空的**失败。
    """
    _status, text, kind, meta_json = row
    if text is None:
        return _NOREPLAY_FRAME
    try:
        meta = json.loads(meta_json) if meta_json else {}
    except (TypeError, ValueError):
        meta = {}
    return ToolResult(text, kind or "ok", meta if isinstance(meta, dict) else {})


# 取行的 SQL **只此一处**：多取一列 `fp`（本模块自己比对 `attempt` 用），比对完就由
# `_split_row` 剥掉。两个后端各写一遍列清单是它们各自方言决定的（`?` / `%s`），但**列序
# 与列数必须是同一份**——否则 `_split_row` 的下标会在一档上错位。
_SELECT_COLS = "status, fp, text, kind, meta"


# Shared decoder for persisted tool results.
replay_result = _rehydrate


def _split_row(row) -> tuple[str, tuple | None]:
    """账本行 → `(status, 交给 `_rehydrate` 的那一段)`。

    ⚠️ **交给 `_rehydrate` 的必须仍是 4 元组** `(status, text, kind, meta)`：PG 那条池的
    `row_factory` 是 `dict_row`，喂 dict 会**按键名解包**、不报错地回放字面串
    （`tests/test_write_idempotency.py` 块⑩ 钉的就是这个形状）。`fp` 只用于比对
    attempt，**不进**那一段。
    """
    if row is None:
        # 插入被忽略（行存在）却回读不到（行没了）——只可能是并发清理撞上了。
        # **不猜**：抛出去，交给上层 fail-closed（拒绝这次写）。
        raise RuntimeError("幂等账行状态不可判定（插入被忽略但回读为空）")
    if row[0] not in ("done", "intent"):
        raise RuntimeError(f"幂等账行状态认不出：{row[0]!r}")
    return row[0], (row[0], row[2], row[3], row[4])


class Ledger:
    """一本 SQLite 账。`key` 是主键 ⇒ 多个 worker 天然互斥（生产 4 个）。

    连接**惰性**：import 期不开库，`active()` 为假时一次都不开——没挂 saver 的环境
    连这个文件都不该被创建出来。
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = None

    # 冷启重试的退避与次数（见 `_connect` 的注）。20 × 50ms ≈ 1s 的上限。
    _OPEN_RETRIES = 20
    _OPEN_BACKOFF = 0.05

    def _connect(self):
        """开库 + 建表。**带重试，且重试的是"冷启竞态"不是"等锁"**。

        实测（20261017，`--concurrency 4 --guard` 探针）：4 个真进程**同时**冷启一个
        还不存在的库时，`claim` 有约 1/5 的概率抛 `database is locked` ——
        而 `PRAGMA journal_mode=WAL` 要一把排他锁，SQLite **不为它调 busy handler**
        （把 `busy_timeout` 挪到 WAL 那句之前也没用，实测 6/32 vs 7/32）⇒ 那句是
        **立刻**抛的，`timeout=5.0` 挡不住。加这一圈短退避重试后实测 0/32。

        ⚠️ 这一格**只在冷启**存在（库建好之后走的是 busy handler，正常等锁）。所以
        退避很短、次数有限；**用尽仍然抛** —— 调用方 `guarded_invoke` 的 fail-closed
        会把它变成"拒绝这次写"（`unavailable`），那是刻意的姿态，不在这里吞掉。

        ⚠️ 这一格是**降级档（SQLite）**的：账挂在 PG saver 上时走 `idem_pg`，没有
        开库竞态这回事。留着它是因为"降级档也得真的能用"，而且探针在它上面出读数。
        """
        last: Exception | None = None
        for attempt in range(self._OPEN_RETRIES):
            if attempt:
                time.sleep(self._OPEN_BACKOFF)
            conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
            try:
                # `isolation_level=None` = 自动提交：`claim` 的 INSERT 必须在**发出
                # 请求之前**真的落盘，否则"先记账"这条协议是假的。
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS idem ("
                    " key TEXT PRIMARY KEY, tool TEXT NOT NULL, fp TEXT NOT NULL,"
                    " status TEXT NOT NULL, ts REAL NOT NULL,"
                    " text TEXT, kind TEXT, meta TEXT)")
            except sqlite3.OperationalError as e:
                conn.close()          # 不关就是每重试一次漏一条连接
                last = e
                continue
            except Exception:
                conn.close()
                raise
            return conn
        raise last

    def _db(self):
        if self._conn is None:
            with self._lock:
                if self._conn is None:
                    parent = os.path.dirname(self.path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    self._conn = self._connect()
        return self._conn

    def claim(self, key: str, tool: str, attempt: str, *,
              resuming: bool = True) -> tuple[str, tuple | None]:
        """占位。返回 `("new"|"done"|"intent", 行或 None)`。

        `INSERT OR IGNORE` + 回读是**一次原子判定**：插入成功 ⇒ 这一件从没做过；
        被忽略 ⇒ 行已存在，再按两格分流（口径见头注《修法》）：

          · `行.fp == attempt` ⇒ **同一 superstep 的重放** ⇒ 逐字就是今天的行为；
          · 换了一轮（`行.fp != attempt`）而 **`resuming`** ⇒ 上一轮没跑完 ⇒ 那一行就是
            那一轮的尝试 ⇒ `done` 重放 / `intent` 判结果未知；
          · 换了一轮而**非 `resuming`** ⇒ 主人明确再要一次 ⇒ 把那行原子地改回 `intent`
            并换 `attempt` ⇒ 当新的一件执行。

        `attempt` 存在 `fp` 列里（**列名与表结构不变**，换的只是值的算法）。
        """
        conn = self._db()
        cur = conn.execute(
            "INSERT OR IGNORE INTO idem(key, tool, fp, status, ts)"
            " VALUES (?,?,?,'intent',?)", (key, tool, attempt, time.time()))
        if cur.rowcount == 1:
            return "new", None
        row = conn.execute(f"SELECT {_SELECT_COLS} FROM idem WHERE key=?", (key,)).fetchone()
        if row is None:
            raise RuntimeError("幂等账行状态不可判定（插入被忽略但回读为空）")
        if row[1] == attempt or resuming:
            return _split_row(row)
        # 合法的重复：**一次原子的 UPDATE** 才算抢到（`fp` 与 `status` 一起进 WHERE，
        # 抢不到就是有人同时在动这一行 ⇒ 回读一次再判，不猜）。
        got = conn.execute(
            "UPDATE idem SET status='intent', fp=?, text=NULL, kind=NULL, meta=NULL, ts=?"
            " WHERE key=? AND fp=?", (attempt, time.time(), key, row[1])).rowcount
        if got == 1:
            return "new", None
        return _split_row(conn.execute(
            f"SELECT {_SELECT_COLS} FROM idem WHERE key=?", (key,)).fetchone())

    def finish(self, key: str, out) -> bool:
        """记完成。返回"结果是否因超上限而未留档"（撞顶时上层补一条 trace）。"""
        text = str(out)
        capped = len(text) > _RESULT_MAX
        self._db().execute(
            "UPDATE idem SET status='done', text=?, kind=?, meta=? WHERE key=?",
            (None if capped else text, str(getattr(out, "kind", "ok") or "ok"),
             json.dumps(getattr(out, "meta", None) or {}, ensure_ascii=False), key))
        return capped

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class NullLedger:
    """不记账的账本 = **今天的形状**。

    存在的理由只有一条：**反控**。判据要用它证明"没有这本账时重放真的会让写发生
    两次"——否则探针①（有账 ⇒ 只发生一次）不红不算数，它可能只是恒真。
    """

    def claim(self, key: str, tool: str, attempt: str, *,
              resuming: bool = True) -> tuple[str, tuple | None]:
        return "new", None

    def finish(self, key: str, out) -> bool:
        return False


_shared_lock = threading.Lock()
_shared_ledger = None


def _pg_pool(config):
    """Read the legacy graph's saver and require the configured PostgreSQL pool."""
    from agent.runtime.checkpoint_store import backend, postgres

    try:
        saver = ((config or {}).get("configurable") or {}).get("__pregel_checkpointer")
    except AttributeError:
        return None
    if saver is None or backend(saver) == "memory":
        return None
    if not postgres(saver):
        raise RuntimeError("不支持的持久化后端；不能回落到本地账本。")
    pool = saver.conn
    if not callable(getattr(pool, "connection", None)):
        raise RuntimeError("PostgreSQL 幂等账要求连接池；不能回落到本地账本。")
    return pool


def _shared(config=None):
    """这一轮该用哪本账。**两档**，按 saver 自己选：

      · 挂的是 PG saver ⇒ 账落**同一个池、同一个库**（`agent/idem_pg.py`）。这样账与
        checkpoint 的保留期天然同源——分开清会把本批要堵的洞重新打开（清了账、留着
        checkpoint ⇒ 恢复重放时账是空的 ⇒ 写落第二遍）。
      · 其余（内存 saver / 没挂 saver / CI）⇒ 今天的 SQLite，**逐字节不变**。

    **失败姿态**：拿到池但建表/断言失败 ⇒ **响亮抛，绝不静默退回 SQLite**。调用方
    （`guarded_invoke`）把它变成 `unavailable` ⇒ 拒绝这次写——ADR-0012 的失效姿态就是
    "账本不可用 ⇒ 拒绝这次写"，不是"换一本假装没事的账"。

    缓存口径**刻意不同**：SQLite 那档缓存（一条进程级连接，4 个 worker 各持一份，互斥靠
    主键），PG 那档**不缓存**（`PostgresLedger` 是薄壳、构造零成本，而池本身已经是共享的
    ——再包一层缓存只会给"测试之间互相泄漏结论"留门）。

    **只在真的要记账时才会被调到**：`active()` 为假时 `guarded_invoke` 在它之前就返回了。
    """
    pool = _pg_pool(config)
    if pool is not None:
        from agent import idem_pg     # psycopg 只住那边（依赖面锁：块⑪）
        return idem_pg.PostgresLedger(pool)
    global _shared_ledger
    with _shared_lock:
        if _shared_ledger is None:
            _shared_ledger = Ledger(settings.idem_db)
    return _shared_ledger


def guarded_invoke(tool, name: str, args: dict, config, *,
                   fingerprint: str, ledger=None) -> str:
    """写操作的**唯一出口**：账本在场则至多一次，账本不在场则与今天逐字相同。

    ⚠️ 账本在场时**必须** `durability="sync"`（`require_sync_durability`，档不对当场抛）：
    缺省 `async` 下恢复后的 `__pregel_task_id` 会变，账**静默失效**。

    账本**不进 config**：本函数只拿到 `config`，后端由 `_shared(config)` 按 saver 自己选
    （挂 PG saver ⇒ 落同一个库；否则 SQLite）。`ledger=` 那个参数是给判据换账本用的
    （反控 `NullLedger` 之类），生产调用点不传。

    `fingerprint` **必须由调用方在改动参数之前取**：`execute_node` 会在调用前把
    `device_oled_display` 缺的 `text` 用 temperature 0.7 的 LLM 现创出来
    （`agent/graph.py::_create_display_text`）——对改后的 args 取指纹，那个工具每次
    指纹都不同，幂等判据对它**恒不命中，而且不报错**。

    键与"这一次尝试"是**两个**值（`key_for` / `attempt_for`，理由见头注《修法》）；
    `resuming` 那一格由服务端按"上一轮跑没跑完"算好塞进 config（缺省按"没跑完"办）。
    """
    if not active(config):
        return _call(tool, args)        # 今天的形状：直通，一行账都不记
    # 账本在场 ⇒ 先查档位。**在 `try` 之外、在 is_write 之前**：档不对是配置错，
    # 对**任何**工具都该当场炸，不该等"恰好这件是写"才炸，更不该降级成一条回执。
    require_sync_durability(config)
    if not authz.is_write(name):
        return _call(tool, args)
    key = key_for(config, name, fingerprint)
    attempt = attempt_for(config, fingerprint)
    led = None
    try:
        # 选后端也在这个 `try` 里：PG 档建表/断言失败 ⇒ 与"记账失败"同一个姿态
        # （`unavailable` ⇒ 拒绝这次写），**不静默退回 SQLite**。
        led = ledger if ledger is not None else _shared(config)
        state, row = led.claim(key, name, attempt, resuming=resuming(config))
    except Exception as e:
        logger.warning("[idem] 记账失败，拒绝执行写操作 %s: %s", name, e)
        record("execute", "idem_error", name=name,
               phase="claim" if led is not None else "open", error=str(e)[:200])
        return unavailable("写操作未执行：幂等账不可用（拒绝在无法记账的情况下动手）")
    if state == "done":
        record("execute", "write_replay_hit", name=name, key=key)
        return _rehydrate(row)
    if state == "intent":
        # 上一次"已经记账、还没拿到返回"就死了。**不许重试**——重试正是要防的那件事。
        record("execute", "write_outcome_unknown", name=name, key=key)
        return unknown_frame(name)
    out = _call(tool, args)
    try:
        if led.finish(key, out):
            record("execute", "idem_result_capped", name=name, key=key)
    except Exception as e:
        # 写**已经发生**了，取消不了；收尾记账失败不能让一条成功的写看起来像失败。
        logger.warning("[idem] 记账收尾失败（写已发生，无法撤销）: %s", e)
        record("execute", "idem_error", name=name, phase="finish", error=str(e)[:200])
    return out
