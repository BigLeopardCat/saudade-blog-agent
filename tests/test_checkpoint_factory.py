# -*- coding: utf-8 -*-
"""checkpointer 接线点的判据（20261017；20261018 批 7 随采纳改过 ④ 与 ⑧）。

**这一件为什么存在**：`agent/checkpoint.py` 此前是个**返回错对象**的存根
（`get_checkpointer()` → `MemorySaver()`：进程内、每 worker 一份），而它的英文 docstring
写着 "maintain context across multiple turns in a session"，与 `docs/agent-architecture.md`
§4.6 明说的"它从未积累过任何状态"正相反。重写之后必须证明两件事：**不配就一次都不碰**
（惰性），以及**任何"半成品接法"都炸得响**（不静默降级）。

八条探针，每条都配了会红的那一半：
  · ① **惰性**：不给 DSN ⇒ 返回 `None`，且**一次 import 都不发**——不是"import 完才发现
    没配再返回 None"，那已经晚了一步；
  · ② **反控**：给了合法 DSN ⇒ **绝不返回 None**。没有这一条，①可能只是恒真
    （本仓纪律：正控不红不算数）；
  · ③ **认不出来就响亮**：`memory://` 这类 ⇒ `RuntimeError`，且消息里**点名"故意不支持
    内存档"**——内存 saver 恰好长得像能用，这是本件的重点；
  · ④ **缺依赖就响亮**：合法 scheme 但后端没装 ⇒ `RuntimeError`，消息里有包名与 pip 命令。
    **20261018 批 7 改成打桩 import 而不是看本机装没装**：采纳之后本机与 CI 都装了 PG
    后端，照"装没装"分叉的话这一条在最需要它的两个环境里**永远不执行**。
  · ④b **连不上就响亮**（批 7 新增）：后端装了但 DSN 连不上 ⇒ `RuntimeError`，
    **绝不返回一个坏 saver**（坏 saver 的失效时刻是"第一轮对话的第一次 checkpoint"，
    那时已经没有任何东西指向配置了）；异常文本里的口令必须已脱敏（它会进 `agent.log`）；
  · ⑤ **接线锁**：`agent/graph.py` 那一处 `.compile()` **只许把调用方给的 `checkpointer`
    原样传下去**（`g.compile(checkpointer=checkpointer)`），且 `build_graph` 自己的
    `checkpointer` 缺省 `None`。**20261017 改过判据**：这条锁原先是"末尾仍是**裸的**
    `g.compile()`"，上游 `48da74f` 正是照它要的方式接了线 ⇒ 锁跟着钉**新**不变量。
    现在禁的不是"传参"，是**在编译点造 saver**——那几种会让生产**突然开始持久化**，
    而别的判据一条都不会因此变红。裸 `g.compile()` 同样算违规，理由不同：实验臂
    （`agent/checkpoint_arm.py`）传进来的 saver 会被**静默忽略**，于是"没挂 saver"
    伪装成"checkpoint 没区别"，正是本件要防的那种失效；
  · ⑥ **旧陷阱不许回来**：`agent/*.py` 的生产代码里再出现 `MemorySaver` 就红；
  · ⑦ **DSN 的来源只有一处**：`build_checkpointer()` 不给参数时读
    `settings.checkpoint_dsn`。把那一格临时拨成 DSN ⇒ **不给参数**的调用必须**响亮抛**
    （没读到的话它会拿空串直接返回 `None`，那是**静默**）；拨回空 ⇒ 返回 `None`。
    没有这一条，那一格就是个"填了不生效、也不报错"的幽灵旋钮（本仓在配置面上栽过五次）；
  · ⑧ **会话坐标与脱敏**（批 7 新增）：`conversation_thread` 在缺席的六种形态上必须
    回 `None`——拼成 `conv_None` 会让所有"没有会话身份"的请求共用一条线程，也就是
    **跨用户串台**（安全判据，不是洁癖）；`thread_id_for` 三支各判一次；`redact` 不许
    把口令带进异常文本、也不许把整条 DSN 吃掉。

用法：.venv/bin/python tests/test_checkpoint_factory.py
"""
import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent.checkpoint as C  # noqa: E402

# 把连接等待调短（`_POOL_WAIT` 是模块常量就是为了这件事）：本件量的是"连不上时抛不抛"，
# 不是"等多久"。不调的话每条要真连库的判据都得等满 10 秒。
# ⚠️ 所有 DSN 都指 **127.0.0.1:1**（一个必然没人听的端口）——**不许指 5432**：那是本机
# 生产 PG，判据去连它就把"单测"变成了"对生产的探测"（连上了会更快，连不上要等超时，
# 两种结局都与这条判据要量的事无关）。
C._POOL_WAIT = 0.5
_DEAD = "postgresql://u:s3cret@127.0.0.1:1/blog"

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {desc}")
    else:
        FAILS.append(f"{desc} —— {detail}")
        print(f"  ❌ {desc} —— {detail}")


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
        return C.build_checkpointer(dsn) is not None, "返回了 None"
    except Exception as e:
        return True, f"抛了（可以接受）：{type(e).__name__}"


print("① 惰性：不给 DSN ⇒ None，且一次 import 都不发")
_imported: list[str] = []
_real_import = C.importlib.import_module


def _spy(name, *a, **kw):
    _imported.append(name)
    return _real_import(name, *a, **kw)


C.importlib.import_module = _spy
try:
    _none = C.build_checkpointer()
    _blank = C.build_checkpointer("   ")
finally:
    C.importlib.import_module = _real_import

check("空 DSN 返回 None（= 今天的行为）", _none is None, repr(_none))
check("只有空白也当没配", _blank is None, repr(_blank))
check("空 DSN **一次 import 都不发**（不是 import 完才发现没配）", _imported == [], str(_imported))

print("\n② 反控：给了 DSN 就绝不许静默返回 None")
for _dsn in (_DEAD, "sqlite:////tmp/ckpt.sqlite3"):
    _ok, _why = _not_none_or_raised(_dsn)
    check(f"`{C.redact(_dsn)}` 不静默返回 None", _ok, _why)

print("\n③ 认不出来的 scheme ⇒ 响亮，且点名「故意不支持内存档」")
for _bad in ("memory://x", "memory", "mysql://root@127.0.0.1/blog", "redis://127.0.0.1"):
    _m = _raises(C.build_checkpointer, _bad)
    check(f"`{_bad}` ⇒ RuntimeError", _m.startswith("RuntimeError:"), _m[:90] or "（没抛）")
_mem = _raises(C.backend_for, "memory://x")
check("内存档的拒绝理由写清楚了（不然人会以为只是没实现）",
      "故意不支持内存档" in _mem, _mem[:160])

print("\n③b sqlite 这一支**刻意不落地**（产线 4 worker 共用一个文件库，并发写没验过）")
_sq = _raises(C.build_checkpointer, "sqlite:////tmp/ckpt.sqlite3")
check("抛 RuntimeError（不是静默去造一个没验过的 saver）",
      _sq.startswith("RuntimeError:"), _sq[:90] or "（没抛）")
check("说清是「没有落地」而不是含糊的「还没实现」", "没有落地" in _sq, _sq[:200])

print("\n④ 合法 scheme 但后端没装 ⇒ 响亮，消息里有包名与 pip 命令")
# **打桩而不是看本机装没装**（20261018 批 7 改）：采纳之后本机与 CI 都装了 PG 后端
# （`pyproject.toml` 的依赖组），照"装没装"分叉的话，这一条在最需要它的两个环境里
# **永远不会被执行**。打桩 import 让这条判据在任何机器上判的都是同一件事：
# 依赖缺失时**响亮、点名包名、给出 pip 命令、明说不回落成内存档**。
_pg_installed = importlib.util.find_spec("langgraph.checkpoint.postgres") is not None
_real_import = C.importlib.import_module


def _no_module(name, *a, **kw):
    raise ImportError(f"No module named {name!r}")


C.importlib.import_module = _no_module
try:
    _pg = _raises(C.build_checkpointer, _DEAD)
finally:
    C.importlib.import_module = _real_import
check("抛 RuntimeError", _pg.startswith("RuntimeError:"), _pg[:90] or "（没抛）")
check("点名包名", "langgraph-checkpoint-postgres" in _pg, _pg[:200])
check("给出 pip 命令", ".venv/bin/pip install" in _pg, _pg[:200])
check("明说不回落成内存档", "不会静默回落成内存档" in _pg, _pg[:240])

print("\n④b 后端已装 + DSN 连不上 ⇒ 响亮抛，**绝不返回一个坏 saver**")
# 坏 saver 的失效时刻是"第一轮对话的第一次 checkpoint"——那时已经没有任何东西指向
# 配置了。所以这里必须抛。`_POOL_WAIT` 是模块常量就是为了让这条判据**不必真等 10 秒**
# （判据把等待调短，量的是"抛不抛"，不是"等多久"）。
if not _pg_installed:
    print("  ⏭ 本机没装 langgraph-checkpoint-postgres ⇒ **这一条没验**（不是通过）")
else:
    _down = _raises(C.build_checkpointer, _DEAD)
    check("抛 RuntimeError（没抛 = 返回了一个连不上的 saver）",
          _down.startswith("RuntimeError:"), _down[:90] or "（没抛）")
    check("异常文本里的口令已脱敏", "s3cret" not in _down and "***@" in _down, _down[:200])
    check("明说不静默降级", "不静默降级" in _down, _down[:240])

print("\n⑤ 接线锁：那一处 `.compile()` 只许把调用方给的 `checkpointer` 原样传下去")


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


# 红基线（本仓纪律：正控不红不算数）——锁必须抓得住"在这一层造 saver"那四种形态。
# 正控用**真源码**（下面两条）与一份最小的合规骨架，三处都不许红。
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

_graph_src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
_agent_src = (ROOT / "agent" / "agent.py").read_text(encoding="utf-8")
check("生产图 `agent/graph.py::build_graph` 合规",
      not _wire_lock(_graph_src, "build_graph", "compile"),
      "；".join(_wire_lock(_graph_src, "build_graph", "compile")))
check("`agent/agent.py::create_agent` 同样只传不造（缺省 None + 原样传给 build_graph）",
      not _wire_lock(_agent_src, "create_agent", "build_graph"),
      "；".join(_wire_lock(_agent_src, "create_agent", "build_graph")))

print("\n⑥ 旧陷阱不许回来：`agent/` 的生产代码里没有 MemorySaver")


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


# 红基线（本仓纪律：正控不红不算数）——检测器必须**抓得住**那个陷阱
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
# 20261017：`config/settings.py` 加了 `checkpoint_dsn` 这一格，`build_checkpointer()` 不给
# 参数时读它。这一条必须证明**那条通路真的通**：不通的话那一格就是幽灵旋钮——填了不生效、
# 也不报错（本仓在配置面上栽过五次，见 `tests/test_settings_surface.py` 头注）。
# 打的是**实例属性**：`config/__init__.py` 导出的就是那个单例，与 `agent/checkpoint.py`
# `from config import settings` 拿到的是**同一个对象** ⇒ 测的是真通路，不是影子。
from config import settings as _S  # noqa: E402

_old_dsn = _S.checkpoint_dsn
try:
    _S.checkpoint_dsn = _DEAD
    _loud = _raises(C.build_checkpointer)
    # 拨成一个连不上的 DSN ⇒ 必须**响亮抛**。这就证明了那一格被真的读到了：
    # 没读到的话 `build_checkpointer()` 会拿空串直接返回 `None`（**静默**）。
    # 刻意不用"能连上的 DSN + 断言返回了 saver"——那要求本机有个真库，
    # 判据就变成了"本机有没有库"，而这条要判的是**接线通不通**。
    check("拨成 DSN ⇒ **不给参数**也响亮抛（不是静默返回 None ⇒ 那一格是活的）",
          _loud.startswith("RuntimeError:"), _loud[:90] or "（没抛 = 这一格是死的）")
    _S.checkpoint_dsn = "   "
    check("拨成纯空白 ⇒ 与 `build_checkpointer('   ')` 同一把尺子：None",
          C.build_checkpointer() is None)
    _S.checkpoint_dsn = ""
    check("拨回空 ⇒ None（= 没挂 saver 时的行为）", C.build_checkpointer() is None)
finally:
    _S.checkpoint_dsn = _old_dsn

print("\n⑧ 会话坐标与脱敏（批 7 新增的三个纯函数——**不连库，本机就能判**）")
# ① 缺席的三种形态一个都不许拼成坐标。这一条是**安全判据**不是洁癖：拼成
# `conv_None` 会让所有"没有会话身份"的请求（新会话、评测直连、请求体缺这一格）
# 共用同一条线程 ⇒ 图把别人的历史当自己的。
_ABSENT = (None, "", "   ", 0, "0", "None")
check("会话 id 缺席的六种形态 ⇒ 全 None（**不编坐标**）",
      all(C.conversation_thread(x) is None for x in _ABSENT),
      str([C.conversation_thread(x) for x in _ABSENT]))


def _naive(cid):                    # 反控：缺席也照拼（上面那组必须抓得住它）
    return f"conv_{cid}"


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

print()
if FAILS:
    print(f"失败 {len(FAILS)} 项 ❌: {FAILS}")
    raise SystemExit(1)
print("全部通过 ✅")
raise SystemExit(0)
