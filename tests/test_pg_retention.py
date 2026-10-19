# -*- coding: utf-8 -*-
"""PG 保留治理的判据（20261019 批 C）：**纯函数 + 一个很小的假连接**，零真库、零网络。

**这一件为什么存在**：`eval/pg_retention.py` 是本仓唯一会 **DELETE 生产对话数据**的东西。
它的失败面不是"少删几行"，是**删错**——而删错至少有三种，**三种都不报错**：

  ① 把"取不到时间"的线程当成"很老" ⇒ 一次 `checkpoint->>'ts'` 的形态漂移变成一次
     **全库删除**（`checkpoints` 没有时间列，时间只住在那个 JSONB 里）；
  ② 只删 `checkpoints` 主表 ⇒ 孤儿 blob/writes 留着。**这三张表之间一条外键都没有**
     （实测 `pg_constraint` 里 `conrelid LIKE 'checkpoint%'` 是空的）⇒ 数据库不会替我们
     级联，也不会报错；
  ③ 两个 DELETE 不在一个事务里 / 断言在 commit 之后 ⇒ 半裁的状态落库
     （留下 checkpoint 却删了账，正是 ADR-0012 要堵的那个洞）。

所以判据钉的是**分类规则**与**事务形状**，不是"删了多少行"。被测七块，每条都配了会红
的那一半（本仓纪律：正控不红不算数）：

  · ① `cutoff_of`：带时区；**边界恰好等于 cutoff ⇒ 不裁**（严格小于）；
  · ② `plan_rows` 的四档分类 + 预算（超了只裁**最老的**那批）+ 顺序；
  · ③ **反控**：把"取不到时间"那一支并进"该裁" ⇒ 判据当场红；
  · ④ `apply_plan` 的语句形状：BEGIN → 三张表各一条 DELETE → 账一条 DELETE →
    断言 → COMMIT；
  · ⑤ **反控**：吞掉某一张表的 DELETE（模拟"漏删一张表"）⇒ **抛**，且记录里**没有 COMMIT**；
  · ⑥ **反控**：把 `with conn.transaction():` 换成 `if True:`（去掉事务）⇒ ④ 那条形状
    断言必须红；
  · ⑦ **反控**：把 `_assert_clean(...)` 那一行换成 `None` ⇒ ⑤ 那条"必须抛"必须红；
  · ⑧ 账表不在 ⇒ 不删、不报错、也**不去查它**；表清单与 langgraph 的迁移对得上
    （langgraph 哪天多一张 `checkpoint*` 表，这一条会红）。

用法：.venv/bin/python tests/test_pg_retention.py
"""
import importlib.util
import pathlib
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import pg_retention as R  # noqa: E402

SRC = ROOT / "eval" / "pg_retention.py"

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ── 一个很小的假连接：只认 `pg_retention` 真会发的那几条语句 ──────────────────
class _Cur:
    def __init__(self, rows, rowcount=-1):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Tx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        self._conn.log.append("BEGIN")
        return self

    def __exit__(self, et, ev, tb):
        self._conn.log.append("COMMIT" if et is None else "ROLLBACK")
        return False


class FakeConn:
    """内存模型：`present` = 每张线程表还剩几行；`ledger_rows` = 账还剩几行。

    **DELETE 真的会把它改小**（不是恒回 0）：否则"漏删一张表"这个反控根本摆不出来——
    而那正是 ⑤ 要判的那件事。`ignore_delete` 用来模拟"这条 DELETE 没生效"。
    """

    def __init__(self, plan_rows, *, present=None, ledger_rows=0, ledger_exists=False,
                 ignore_delete=()):
        self.plan_rows = plan_rows
        self.present = dict(present or {t: 0 for t in R.THREAD_TABLES})
        self.ledger_rows = ledger_rows
        self.ledger_exists = ledger_exists
        self.ignore_delete = set(ignore_delete)
        self.log: list[str] = []

    def transaction(self):
        return _Tx(self)

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.log.append("SQL:" + s)
        if s.startswith("SELECT to_regclass"):
            name = params[0]
            ok = self.ledger_exists if name == R.LEDGER_TABLE else name in self.present
            return _Cur([(ok,)])
        if s.startswith("SELECT thread_id,"):
            return _Cur(self.plan_rows)
        if s.startswith(f"SELECT count(*) FROM {R.LEDGER_TABLE}"):
            return _Cur([(self.ledger_rows,)])
        if s.startswith("DELETE FROM "):
            table = s.split()[2]
            if table == R.LEDGER_TABLE:
                n = 0 if table in self.ignore_delete else self.ledger_rows
                self.ledger_rows -= n
                return _Cur([], n)
            before = self.present.get(table, 0)
            n = 0 if table in self.ignore_delete else before
            self.present[table] = before - n
            return _Cur([], n)
        if s.startswith("SELECT count(*) FROM "):
            table = s.split()[3]
            return _Cur([(self.present.get(table, 0),)])
        raise AssertionError(f"假连接不认识这条语句：{s[:90]}")


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
CUT = R.cutoff_of(30, NOW)                     # 2026-09-19T12:00Z
_OLD = datetime(2026, 8, 1, tzinfo=timezone.utc)
_NEW = datetime(2026, 10, 18, tzinfo=timezone.utc)
ROWS = [
    ("conv_old", 12, _OLD, 0),                 # 该裁
    ("conv_edge", 3, CUT, 0),                  # **恰好等于 cutoff ⇒ 不裁**
    ("conv_new", 40, _NEW, 0),                 # 窗内
    ("conv_undated", 5, None, 5),              # 取不到时间 ⇒ 永不裁
]


def _ids(rows: list[dict]) -> list[str]:
    return [r["thread_id"] for r in rows]


def _mk_conn(**kw) -> FakeConn:
    kw.setdefault("present", {"checkpoints": 20, "checkpoint_blobs": 31, "checkpoint_writes": 94})
    kw.setdefault("ledger_rows", 7)
    kw.setdefault("ledger_exists", True)
    return FakeConn(ROWS, **kw)


print("① `cutoff_of`：带时区、边界是**严格小于**")
check("cutoff = now - keep_days（且带时区，与 timestamptz 同族）",
      R.cutoff_of(30, NOW) == NOW - timedelta(days=30) and R.cutoff_of(30, NOW).tzinfo is not None,
      str(R.cutoff_of(30, NOW)))
check("keep_days 是浮点也认（--keep-days 7.5）",
      R.cutoff_of(7.5, NOW) == NOW - timedelta(days=7.5))

print("\n② `plan_rows` 的四档分类 + 预算")
_pl = R.plan_rows(ROWS, CUT)
check("该裁的只有 conv_old", _ids(_pl["prune"]) == ["conv_old"], str(_ids(_pl["prune"])))
check("**恰好等于 cutoff 的不裁**（`<` 不是 `<=`：边界上宁可多留一天）",
      "conv_edge" in _ids(_pl["keep"]) and "conv_edge" not in _ids(_pl["prune"]))
check("窗内的留下", "conv_new" in _ids(_pl["keep"]))
check("**取不到时间的进 undated、不进 prune**（判据缺失时不动手）",
      _ids(_pl["undated"]) == ["conv_undated"]
      and "conv_undated" not in _ids(_pl["prune"]))
_more = ROWS + [("conv_older", 2, datetime(2026, 5, 1, tzinfo=timezone.utc), 0)]
_pl2 = R.plan_rows(_more, CUT, max_threads=1)
check("预算：超了只裁**最老的**那批（从老到新取前 N）",
      _ids(_pl2["prune"]) == ["conv_older"] and _ids(_pl2["deferred"]) == ["conv_old"],
      f"prune={_ids(_pl2['prune'])} deferred={_ids(_pl2['deferred'])}")
check("预算不把 undated 算进去（它根本不参与排队）",
      _ids(_pl2["undated"]) == ["conv_undated"])
check("prune 的顺序是从老到新（报告里先看到最老的）",
      [r["last_at"] for r in _pl2["prune"]] == sorted(r["last_at"] for r in _pl2["prune"]))


# ── 变异体：把源码改一处，再跑同一组判据 ─────────────────────────────────────
def _mutant(old: str, new: str):
    """把 `eval/pg_retention.py` 里**唯一**一处 `old` 换成 `new`，加载成模块。

    变异点是**源码文本**（同族做法见 `tests/test_wordlist_gate.py`）：判据要能证明自己
    会红，而"改一处源码 ⇒ 同一次调用变成另一个结局"是唯一能证这件事的办法。
    """
    src = SRC.read_text(encoding="utf-8")
    assert src.count(old) == 1, f"变异点不唯一（{src.count(old)} 处）：{old!r}"
    d = pathlib.Path(tempfile.mkdtemp(prefix="pgret-mut-"))
    p = d / "mut.py"
    p.write_text(src.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("pg_retention_mut", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _classify(mod) -> tuple[list[str], list[str]]:
    pl = mod.plan_rows(ROWS, CUT)
    return _ids(pl["prune"]), _ids(pl["undated"])


print("\n③ 反控：把「取不到时间」并进「该裁」那一支 ⇒ ② 那两条必须红")
# 变异体就是那条真会写出来的错法：`if last_at is None or last_at < cutoff:` ——
# 一行之差，`checkpoint->>'ts'` 哪天形态变了就是**全库删除**。`last_at or cutoff`
# 是为了让变异体自己别抛（抛了也能算红，但"分类错了"比"崩了"更接近那个真实失效）。
_mut3 = _mutant(
    '        if last_at is None:\n'
    '            undated.append({"thread_id": tid, "rows": rows_n, "undated_rows": n_undated})\n'
    '        elif last_at < cutoff:\n',
    '        if last_at is None or last_at < cutoff:\n'
    '            prune.append({"thread_id": tid, "rows": rows_n, "last_at": last_at or cutoff})\n'
    '        elif False:\n')
try:
    _p3, _u3 = _classify(_mut3)
    _red3 = ("conv_undated" in _p3) or ("conv_undated" not in _u3)
except Exception as e:                                    # 变异体抛了也算红
    _red3, _p3, _u3 = True, [], [f"<{type(e).__name__}>"]
check("变异体上「取不到时间的线程」确实不再被判成 undated（⇒ ② 会红）",
      _red3, f"prune={_p3} undated={_u3}")

print("\n④ `apply_plan` 的语句形状：全在一个事务里，断言在 commit 之前")
_conn = _mk_conn()
_pl4 = R.scan(_conn, CUT, 200)
_conn.log.clear()                                        # scan 是只读的，形状只看 apply
_res4 = R.apply_plan(_conn, _pl4)
_dels = [i for i, e in enumerate(_conn.log) if e.startswith("SQL:DELETE FROM")]
_cnts = [i for i, e in enumerate(_conn.log) if e.startswith("SQL:SELECT count(*)")]
check("事务是**一个**：BEGIN 在头、COMMIT 在尾，中间没有第二个 BEGIN",
      _conn.log[0] == "BEGIN" and _conn.log[-1] == "COMMIT"
      and _conn.log.count("BEGIN") == 1, str(_conn.log[:2] + _conn.log[-2:]))
check("三张线程表**各一条** DELETE + 账一条 = 4 条（少一张就是孤儿数据）",
      len(_dels) == 4, f"{len(_dels)} 条："
      f"{[e.split('DELETE FROM ')[1].split(' ')[0] for e in _conn.log if 'DELETE FROM' in e]}")
check("四条 DELETE 全在事务里（不在 BEGIN 之前、也不在 COMMIT 之后）",
      all(0 < i < len(_conn.log) - 1 for i in _dels))
check("断言（那几条 count）**在删除之后、COMMIT 之前**",
      bool(_cnts) and min(_cnts) > max(_dels) and max(_cnts) < len(_conn.log) - 1,
      f"delete@{_dels} count@{_cnts} 末尾@{len(_conn.log) - 1}")
check("真删干净了（假连接的行数也归零）",
      all(v == 0 for v in _conn.present.values()) and _conn.ledger_rows == 0,
      f"{_conn.present} ledger={_conn.ledger_rows}")
check("返回值如实报了每张表删了几行",
      _res4["rows"] == {"checkpoints": 20, "checkpoint_blobs": 31, "checkpoint_writes": 94}
      and _res4["ledger"] == 7, str(_res4))


def _shape_ok(log: list[str]) -> bool:
    """④ 那组形状断言（抽出来给变异体复用）。"""
    dels = [i for i, e in enumerate(log) if e.startswith("SQL:DELETE FROM")]
    cnts = [i for i, e in enumerate(log) if e.startswith("SQL:SELECT count(*)")]
    return (bool(log) and log[0] == "BEGIN" and log[-1] == "COMMIT"
            and log.count("BEGIN") == 1 and len(dels) == 4
            and all(0 < i < len(log) - 1 for i in dels)
            and bool(cnts) and min(cnts) > max(dels) and max(cnts) < len(log) - 1)


def _probe_shape(mod) -> bool:
    try:
        conn = _mk_conn()
        pl = mod.scan(conn, CUT, 200)
        conn.log.clear()
        mod.apply_plan(conn, pl)
        return _shape_ok(conn.log)
    except Exception:                                    # 变异体抛了也算红
        return False


print("\n⑤ 反控：某一张表的 DELETE 被吞掉（= 漏删一张表）⇒ 必须**抛**且**不提交**")


def _probe_raises(mod) -> bool:
    """跑一遍"漏删一张表"；True = 抛了**且**没有 COMMIT。"""
    conn = _mk_conn(ignore_delete={"checkpoint_blobs"})
    try:
        pl = mod.scan(conn, CUT, 200)
        conn.log.clear()
        mod.apply_plan(conn, pl)
    except RuntimeError as e:
        return "COMMIT" not in conn.log and "checkpoint_blobs" in str(e)
    return False


check("漏删一张表 ⇒ RuntimeError，且事务**回滚**（记录里没有 COMMIT）",
      _probe_raises(R))
_conn5 = _mk_conn()
_pl5 = R.scan(_conn5, CUT, 200)
_conn5.log.clear()
try:
    R.apply_plan(_conn5, _pl5)
    _ok5 = "COMMIT" in _conn5.log
except RuntimeError:
    _ok5 = False
check("（对照）真实现下同一组输入照常提交 —— ⇒ 上面那条的红只可能来自那个断言", _ok5)

print("\n⑥ 反控：把 `with conn.transaction():` 换成 `if True:`（去掉事务）⇒ ④ 必须红")
check("变异体上 ④ 那组形状断言不成立",
      _probe_shape(_mutant("with conn.transaction():", "if True:")) is False)
check("（对照）真实现上同一组断言成立 —— ⇒ 上面那条的红只可能来自「没有事务」",
      _probe_shape(R) is True)

print("\n⑦ 反控：把 `_assert_clean(...)` 换成 `None`（断言不跑）⇒ ⑤ 必须红")
check("变异体上「漏删一张表」不再抛（⇒ ⑤ 会红）",
      _probe_raises(_mutant('_assert_clean(conn, threads, cutoff, pl["ledger"]["exists"])',
                            'None')) is False)

print("\n⑧ 账表不在 ⇒ 不删、不报错、也不去查它")
_conn8 = _mk_conn(ledger_exists=False, ledger_rows=0)
_pl8 = R.scan(_conn8, CUT, 200)
check("`scan` 如实说这张表不存在（不是「账 0 行」）",
      _pl8["ledger"] == {"exists": False, "due": 0}, str(_pl8["ledger"]))
_conn8.log.clear()
_res8 = R.apply_plan(_conn8, _pl8)
check("照常删 checkpoint、**不碰账**（也没有对它发过任何语句）",
      _res8["ledger"] == 0 and len(_res8["rows"]) == 3
      and not any(R.LEDGER_TABLE in e for e in _conn8.log),
      str([e for e in _conn8.log if "DELETE" in e]))

print("\n⑨ 表清单与 langgraph 的迁移对得上（它哪天多一张 `checkpoint*` 表，这一条要红）")
try:
    from langgraph.checkpoint.postgres.base import BasePostgresSaver
    _names = set()
    for sql in BasePostgresSaver.MIGRATIONS:
        for m in re.finditer(r"CREATE TABLE IF NOT EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", sql):
            if m.group(1).startswith("checkpoint") and m.group(1) != "checkpoint_migrations":
                _names.add(m.group(1))
    check("langgraph 建的那几张数据表都在 `THREAD_TABLES` 里（`checkpoint_migrations` 除外——"
          "它记的是 schema 版本，不是数据）",
          _names and _names <= set(R.THREAD_TABLES), f"langgraph={sorted(_names)}")
    check("反过来也没有多写的表名（多写一张不存在的表 ⇒ 每次 DELETE 都报错）",
          set(R.THREAD_TABLES) <= _names, f"我们={sorted(R.THREAD_TABLES)}")
except ImportError as e:                                  # pragma: no cover
    check("langgraph 的 PG 后端装得上（这条判据的前提）", False, str(e))

print("\n⑩ 保留期那一个数在两处必须相等（登记表 vs 执行者缺省）")
import retention_manifest as RM  # noqa: E402

check("`retention_manifest.KEEP_DAYS_CHECKPOINT` == `pg_retention.KEEP_DAYS_DEFAULT`",
      RM.KEEP_DAYS_CHECKPOINT == R.KEEP_DAYS_DEFAULT,
      f"{RM.KEEP_DAYS_CHECKPOINT} vs {R.KEEP_DAYS_DEFAULT}")

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "；".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
