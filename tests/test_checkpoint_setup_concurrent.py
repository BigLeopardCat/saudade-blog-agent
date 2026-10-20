"""真连一个库、**4 个进程同时建表**（20261018 真停服之后新增的那一层判据）。

**这一件为什么存在**：`agent/checkpoint.py` 的建表锁第一版是**阻塞式**
`pg_advisory_lock`。20261018 上线当天它造成了一次真停服——四个 worker 无限期卡死、
8010 根本不监听、`systemctl restart` 也跟着卡住（旧 worker 在 PG 上无限期等待，要等满
`TimeoutStopSec=120` 才被 SIGKILL）。而当时**四层判据全绿**，因为它们全都在
`127.0.0.1:1` 这种**必然失败**的 DSN 上判："真连上一个库、真跑一次迁移、4 个进程真的
同时来"这条路径**从来没有被跑过**。这一件就是那条路径。

死锁的形状（`pg_stat_activity` 实测）：拿到锁的那个 worker 在跑
`CREATE INDEX CONCURRENTLY`，而 CIC 必须**等所有并发事务收尾**；另外三个 worker 卡在
`pg_advisory_lock` 上——**一条挂着的语句就是一条没结束的事务**。于是互相等，成环。

## 同步点：为什么不是「同时 Popen 四个进程」

第一版就是那样写的，**红基线复现不出来**（四个进程全绿）——四个 `python -c` 各自冷启动
import，到达抢锁那一刻的散布有几百毫秒，比空库上那几句 CIC 还长，于是赢家早就建完了。
生产上不是这样：四个 uvicorn worker 是**预热好的**（同一秒里都打出了
`Initialising LangChain agent ...`）。

所以这里的子进程**先各自把池开好**（最慢的那一步），报到之后再放一个 go 文件、
四个同时往下走——到达抢锁那一刻的散布是亚毫秒级。**这个同步点只去掉脚手架的抖动，
不改变锁的语义**：A/B 两臂用的是同一套脚手架，换的只有建表那一步。

两件事，缺一不可：
  · **红基线**：把建表那一步换回**阻塞版**，同一组输入 ⇒ **必须有进程卡住**
    （本仓纪律：正控不红不算数——一条从没红过的判据不是判据）；
  · **绿**：真实现 ⇒ 4/4 在预算内成功、迁移跑到底、**没有留下无效索引**。

## 第二件实测撞出来的事：**杀掉客户端 ≠ PG 那边停了**

红基线要把 4 个子进程 SIGKILL 掉，而它们的 PG 后端**不会跟着退出**——正在跑的
`CREATE INDEX CONCURRENTLY` 继续跑、继续拿着表上的 `ShareUpdateExclusiveLock`；卡在
`pg_advisory_lock` 上的那个会在持锁者会话终于消失之后**接着把迁移跑完**。第一版没清这些
僵尸会话，于是**① 之后 ② 全卡住**（两个 CIC 后端互等表锁），红的像是真实现。

所以每一相之间走 `_quiesce()`：把这类会话 `pg_terminate_backend` 掉，再等这个库真的静下来
（没有别的会话 + 咨询锁已放开）才往下走。**这一条不是防御性编程**——它和本件要判的那个
死锁是同一个机制的另一副面孔：进程死了，数据库那边的活还在。

⚠️ 同一件事还有一个副作用：① 里偶发有子进程打完 `OK` 却仍然"卡住"（池的非守护线程拖着
解释器不退出，不是建表没跑完）。所以红基线真正的硬判据是**迁移没跑到底**（`max(v) < N`），
不是"卡住几个进程"。

⚠️ **它会 `DROP TABLE`**（那正是"第一次启动"的前提）。所以它**只认一个专门的测试库**：
DSN 指到产线库（`saudade_blog`）上**当场拒跑**。

用法：
  SAUDADE_CKPT_TEST_DSN='postgresql://…@127.0.0.1:5432/saudade_it' \\
      .venv/bin/python tests/test_checkpoint_setup_concurrent.py

**没有那个变量时这一件不报绿，它报「未评估」**（一整块大字），并且
`SAUDADE_REQUIRE_CKPT_TEST=1` 会把"未评估"变成失败——本仓把"没量"与"量了是绿的"
分得很开（同 `tests/_parent_repo.py` 那条：找不到父仓就响亮地说"没验"，不许静默算过）。
那一条自己每次运行都另打一行**每次运行都报**这一层量没量。
"""


from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


DSN = os.environ.get("SAUDADE_CKPT_TEST_DSN", "").strip()


STRICT = bool(os.environ.get("SAUDADE_REQUIRE_CKPT_TEST"))


_WARM_TIMEOUT = 60.0     # 等四个子进程把池开好（冷 import 最慢的那一段）


_TABLES = ("checkpoint_migrations", "checkpoint_writes", "checkpoint_blobs", "checkpoints")


FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {desc}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _skip(why: str) -> None:
    """前提不可用 ⇒ **报「未评估」，不报绿**。"""
    print("=" * 78)
    print("⏭  未评估：断点恢复的**真连库 · 4 进程并发建表**那一层")
    print(f"   原因：{why}")
    print("   ⚠️ 20261018 那次真停服（4 个 worker 无限期卡死、8010 不监听、restart 卡住）")
    print("     正是死在这一层，而当时所有套件全绿 —— 别再让它无声地没量。")
    print("   跑法：")
    print("     SAUDADE_CKPT_TEST_DSN='postgresql://…@127.0.0.1:5432/saudade_it' \\")
    print("         .venv/bin/python tests/test_checkpoint_setup_concurrent.py")
    print("=" * 78)
    if STRICT:
        print("SAUDADE_REQUIRE_CKPT_TEST=1 ⇒ 未评估算失败（本次退出码 1）")
        raise SystemExit(1)
    if "pytest" in sys.modules:
        # pytest 轨：`SystemExit` 在任何嵌套深度都被判为**失败**（黄要显式 skip 才是黄）。
        # `allow_module_level=True` 是必需的 —— 本函数**既在 import 期被调**（psycopg 没装 /
        # 连不上），**也在驱动里被调**（没给 DSN）；不给这个开关，前者会退化成
        # `ERROR ... Interrupted: 1 error during collection`（**红**，不是黄）。
        # 直跑轨照旧走下面的 `raise SystemExit(0)`，语义不变。
        import pytest  # noqa: PLC0415
        pytest.skip(why, allow_module_level=True)
    raise SystemExit(0)


try:
    import psycopg
except ImportError as e:                                       # pragma: no cover
    _skip(f"psycopg 没装（{e}）")


try:
    from langgraph.checkpoint.postgres.base import MIGRATIONS
except ImportError as e:                                       # pragma: no cover
    _skip(f"langgraph 的 PG 后端没装（{e}）")


try:
    CONN = psycopg.connect(DSN, autocommit=True, connect_timeout=5)
except Exception as e:
    _skip(f"连不上（{type(e).__name__}: {e}）")


_TMP = Path(tempfile.mkdtemp(prefix="ckpt_conc_"))


_GO = _TMP / "go"


def _drop_all() -> None:
    """把 checkpoint 那几张表删干净 = 摆出"第一次启动"那个前提。"""
    for t in _TABLES:
        CONN.execute(f"DROP TABLE IF EXISTS {t} CASCADE")


def _max_migration() -> int:
    row = CONN.execute("SELECT coalesce(max(v), -1) FROM checkpoint_migrations").fetchone()
    return int(row[0])


def _invalid_indexes() -> list[str]:
    rows = CONN.execute(
        "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE NOT i.indisvalid").fetchall()
    return sorted(r[0] for r in rows)


def _lock_free() -> bool:
    """咨询锁现在没人拿着吗。**用完立刻关连接**（会话级锁随会话结束释放）。"""
    from agent.checkpoint import _SETUP_LOCK
    with psycopg.connect(DSN, autocommit=True, connect_timeout=5) as c:
        row = c.execute("SELECT pg_try_advisory_lock(%s) AS got", (_SETUP_LOCK,)).fetchone()
        return bool(row[0])


def _dump_activity(tag: str) -> None:
    """把 PG 那边现在的形状打出来。**卡住时唯一能指方向的东西**。

    20261018 那次生产停服就是靠这份读数定性的（谁在等谁、等的是什么事件）。这一件自己
    卡住时也打一份——不然读的人只知道"红了"，还得自己去猜是锁没放、还是 CIC 在等旧快照。
    """
    print(f"     ── 现场（{tag}）：pg_stat_activity ──")
    rows = CONN.execute(
        "SELECT pid, state, wait_event_type, wait_event, left(query, 70) AS q "
        "FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "ORDER BY pid").fetchall()
    for pid, state, wt, we, q in rows:
        print(f"        pid={pid} state={state} wait={wt or '-'}/{we or '-'} :: {q}")
    if not rows:
        print("        （除了我自己，这个库上没有任何会话）")
    locks = CONN.execute(
        "SELECT locktype, classid, objid, granted, count(*) FROM pg_locks "
        "WHERE locktype IN ('advisory', 'virtualxid', 'relation') "
        "GROUP BY 1, 2, 3, 4 ORDER BY 1, 4").fetchall()
    print(f"     ── 现场（{tag}）：pg_locks ──")
    for lt, cid, oid, granted, n in locks:
        print(f"        {lt} classid={cid} objid={oid} granted={granted} ×{n}")


def _quiesce(tag: str) -> None:
    """把上一相留在 PG 那边的**僵尸会话**清掉，并等这个库真的静下来。

    ⚠️ **这一条是实测撞出来的，不是防御性编程**：**杀掉客户端进程 ≠ PG 那边停了**。
    子进程被 SIGKILL 之后，它的 PG 后端不会立刻退出——正在跑的那条
    `CREATE INDEX CONCURRENTLY` 还在跑、还拿着表上的 `ShareUpdateExclusiveLock`；
    另一个卡在 `pg_advisory_lock` 上的后端会在持锁者的会话终于消失之后**接着把迁移跑完**。
    20261018 本机实测现场（`pg_stat_activity`，① 之后）：

    ```
    1 条 active 卡在 Lock/advisory 跑 SELECT pg_advisory_lock($1)   ← ① 的输家，还活着
    2 条 active 各跑一句 CREATE INDEX CONCURRENTLY（一个等 virtualxid、一个等 relation）
    ```

    两个 CIC 后端互等表锁 ⇒ **② 全卡住**，而那不是真实现的问题，是上一相没清干净。
    （顺带说明：① 里偶发有子进程打完 `OK` 却仍"卡住"——那是池的非守护线程拖着解释器
    不退出，不是建表没跑完；所以红基线真正的硬判据是**迁移没跑到底**，不是"卡住几个"。）
    """
    rows = CONN.execute(
        "SELECT pid, state, wait_event, left(query, 50) AS q FROM pg_stat_activity "
        " WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "   AND (query ILIKE '%checkpoint%' OR wait_event = 'advisory' "
        "        OR query ILIKE '%CREATE INDEX CONCURRENTLY%')").fetchall()
    for pid, state, we, q in rows:
        CONN.execute("SELECT pg_terminate_backend(%s)", (pid,))
        print(f"     · 清掉一个上一相留下的僵尸会话 pid={pid} state={state} "
              f"wait={we or '-'} :: {q}")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 20.0:
        busy = CONN.execute(
            "SELECT count(*) FROM pg_stat_activity "
            " WHERE datname = current_database() AND pid <> pg_backend_pid()").fetchone()[0]
        if busy == 0 and _lock_free():
            return
        time.sleep(0.2)
    print(f"     ⚠️ {tag}：20s 内这个库没静下来（还有 {busy} 个别的会话，"
          f"锁{'还被拿着' if not _lock_free() else '已放开'}）——下面的判据可能因此假红")


_BLOCKING = (
    "# 把建表那一步换回**阻塞版**（20261018 上线时的那个版本，逐字）\n"
    "def _blocking(saver, pool=None):\n"
    "    pool = C._pool_for(C.settings.checkpoint_dsn) if pool is None else pool\n"
    "    with pool.connection() as conn:\n"
    "        conn.execute('SELECT pg_advisory_lock(%s)', (C._SETUP_LOCK,))\n"
    "        try:\n"
    "            saver.setup()\n"
    "        finally:\n"
    "            conn.execute('SELECT pg_advisory_unlock(%s)', (C._SETUP_LOCK,))\n"
    "C._setup_under_lock = _blocking\n"
)


def _child_code(blocking: bool) -> str:
    """子进程源码。A/B 两臂**只差中间那一段 monkeypatch**，其余逐字相同。"""
    return (
        "import os, time\n"
        "import agent.checkpoint as C\n"
        # 预热：把最慢的几步（模块 import + 开池）都挪到同步点之前。`build_checkpointer`
        # 里那句 `importlib.import_module('langgraph.checkpoint.postgres')` 尤其重
        # ——不预热的话它的耗时差会盖过 CIC 那个窗口，红基线就又复现不出来了。
        "import langgraph.checkpoint.postgres\n"
        "C._pool_for(C.settings.checkpoint_dsn)\n"
        + (_BLOCKING if blocking else "")
        + "open(os.environ['CKPT_WARM'], 'w').write('1')\n"
        "while not os.path.exists(os.environ['CKPT_GO']):\n"
        "    time.sleep(0.005)\n"
        "s = C.build_checkpointer()\n"
        "print('OK' if s is not None else 'NONE')\n"
    )


def _run_batch(blocking: bool, timeout: float) -> tuple[list[tuple[bool, int, str]], int]:
    """起 4 个子进程，等它们都预热好，放行，收集结果。

    返回 `(每进程的 (超时了吗, 退出码, 输出), 预热成功几个)`。
    """
    env = dict(os.environ)
    # **前提自己摆，不继承运维取值**：DSN 由这一件给，`.env` 一律不读
    # （不然"跑的是哪个库"取决于这台机器上那份 .env）。
    env["CHECKPOINT_DSN"] = DSN
    env["SAUDADE_IGNORE_ENV_FILE"] = "1"
    env["CKPT_GO"] = str(_GO)
    if _GO.exists():
        _GO.unlink()
    warm = [_TMP / f"warm_{i}" for i in range(4)]
    for w in warm:
        if w.exists():
            w.unlink()
    procs = []
    for i in range(4):
        e = dict(env, CKPT_WARM=str(warm[i]))
        procs.append(subprocess.Popen([sys.executable, "-c", _child_code(blocking)],
                                      cwd=str(ROOT), env=e, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True))
    # 等预热（最慢的那一段：冷 import + 开池）
    t0 = time.monotonic()
    while not all(w.exists() for w in warm) and time.monotonic() - t0 < _WARM_TIMEOUT:
        time.sleep(0.05)
    warmed = sum(1 for w in warm if w.exists())
    _GO.write_text("1")                       # 放行：四个同时往下走
    deadline = time.monotonic() + timeout
    out: list[tuple[bool, int, str]] = []
    for p in procs:
        try:
            text, _ = p.communicate(timeout=max(0.2, deadline - time.monotonic()))
            out.append((False, p.returncode, text or ""))
        except subprocess.TimeoutExpired:
            p.kill()
            text, _ = p.communicate()
            out.append((True, p.returncode, text or ""))
    return out, warmed


def _summary(rows: list[tuple[bool, int, str]]) -> str:
    return "；".join(f"{'卡住' if t else rc}" for t, rc, _ in rows)


def test_checkpoint_setup_concurrent():
    _TIMEOUT = 45.0          # 绿：同步放行之后整批的上限（真实现下几秒就完）
    _RED_TIMEOUT = 25.0      # 红基线：阻塞版**永远不返回**，够判"卡住"了
    if not DSN:
        _skip("没有 SAUDADE_CKPT_TEST_DSN")
    _DB = DSN.rsplit("/", 1)[-1].split("?", 1)[0]
    if not _DB or "saudade_blog" in _DB:
        _skip(f"DSN 指向的库是 {_DB!r}——本件会 **DROP TABLE**，拒绝在产线库上跑")
    CONN.execute("SET lock_timeout = '15s'")
    print(f"测试库 = {_DB}（本件会 DROP TABLE；产线库已拒）")
    _n = len(MIGRATIONS) - 1      # 从 langgraph 现取，不手抄那个数
    print(f"\n① 红基线：建表那一步换回**阻塞版** ⇒ 必须有进程卡住（{_RED_TIMEOUT:.0f}s）")
    _drop_all()
    _red, _warmed = _run_batch(blocking=True, timeout=_RED_TIMEOUT)
    _hung = sum(1 for t, _, _ in _red if t)
    _red_v = _max_migration()
    check("4 个进程都预热到同步点了（否则下面的'卡住'可能是别的原因）", _warmed == 4,
          f"预热 {_warmed}/4")
    check(f"阻塞版下有进程**没能在 {_RED_TIMEOUT:.0f}s 内返回**（那就是死锁的形状）",
          _hung >= 1, f"卡住 {_hung}/4；退出码 {[rc for _, rc, _ in _red]}")
    check("  **不止一个**卡住 ⇒ 是环（持锁那个也在等 CIC 等的那些事务），不是单个进程自己慢",
          _hung >= 2, f"卡住 {_hung}/4")
    check(f"  阻塞版下**迁移没跑到底**（`max(v)` = {_red_v} < {_n}）", _red_v < _n,
          f"实测 {_red_v}")
    print("     · 现场形状：持锁者在跑 `CREATE INDEX CONCURRENTLY`，其余卡在 `pg_advisory_lock`")
    time.sleep(1.0)          # 让被 kill 的会话在 PG 那边收尸
    _quiesce("① 之后")
    _drop_all()              # 清掉被掐断留下的半截（含无效索引）
    print(f"\n② 绿：真实现 ⇒ 4 个进程同时在**空库**上建表（{_TIMEOUT:.0f}s 预算）")
    _green, _warmed2 = _run_batch(blocking=False, timeout=_TIMEOUT)
    if any(t for t, _, _ in _green):
        _dump_activity("② 真实现下卡住了")
    check("4 个进程都预热到同步点了", _warmed2 == 4, f"预热 {_warmed2}/4")
    check("4/4 都在预算内正常退出且打印 OK",
          all((not t) and rc == 0 and "OK" in out for t, rc, out in _green),
          _summary(_green))
    check(f"（对照）同一套脚手架在真实现下 0/4 卡住，而阻塞版卡住 {_hung}/4 ⇒ 差异归因于锁",
          _hung >= 1 and not any(t for t, _, _ in _green), f"阻塞版 {_hung}/4")
    check(f"迁移跑到底了（`max(v)` = {_n}）", _max_migration() == _n, f"实测 {_max_migration()}")
    check("没有留下**无效索引**（被打断的 CIC 会留下一个，而 `IF NOT EXISTS` 永远跳过它）",
          _invalid_indexes() == [], str(_invalid_indexes()))
    check("咨询锁已经放开（没把锁带进下一个请求）", _lock_free())
    print(f"\n③ 已经建好之后再 4 个进程同时来一次（= 重启的形状，{_TIMEOUT:.0f}s 预算）")
    _quiesce("② 之后")
    _g2, _ = _run_batch(blocking=False, timeout=_TIMEOUT)
    if any(t for t, _, _ in _g2):
        _dump_activity("③ 真实现下卡住了")
    check("4/4 仍然正常退出（幂等那一支：全都看到版本号、直接跳过）",
          all((not t) and rc == 0 and "OK" in out for t, rc, out in _g2), _summary(_g2))
    check("  且没有重复跑迁移（版本号没被再插一遍）", _max_migration() == _n,
          f"实测 {_max_migration()}")
    shutil.rmtree(_TMP, ignore_errors=True)
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项 ❌：" + "；".join(FAILS))
        raise SystemExit(1)
    print("全部通过 ✅")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_checkpoint_setup_concurrent()
