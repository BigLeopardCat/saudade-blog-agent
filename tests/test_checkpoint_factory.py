"""Check checkpoint backend selection, startup failures and nonblocking setup locks.

Uses fake connections and independent positive/negative controls; SQL checks do
not depend on historical commentary in the production module.
"""


import ast
import importlib.util
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


import agent.checkpoint as C  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


def _build_checkpointer(dsn=None):
    with ExitStack() as resources:
        return C.build_checkpointer(dsn, resources=resources)


def _raises(fn, *a, **kw) -> str:
    """调用并返回异常文本；没抛就返回空串。"""
    try:
        fn(*a, **kw)
    except Exception as e:                      # 判据要的就是"抛了"
        return f"{type(e).__name__}: {e}"
    return ""


def _not_none_or_raised(dsn: str) -> tuple[bool, str]:
    """给了 DSN 时的唯一合法结局：返回一个真 saver，或者抛。**静默返回 None 是错的。**"""
    try:
        return _build_checkpointer(dsn) is not None, "返回了 None"
    except Exception as e:
        return True, f"抛了（可以接受）：{type(e).__name__}"


from config import settings as _S  # noqa: E402

_imported: list[str] = []


_real_import = C.importlib.import_module


def _spy(name, *a, **kw):
    _imported.append(name)
    return _real_import(name, *a, **kw)


_real_import = C.importlib.import_module


def _no_module(name, *a, **kw):
    raise ImportError(f"No module named {name!r}")


def _defaults_of(fn: ast.FunctionDef) -> dict:
    """参数名 → 缺省值节点（没有缺省值的不在表里）。

    位置参数与 kw-only 的对齐规则不同（`defaults` 只盖位置参数**尾部**，`kw_defaults`
    与 `kwonlyargs` 一一对应、其中"没有缺省值"占位是 `None`）——手算下标最容易在这里错，
    所以先把它摊成一张表再用。
    """
    out = {}
    pos = fn.args.posonlyargs + fn.args.args
    for a, d in zip(pos[len(pos) - len(fn.args.defaults):], fn.args.defaults):
        out[a.arg] = d
    for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
        if d is not None:
            out[a.arg] = d
    return out


def _callee_name(node: ast.expr) -> str | None:
    """被调方**按名字**认：`g.compile(...)` → `compile`，`build_graph(...)` → `build_graph`。

    锁不认"是属性还是裸名"（两者都合法），只认名字——这样同一条锁能同时钉住
    `build_graph` 里那一处 `.compile()` 与 `create_agent` 里那一处 `build_graph()`。
    """
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _wire_lock(src: str, fname: str, callee: str) -> list[str]:
    """接线锁：**违规描述**列表（空 = 合规）。红基线用它证明锁抓得住那几种坏法。

    判据 = 三件事同时成立，合起来是"saver 只能由调用方带进来，这一层自己不造、不写死、
    也不把它丢掉"：① `fname` 里恰好一处 `callee(...)`；② 那一处**只**带一个关键字参数
    `checkpointer=`，值**恰好是同名参数**（不是 `MemorySaver()`、不是 `build_checkpointer(...)`）；
    ③ 该参数缺省 `None`（不传 ⇒ 无持久化）。
    """
    bad: list[str] = []
    fn = next((n for n in ast.parse(src).body
               if isinstance(n, ast.FunctionDef) and n.name == fname), None)
    if fn is None:
        return [f"找不到 `{fname}`（锚点没了 ⇒ 这条锁会空过）"]
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and _callee_name(n.func) == callee]
    if len(calls) != 1:
        bad.append(f"{len(calls)} 处 `{callee}(...)`（多出来 = 有人从别处把 saver 接上了）")
        return bad
    _d = _defaults_of(fn).get("checkpointer")
    if _d is None:
        bad.append("`checkpointer` 没有缺省值（必填 ⇒ 生产调用点会当场炸）")
    elif not (isinstance(_d, ast.Constant) and _d.value is None):
        bad.append(f"`checkpointer` 缺省不是 None：{ast.unparse(_d)}")
    _c = calls[0]
    _kw = [k for k in _c.keywords if k.arg]
    if _c.args or [k.arg for k in _kw] != ["checkpointer"]:
        bad.append(f"`{callee}` 不是「只带一个 checkpointer=」：{ast.unparse(_c)}")
    elif not (isinstance(_kw[0].value, ast.Name) and _kw[0].value.id == "checkpointer"):
        bad.append(f"`checkpointer=` 传的不是调用方那个名字：{ast.unparse(_c)}")
    return bad


def _scan(text: str) -> list[str]:
    """找出**代码**里对 MemorySaver 的引用（导入 / 名字 / 属性），返回 `行号: 形态`。

    **读 AST 不读文件文本**：本模块的头注里正当地写着"MemorySaver"这个词（解释它为什么
    被换掉），按字面扫会假红——判据的落点要选判据，别选字面。
    """
    out: list[str] = []
    for n in ast.walk(ast.parse(text)):
        if isinstance(n, ast.ImportFrom) and "checkpoint.memory" in (n.module or ""):
            out.append(f"{n.lineno}: import")
        elif isinstance(n, ast.Name) and n.id == "MemorySaver":
            out.append(f"{n.lineno}: Name")
        elif isinstance(n, ast.Attribute) and n.attr == "MemorySaver":
            out.append(f"{n.lineno}: Attr")
    return out


def _naive(cid):                    # 反控：缺席也照拼（上面那组必须抓得住它）
    return f"conv_{cid}"


class _Res:
    """`conn.execute(...)` 的返回：`.fetchone()` / `.fetchall()`。行是 dict
    （真池是 `row_factory=dict_row`，判据照那个形状喂，不喂元组——形状不对时
    判据该红，而不是被 `row[0]` 蒙过去）。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _sql_text(obj) -> str:
    """把 `execute` 的第一个参数渲染成文本。真驱动收 `str` 与 `sql.Composed` 两种，
    假连接两种都得认——`Composed` **没有 `__str__`**（`str()` 出来是 repr，
    于是"这条 SQL 是什么"根本读不出来）。`as_string(None)` 才给出真正会发出去的那句，
    标识符该加的引号也在里面。"""
    as_string = getattr(obj, "as_string", None)
    return as_string(None) if as_string else str(obj)


class _FakeConn:
    """假连接。`got_seq` 用尽之后**一直重复最后一个值**（好写"永远抢不到"那一支）。"""

    def __init__(self, got_seq, invalid=(), autocommit=True):
        self._got = list(got_seq)
        self._invalid = list(invalid)
        self.autocommit = autocommit
        self.lock_tries = 0
        self.unlocks = 0
        self.drops: list[str] = []

    def execute(self, sql, params=None):
        s = " ".join(_sql_text(sql).split())
        if "pg_try_advisory_lock" in s:
            self.lock_tries += 1
            val = self._got[0] if len(self._got) == 1 else self._got.pop(0)
            return _Res([{"got": val}])
        if "pg_advisory_unlock" in s:
            self.unlocks += 1
            return _Res([])
        if "pg_index" in s:
            return _Res([{"name": n} for n in self._invalid])
        if s.upper().startswith("DROP INDEX"):
            self.drops.append(s)
            return _Res([])
        if s.startswith(("CREATE TABLE IF NOT EXISTS agent_deleted_threads",
                         "CREATE TABLE IF NOT EXISTS agent_write_idem")):
            return _Res([])
        raise AssertionError(f"假连接收到了没预期的 SQL：{s}")


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, *exc):
                return False

        return _Ctx()


class _Saver:
    def __init__(self):
        self.calls = 0

    def setup(self):
        self.calls += 1


class _Clock:
    """假时钟：`now`/`sleep` 可注入就是为了这个——判据不真等 60 秒。"""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, d):
        self.t += d


_DOCSTRINGS = set()


def _collect_docstrings(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                _DOCSTRINGS.add(id(body[0].value))


def _advisory_literals(src: str) -> list[str]:
    """源码里所有含 `advisory_` 的**字符串字面**（**docstring 不算**——头注里正写着
    这件事，算进去的话这条锁会被自己的说明文字骗绿）。

    ⚠️ 找的是 `advisory_` 不是 `advisory_lock`：**解锁那条叫 `pg_advisory_unlock`**，
    它里面没有 `advisory_lock` 这个子串（第一版就是照后者找的，于是只捞到一条、
    判据红在自己的过滤词上）。"""
    _DOCSTRINGS.clear()
    tree = ast.parse(src)
    _collect_docstrings(tree)
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in _DOCSTRINGS and "advisory_" in node.value):
            out.append(" ".join(node.value.split()))
    return sorted(out)


def test_checkpoint_factory():
    C._POOL_WAIT = 0.5
    _DEAD = "postgresql://u:s3cret@127.0.0.1:1/blog"
    print("① 惰性：不给 DSN ⇒ None，且一次 import 都不发")
    _ambient_dsn = _S.checkpoint_dsn
    _S.checkpoint_dsn = ""
    check("（前提）DSN 已钉成空串——本节量的是「没配 DSN」那一支，不是这台机器 `.env` 的值",
          _S.checkpoint_dsn == "", repr(_S.checkpoint_dsn))
    C.importlib.import_module = _spy
    try:
        _none = _build_checkpointer()
        _blank = _build_checkpointer("   ")
    finally:
        C.importlib.import_module = _real_import
    check("空 DSN 返回 None（= 今天的行为）", _none is None, repr(_none))
    check("只有空白也当没配", _blank is None, repr(_blank))
    check("空 DSN **一次 import 都不发**（不是 import 完才发现没配）",
        _imported == [], str(_imported))
    _S.checkpoint_dsn = _ambient_dsn          # 本节只借那一格判"没配 DSN"那一支，还回去
    print("\n② 反控：给了 DSN 就绝不许静默返回 None")
    for _dsn in (_DEAD, "sqlite:////tmp/ckpt.sqlite3"):
        _ok, _why = _not_none_or_raised(_dsn)
        check(f"`{C.redact(_dsn)}` 不静默返回 None", _ok, _why)
    print("\n③ 认不出来的 scheme ⇒ 响亮，且点名「故意不支持内存档」")
    for _bad in ("memory://x", "memory", "mysql://root@127.0.0.1/blog", "redis://127.0.0.1"):
        _m = _raises(_build_checkpointer, _bad)
        check(f"`{_bad}` ⇒ RuntimeError", _m.startswith("RuntimeError:"), _m[:90] or "（没抛）")
    _mem = _raises(C.backend_for, "memory://x")
    check("内存档的拒绝理由写清楚了（不然人会以为只是没实现）",
          "故意不支持内存档" in _mem, _mem[:160])
    print("\n③b sqlite 这一支**刻意不落地**（产线 4 worker 共用一个文件库，并发写没验过）")
    _sq = _raises(_build_checkpointer, "sqlite:////tmp/ckpt.sqlite3")
    check("抛 RuntimeError（不是静默去造一个没验过的 saver）",
          _sq.startswith("RuntimeError:"), _sq[:90] or "（没抛）")
    check("说清是「没有落地」而不是含糊的「还没实现」", "没有落地" in _sq, _sq[:200])
    print("\n④ 合法 scheme 但后端没装 ⇒ 响亮，消息里有包名与 pip 命令")
    _pg_installed = importlib.util.find_spec("langgraph.checkpoint.postgres") is not None
    C.importlib.import_module = _no_module
    try:
        _pg = _raises(_build_checkpointer, _DEAD)
    finally:
        C.importlib.import_module = _real_import
    check("抛 RuntimeError", _pg.startswith("RuntimeError:"), _pg[:90] or "（没抛）")
    check("点名包名", "langgraph-checkpoint-postgres" in _pg, _pg[:200])
    check("给出 pip 命令", ".venv/bin/pip install" in _pg, _pg[:200])
    check("明说不回落成内存档", "不会静默回落成内存档" in _pg, _pg[:240])
    print("\n④b 后端已装 + DSN 连不上 ⇒ 响亮抛，**绝不返回一个坏 saver**")
    if not _pg_installed:
        print("  ⏭ 本机没装 langgraph-checkpoint-postgres ⇒ **这一条没验**（不是通过）")
    else:
        _down = _raises(_build_checkpointer, _DEAD)
        check("抛 RuntimeError（没抛 = 返回了一个连不上的 saver）",
              _down.startswith("RuntimeError:"), _down[:90] or "（没抛）")
        check("异常文本里的口令已脱敏", "s3cret" not in _down and "***@" in _down, _down[:200])
        check("明说不静默降级", "不静默降级" in _down, _down[:240])
    print("\n⑤ 接线锁：那一处 `.compile()` 只许把调用方给的 `checkpointer` 原样传下去")
    _BARE = "def build_graph(checkpointer=None):\n    return g.compile()\n"
    _HARD = ("def build_graph(checkpointer=None):\n"
         "    return g.compile(checkpointer=MemorySaver())\n")
    _DFLT = ("def build_graph(checkpointer=MemorySaver()):\n"
         "    return g.compile(checkpointer=checkpointer)\n")
    _EXTRA = ("def build_graph(checkpointer=None):\n"
          "    return g.compile(checkpointer=checkpointer, store=STORE)\n")
    _OK = ("def build_graph(checkpointer=None):\n"
       "    return g.compile(checkpointer=checkpointer)\n")
    check("红基线：抓得住裸 `g.compile()`（实验臂传进来的 saver 会被静默忽略）",
          bool(_wire_lock(_BARE, "build_graph", "compile")),
          str(_wire_lock(_BARE, "build_graph", "compile")))
    check("红基线：抓得住这一层现造 `MemorySaver()`",
          bool(_wire_lock(_HARD, "build_graph", "compile")),
          str(_wire_lock(_HARD, "build_graph", "compile")))
    check("红基线：抓得住缺省值被改成非 None",
          bool(_wire_lock(_DFLT, "build_graph", "compile")),
          str(_wire_lock(_DFLT, "build_graph", "compile")))
    check("红基线：抓得住多挂一个 `store=`",
          bool(_wire_lock(_EXTRA, "build_graph", "compile")),
          str(_wire_lock(_EXTRA, "build_graph", "compile")))
    check("反控：合规骨架不许红（不然这条锁只会恒红）",
          not _wire_lock(_OK, "build_graph", "compile"),
          str(_wire_lock(_OK, "build_graph", "compile")))
    check("反控：锚点没了要**报**出来，不许空过",
          bool(_wire_lock("x = 1\n", "build_graph", "compile")),
          str(_wire_lock("x = 1\n", "build_graph", "compile")))
    print("\n⑥ 旧陷阱不许回来：生产代码不直接构造 MemorySaver")
    check("红基线：检测器抓得住 `from langgraph.checkpoint.memory import MemorySaver`",
          bool(_scan("from langgraph.checkpoint.memory import MemorySaver\n")))
    check("红基线：抓得住 `MemorySaver()` 这种直接用",
          bool(_scan("x = MemorySaver()\n")))
    check("反控：只在注释/文档里提到它，不该报",
          not _scan('"""MemorySaver 是进程内的，见 §4.6。"""\nx = 1  # 不用 MemorySaver\n'))
    _hits: list[str] = []
    for _p in sorted((ROOT / "agent").glob("*.py")):
        _hits += [f"{_p.name}:{h}" for h in _scan(_p.read_text(encoding="utf-8"))]
    check("agent/*.py 的**代码**里零引用", not _hits, f"又出现在 {_hits}")
    print("\n⑦ 缺省参数取自 `settings.checkpoint_dsn`（不是第二个写死的空串）")
    _old_dsn = _S.checkpoint_dsn
    try:
        _S.checkpoint_dsn = _DEAD
        _loud = _raises(_build_checkpointer)
        # 拨成一个连不上的 DSN ⇒ 必须**响亮抛**。这就证明了那一格被真的读到了：
        # 没读到的话 `build_checkpointer()` 会拿空串直接返回 `None`（**静默**）。
        # 刻意不用"能连上的 DSN + 断言返回了 saver"——那要求本机有个真库，
        # 判据就变成了"本机有没有库"，而这条要判的是**接线通不通**。
        check("拨成 DSN ⇒ **不给参数**也响亮抛（不是静默返回 None ⇒ 那一格是活的）",
              _loud.startswith("RuntimeError:"), _loud[:90] or "（没抛 = 这一格是死的）")
        _S.checkpoint_dsn = "   "
        check("拨成纯空白 ⇒ 与 `build_checkpointer('   ')` 同一把尺子：None",
              _build_checkpointer() is None)
        _S.checkpoint_dsn = ""
        check("拨回空 ⇒ None（= 没挂 saver 时的行为）", _build_checkpointer() is None)
    finally:
        _S.checkpoint_dsn = _old_dsn
    print("\n⑧ 会话坐标与脱敏（批 7 新增的三个纯函数——**不连库，本机就能判**）")
    _ABSENT = (None, "", "   ", 0, "0", "None")
    check("会话 id 缺席的六种形态 ⇒ 全 None（**不编坐标**）",
          all(C.conversation_thread(x) is None for x in _ABSENT),
          str([C.conversation_thread(x) for x in _ABSENT]))
    check("（反控）「缺席也照拼」的实现在同一组输入上**不是全 None** ⇒ 上面那条不是空过",
          not all(_naive(x) is None for x in _ABSENT))
    check("真会话 id ⇒ conv_<id>（int 与 str 同形）",
          C.conversation_thread(42) == "conv_42" == C.conversation_thread("42"),
          f"{C.conversation_thread(42)!r} / {C.conversation_thread('42')!r}")
    check("挂了 saver + 有会话 ⇒ 会话坐标", C.thread_id_for(7, 42, True) == "conv_42",
          C.thread_id_for(7, 42, True))
    check("挂了 saver 但没会话 id ⇒ 退回每请求随机（**不编 conv_None**）",
          C.thread_id_for(7, None, True).startswith("user_7_"), C.thread_id_for(7, None, True))
    check("没挂 saver ⇒ 每请求随机（= 20260903 以来的行为）",
          C.thread_id_for(7, 42, False).startswith("user_7_"), C.thread_id_for(7, 42, False))
    check("随机的两条**不相同**（不是被改成常量线程）",
          C.thread_id_for(7, 42, False) != C.thread_id_for(7, 42, False))
    check("DSN 里的口令不进异常文本（异常会进 agent.log）",
          C.redact("postgresql://u:s3cret@h:5432/db") == "postgresql://***@h:5432/db",
          C.redact("postgresql://u:s3cret@h:5432/db"))
    check("没带口令的原样返回（不是把整条 DSN 都吃掉）",
          C.redact("postgresql://h:5432/db") == "postgresql://h:5432/db",
          C.redact("postgresql://h:5432/db"))
    print("\n⑨ 建表抢锁：轮询、有预算、池必须 autocommit（20261018 停服之后新增）")
    _c, _s = _FakeConn([False, False, True]), _Saver()
    _tries = C.wait_for_setup_lock(_c, now=lambda: 0.0, sleep=lambda d: None)
    check("抢到锁 ⇒ 返回试了几次（3 次），且 setup 还没被调（那一层不归它管）",
          _tries == 3 and _s.calls == 0 and _c.unlocks == 0, f"tries={_tries}")
    C._setup_under_lock(_s, pool=_FakePool(_c))
    check("`_setup_under_lock` 走完 ⇒ setup 恰好一次、解锁恰好一次",
          _s.calls == 1 and _c.unlocks == 1, f"setup={_s.calls} unlock={_c.unlocks}")
    _clock = _Clock()
    _c2, _s2 = _FakeConn([False]), _Saver()
    try:
        C.wait_for_setup_lock(_c2, now=_clock.now, sleep=_clock.sleep, budget=2.0)
        _msg = ""
    except RuntimeError as e:
        _msg = str(e)
    check("一直抢不到 ⇒ RuntimeError（**不是无限等**）", bool(_msg), _msg[:60])
    check("  消息里写清楚等了多久、试了几次（读日志的人要能一眼看出卡在哪）",
          "没抢到" in _msg and "2s" in _msg and "次" in _msg, _msg[:80])
    check("  它是**轮询**过的（tries ≥ 2）而不是一次就放弃 ⇒ 上面那条不是空过",
          _c2.lock_tries >= 2, f"tries={_c2.lock_tries}")
    check("（反控）预算给够 ⇒ 同一条路径**不抛**（上一条不是恒抛）",
          C.wait_for_setup_lock(_FakeConn([False, False, True]), now=_Clock().now,
                                sleep=lambda d: None, budget=5.0) == 3)
    _c3, _s3 = _FakeConn([False]), _Saver()
    _clock3 = _Clock()
    _t0 = time.monotonic()
    try:
        # ⚠️ `sleep` **必须是会走字的那个假钟**（`_clock3.sleep`），不能写 `lambda d: None`：
        # `wait_for_setup_lock` 的判据是 `now() >= deadline`，一个不前进的钟 ⇒ **死循环**。
        # ⑨b 那两个 `lambda d: None` 之所以没事，是因为那里最终**抢到了**锁、不走预算那条路。
        C._setup_under_lock(_s3, pool=_FakePool(_c3), now=_clock3.now,
                            sleep=_clock3.sleep, budget=2.0)
        _msg3 = ""
    except RuntimeError as e:
        _msg3 = str(e)
    _dt3 = time.monotonic() - _t0
    check("★ 这一格**不真等**：注入假钟之后整块墙钟 < 5 秒（透传断掉就会退化成真等 60 秒）",
          _dt3 < 5.0, f"实测 {_dt3:.2f}s（真等的话 ≈60s）")
    check("  且确实抛了（上面那条不是「没抛所以很快」）", bool(_msg3), _msg3[:60])
    check("没抢到锁 ⇒ 一次 setup 都不调、一次 unlock 都不发",
          _s3.calls == 0 and _c3.unlocks == 0, f"setup={_s3.calls} unlock={_c3.unlocks}")
    _c4, _s4 = _FakeConn([True], autocommit=False), _Saver()
    try:
        C._setup_under_lock(_s4, pool=_FakePool(_c4))
        _amsg = ""
    except RuntimeError as e:
        _amsg = str(e)
    check("池不是 autocommit ⇒ 响亮拒（消息里点名 autocommit 与那条环）",
          "autocommit" in _amsg and "CREATE INDEX CONCURRENTLY" in _amsg, _amsg[:60])
    check("  且**一条语句都没发**（拒在抢锁之前，不留半截状态）",
          _c4.lock_tries == 0 and _s4.calls == 0, f"tries={_c4.lock_tries}")
    _c5 = _FakeConn([True], invalid=["checkpoints_thread_id_idx"])
    _s5 = _Saver()
    C._setup_under_lock(_s5, pool=_FakePool(_c5))
    check("建表前先删无效索引（一次 DROP，名字带引号）",
          len(_c5.drops) == 1 and '"checkpoints_thread_id_idx"' in _c5.drops[0],
          _c5.drops[:1])
    check("  **删无效索引在 `setup()` 之前**（顺序反了就等于没删）",
          _s5.calls == 1 and len(_c5.drops) == 1)
    _c6 = _FakeConn([True], invalid=[])
    check("（反控）没有无效索引 ⇒ 一条 DROP 都不发（上一条不是恒发）",
          C.drop_invalid_indexes(_c6) == [] and _c6.drops == [])
    _c7 = _FakeConn([True], invalid=['we"ird'])
    C.drop_invalid_indexes(_c7)
    check("（反控）索引名带引号 ⇒ 被**转义**（`sql.Identifier`，不是 f-string 拼）",
          'we""ird' in _c7.drops[0], _c7.drops[0])
    _ckpt_src = (ROOT / "agent" / "checkpoint.py").read_text(encoding="utf-8")
    _lits = _advisory_literals(_ckpt_src)
    check("全仓只有两条 `advisory_*` 的 SQL 字面（抢锁 + 解锁）", len(_lits) == 2, str(_lits))
    check("抢锁那条是 **`pg_try_advisory_lock`**（阻塞版就是 20261018 那次停服）",
          any("pg_try_advisory_lock" in s for s in _lits), str(_lits))
    check("解锁那条仍是 `pg_advisory_unlock`（`try` 版拿到的锁同样是会话级的）",
          any("pg_advisory_unlock" in s for s in _lits), str(_lits))
    _bad = _ckpt_src.replace("SELECT pg_try_advisory_lock(%s) AS got",
                             "SELECT pg_advisory_lock(%s) AS got")
    _bad_lits = _advisory_literals(_bad)
    check("（反控）把 `try` 换回阻塞版 ⇒ 字面集合变了（锁抓得住它）",
          _bad != _ckpt_src and not any("pg_try_advisory_lock" in s for s in _bad_lits),
          str(_bad_lits))
    _commented = _ckpt_src + "\n# SELECT pg_advisory_lock(%s)\n"
    check("（反控）注释提到阻塞锁，不影响实际 SQL 字面检查",
          _advisory_literals(_commented) == _lits)
    print("\n⑩ 指错库：`db_name` 的取值 + `CHECKPOINT_FORBID_DB` 硬闸（20261019 批 B）")
    check("URL 形 ⇒ 库名", C.db_name("postgresql://u:p@h:5432/ckpt") == "ckpt")
    check("带 `?params` 的照切（query 不是库名的一部分）",
          C.db_name("postgresql://u:p@h:5432/ckpt?sslmode=require") == "ckpt")
    check("unix socket 形（主机段是空的）也认", C.db_name("postgresql:///ckpt") == "ckpt")
    check("解析不出库名 ⇒ 空串（**不许编一个**）", C.db_name("postgresql://u:p@h:5432") == "")
    _forbid_old = _S.checkpoint_forbid_db
    _dsn_old10 = _S.checkpoint_dsn
    _MYSQL = "mysql://u:s3cret@127.0.0.1:1/blog"
    try:
        _S.checkpoint_forbid_db = "blog"
        _m10 = _raises(_build_checkpointer, _MYSQL)
        check("填了库名 + DSN 指到它 ⇒ **响亮抛**（且不是被 scheme 那条盖住的）",
              "CHECKPOINT_FORBID_DB" in _m10, _m10[:100] or "（没抛 = 硬闸是空的）")
        check("消息点名了那个库，也说清了「为什么」（业务库的结构只许由编号迁移改）",
              "blog" in _m10 and "编号迁移" in _m10, _m10[:200])
        check("消息里**没有口令**（走 `redact`，同 `_make_pool` 那条）",
              "s3cret" not in _m10, _m10[:120])
        _S.checkpoint_forbid_db = "   "
        _m10b = _raises(_build_checkpointer, _MYSQL)
        check("（反控）只填空白 ⇒ 当没填，落回 scheme 那条（⇒ 上面那条只可能来自硬闸）",
              "CHECKPOINT_FORBID_DB" not in _m10b and _m10b.startswith("RuntimeError:"),
              _m10b[:90])
        _S.checkpoint_forbid_db = "another_db"
        _m10c = _raises(_build_checkpointer, _MYSQL)
        check("（反控）填的库名与 DSN 指的**不是**同一个 ⇒ 不拦（不是「见谁都拦」）",
              "CHECKPOINT_FORBID_DB" not in _m10c, _m10c[:90])
        _S.checkpoint_forbid_db = "blog"
        check("（反控）DSN 空 ⇒ 硬闸碰都不碰（返回 None，不是抛）",
              _build_checkpointer("") is None)
        _S.checkpoint_dsn = _MYSQL
        _m10d = _raises(_build_checkpointer)
        check("不给参数 ⇒ 也拦（硬闸接在 `settings` 那条通路上，不是只认显式入参）",
              "CHECKPOINT_FORBID_DB" in _m10d, _m10d[:100] or "（没抛）")
    finally:
        _S.checkpoint_dsn = _dsn_old10
        _S.checkpoint_forbid_db = _forbid_old
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项 ❌: {FAILS}")
        raise SystemExit(1)
    print("全部通过 ✅")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_checkpoint_factory()
