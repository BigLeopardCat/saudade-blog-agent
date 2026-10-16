# -*- coding: utf-8 -*-
"""checkpointer 接线点的判据（20261017）：零 LLM、零网络、零新依赖。

**这一件为什么存在**：`agent/checkpoint.py` 此前是个**返回错对象**的存根
（`get_checkpointer()` → `MemorySaver()`：进程内、每 worker 一份），而它的英文 docstring
写着 "maintain context across multiple turns in a session"，与 `docs/agent-architecture.md`
§4.6 明说的"它从未积累过任何状态"正相反。重写之后必须证明两件事：**今天一次都不碰**
（惰性），以及**任何"半成品接法"都炸得响**（不静默降级）。

六条探针，每条都配了会红的那一半：
  · ① **惰性**：不给 DSN ⇒ 返回 `None`，且**一次 import 都不发**——不是"import 完才发现
    没配再返回 None"，那已经晚了一步；
  · ② **反控**：给了合法 DSN ⇒ **绝不返回 None**。没有这一条，①可能只是恒真
    （本仓纪律：正控不红不算数）；
  · ③ **认不出来就响亮**：`memory://` 这类 ⇒ `RuntimeError`，且消息里**点名"故意不支持
    内存档"**——内存 saver 恰好长得像能用，这是本件的重点；
  · ④ **缺依赖就响亮**：合法 scheme 但后端没装 ⇒ `RuntimeError`，消息里有包名与 pip 命令。
    **故意不去打桩"装上了"再断言返回值**：真造 saver 那一段还没写，写一条断言去假装
    它存在，正是本仓最贵的"看着在跑、其实没跑"；
  · ⑤ **接线锁**：`agent/graph.py` 的 `build_graph()` 末尾仍是裸的 `g.compile()`。
    这条锁不是"永远不许接"——是**接线那一刻必须自觉来改它**（ADR-0011 的三处一起动）；
  · ⑥ **旧陷阱不许回来**：`agent/*.py` 的生产代码里再出现 `MemorySaver` 就红。

用法：.venv/bin/python tests/test_checkpoint_factory.py
"""
import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent.checkpoint as C  # noqa: E402

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
for _dsn in ("postgresql://u:p@127.0.0.1:5432/blog", "sqlite:////tmp/ckpt.sqlite3"):
    _ok, _why = _not_none_or_raised(_dsn)
    check(f"`{_dsn}` 不静默返回 None", _ok, _why)

print("\n③ 认不出来的 scheme ⇒ 响亮，且点名「故意不支持内存档」")
for _bad in ("memory://x", "memory", "mysql://root@127.0.0.1/blog", "redis://127.0.0.1"):
    _m = _raises(C.build_checkpointer, _bad)
    check(f"`{_bad}` ⇒ RuntimeError", _m.startswith("RuntimeError:"), _m[:90] or "（没抛）")
_mem = _raises(C.backend_for, "memory://x")
check("内存档的拒绝理由写清楚了（不然人会以为只是没实现）",
      "故意不支持内存档" in _mem, _mem[:160])

print("\n④ 合法 scheme 但后端没装 ⇒ 响亮，消息里有包名与 pip 命令")
_pg_installed = importlib.util.find_spec("langgraph.checkpoint.postgres") is not None
_pg = _raises(C.build_checkpointer, "postgresql://u@127.0.0.1/blog")
if not _pg_installed:
    check("抛 RuntimeError", _pg.startswith("RuntimeError:"), _pg[:90] or "（没抛）")
    check("点名包名", "langgraph-checkpoint-postgres" in _pg, _pg[:200])
    check("给出 pip 命令", ".venv/bin/pip install" in _pg, _pg[:200])
    check("明说不回落成内存档", "不会静默回落成内存档" in _pg, _pg[:240])
else:
    # 依赖装上了 ⇒ 走的是"造 saver 还没实现"那一支。**如实断言它**，不假装它没装。
    check("后端已装 ⇒ 仍是响亮拒绝（造 saver 属于 ADR-0011 采纳批，未实现）",
          "还没实现" in _pg, _pg[:200])

print("\n⑤ 接线锁：`build_graph()` 今天仍不挂 checkpointer")
_tree = ast.parse((ROOT / "agent" / "graph.py").read_text(encoding="utf-8"))
_fn = next((n for n in _tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "build_graph"), None)
check("找得到 `build_graph`（找不到 = 锚点没了，这条锁会空过）", _fn is not None)
if _fn is not None:
    _compiles = [n for n in ast.walk(_fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "compile"]
    check("里面只有一处 `.compile()`（多出来 = 有人从别处把 saver 接上了）",
          len(_compiles) == 1, f"{len(_compiles)} 处")
    if _compiles:
        check("那一处 `.compile()` 不带任何参数（= 今天恒惰性）",
              not _compiles[0].args and not _compiles[0].keywords,
              ast.unparse(_compiles[0]))

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

print()
if FAILS:
    print(f"失败 {len(FAILS)} 项 ❌: {FAILS}")
    raise SystemExit(1)
print("全部通过 ✅")
raise SystemExit(0)
