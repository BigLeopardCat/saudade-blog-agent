#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`agent/graph.py` 的 re-export 面：**全仓引用点**必须都还取得着（离线、秒级）。

**为什么要有这一条**：`agent/graph.py` 一直是对外的一层 **re-export 面**——十几个套件写
`from agent.graph import _bare_target_name`，还有写成 `import agent.graph as g` 再
`g._lexicon(...)` 的。而 20261017 起这仓连着做了几刀**按族搬家**（`plan_codec` /
`name_grounding` / `textnorm` / …，20261019 是 `write_grounding`）：搬走的每一族都在
graph.py 里**按原名 import 回来** ⇒ "搬家不换门牌"，调用点一处不用改。

风险正落在这句承诺上：搬完家**顺手把"没人用的 import"清掉**是极自然的动作（ruff 的
F401 自动修正是本仓常用命令）。而少一个名字的后果是——某个**别的**套件在
`AttributeError` 上红，红的理由看着像那一族坏了，**不像"门牌被摘了"**。
20261019 本刀实测发生过一次：`_lexicon` 被漏掉，根因是那个一次性扫描器只认字面
`agent.graph.X`，看不见 `g._lexicon(...)` ⇒ 才把这条判据落成常设的。

`tests/test_module_exports.py` **管不到这里**：它头注的"覆盖面"一节明写不覆盖
`graph.py` 这类存量模块（给全仓补 `__all__` 不是那次改造的交付）。两个套件分工互补。

两条判据：

  ① **引用点取得着**：全仓静态扫出所有"从 `agent.graph` 取名字"的写法（六种，见
     `refs_from_graph()`），逐个断言在 `agent.graph` 上取得到 —— 少一个就红，且一次报出
     **全部**引用点（比运行时"第一个 AttributeError"有用得多）。
  ② **搬过家的族不许在 graph.py 里再长一份**：同一个顶层名在两边各定义一次 = 两份会
     各自漂的实现，而调用点走哪一份取决于它 import 谁 ⇒ **静默分岔**。唯一的例外是
     `logger`（每族各写一行 `logging.getLogger("agent.graph")`，**故意**让日志来源仍是
     图文件，见 `write_grounding.py` 头注）——它是**写下来的**例外，不是漏比。

**扫描器自己也有判据**（本仓纪律「正控不红不算数」）：六种写法各喂一个合成样本，必须
一个不多一个不少地取到那个名字；再加两条覆盖率自检（别名文件数、取到的名字数），否则
解析器一坏就是"全都在、全绿"——那正是本仓点名的最贵失效形状。

用法：.venv/bin/python tests/test_graph_reexports.py
"""

from __future__ import annotations

import ast
import pathlib
import sys
import warnings

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _parse(src: str) -> ast.AST:
    """`ast.parse`，但**吞掉 SyntaxWarning**。

    本仓有一批文件的 docstring 里带 `\\_` / `\\``（写正则与转义时的历史笔误），
    `ast.parse` 会对它们各喷一条 SyntaxWarning。那不是本套件要判的东西（语法坏的文件
    有别的闸管），而一次扫 1000+ 文件会把输出淹掉 —— 淹掉的输出等于没有输出。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(src)

# 扫哪些目录：生产包 + 套件 + 评测。**不扫** `frontend/`（没有 .py）与 `.venv`（第三方）。
SCAN_DIRS = ("agent", "tools", "config", "models", "rag", "utils", "tests", "eval")
SCAN_FILES = ("server.py",)

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# ══════════════════════════════════════════════════════════════════
# 扫描器：从一棵 AST 里取出「指向 `agent.graph` 的那些引用」
# 读程序上 AST，不读源码文本——注释/文档里写个 `g.foo` 会把按文本做的判据当场骗过。


def _dotted(node: ast.AST) -> str | None:
    """`g.a.b` → `"g.a.b"`；不是纯属性链（含下标/调用）⇒ None。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _graph_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """→ (模块对象别名集合, 直接 `from agent.graph import` 的原始名集合)。

    六种写法里前四种（`import agent.graph [as g]` / `from agent import graph [as g]`）
    归第一格：它们绑的是**模块对象**，后面靠属性链取名字。
    """
    aliases: set[str] = set()
    direct: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "agent.graph":
                    # `import agent.graph` 不带 as ⇒ 绑的是 `agent`，属性链是字面
                    # `agent.graph.X`，下面用 "agent.graph" 这个名字去对。
                    aliases.add(a.asname or "agent.graph")
        elif isinstance(n, ast.ImportFrom) and n.level == 0:
            if n.module == "agent":
                for a in n.names:
                    if a.name == "graph":
                        aliases.add(a.asname or "graph")
            elif n.module == "agent.graph":
                # `from agent.graph import X as Y` ⇒ 要的是**原名** X（Y 是本地别名）
                direct.update(a.name for a in n.names)
    return aliases, direct


def refs_from_graph(tree: ast.AST) -> dict[str, set[str]]:
    """→ {被引用的名字: 该文件里出现它的写法}。

    属性链只看**第一段**（`g.settings.agent_evidence` ⇒ 取 `settings`：门牌是 `settings`，
    它之后的属性是它自己的事，不归 graph.py 保证）。
    另外收 `getattr(g, "X")` / `setattr(g, "X", …)` 的字面形式——**打桩**全是这个形状。
    """
    aliases, direct = _graph_aliases(tree)
    out: dict[str, set[str]] = {name: {"from-import"} for name in direct}
    if not aliases:
        return out
    for n in ast.walk(tree):
        d: str | None = None
        form = ""
        if isinstance(n, ast.Attribute):
            d, form = _dotted(n), "attr"
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id in ("getattr", "setattr") and len(n.args) >= 2
              and isinstance(n.args[1], ast.Constant)
              and isinstance(n.args[1].value, str)):
            base = _dotted(n.args[0])
            if base is not None:
                d, form = f"{base}.{n.args[1].value}", n.func.id
        if d is None:
            continue
        for a in aliases:
            if d.startswith(a + "."):
                out.setdefault(d[len(a) + 1:].split(".")[0], set()).add(form)
                break
    return out


def _scan_files() -> list[pathlib.Path]:
    files = [p for d in SCAN_DIRS for p in (ROOT / d).rglob("*.py")]
    files += [ROOT / f for f in SCAN_FILES if (ROOT / f).exists()]
    return sorted(files)


# ══════════════════════════════════════════════════════════════════
# 取值：全仓扫一遍
print("① 全仓引用点：`agent.graph` 上的名字一个都不能少")

_hits: dict[str, dict[str, set[str]]] = {}       # 名字 → {文件: 写法}
_alias_files: list[str] = []
for _f in _scan_files():
    try:
        _tree = _parse(_f.read_text(encoding="utf-8"))
    except SyntaxError:                       # 语法坏的文件不是本套件的活（有别的闸管）
        continue
    if _graph_aliases(_tree)[0]:
        _alias_files.append(str(_f.relative_to(ROOT)))
    for _name, _forms in refs_from_graph(_tree).items():
        _hits.setdefault(_name, {})[str(_f.relative_to(ROOT))] = _forms

# 扫描器自检：先证明它真的看见东西了，否则下面那条"全都在"是空过。
check("扫描器在真输入上认出了别名写法（`import agent.graph as g` 一类）",
      len(_alias_files) >= 40, f"只认出 {len(_alias_files)} 个文件")
check("扫描器取到了足够多的名字（下界，防解析器半坏）",
      len(_hits) >= 200, f"只取到 {len(_hits)} 个")
# 抽四个已知存在的名字当**扫描器正控**（不是契约清单：契约是"引用点全都取得着"）。
_KNOWN = ("_lexicon", "_bare_target_name", "plan_state", "settings")
check("四个已知名字都被扫到（`g._lexicon(...)` 这种别名写法在里面）",
      all(k in _hits for k in _KNOWN), f"缺：{[k for k in _KNOWN if k not in _hits]}")

import agent.graph as _G  # noqa: E402

_missing = sorted(n for n in _hits if not hasattr(_G, n))
check("⭐ `agent.graph` 上取得到**每一个**被引用的名字（少一个 = 某个套件会在"
      "AttributeError 上红，而红的理由看着像那一族坏了）",
      not _missing,
      "；".join(f"`{n}` ← " + "、".join(sorted(_hits[n])) for n in _missing[:6]))


# ══════════════════════════════════════════════════════════════════
# ② 搬过家的族：graph.py 里不许再长一份同名定义
print("\n② 搬过家的族不许在 graph.py 里再定义一份（唯一例外：`logger`）")

_GSRC = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
_GTREE = _parse(_GSRC)


def top_names(tree: ast.AST) -> set[str]:
    """模块**顶层**的 def / class / 赋值 / 注解赋值名（函数体内的同名不算）。"""
    out: set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            out.update(t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.add(n.target.id)
    return out


# 射程**从 graph.py 自己现取**（它 import 了谁），不是手抄一份名单——名单会漂。
_SOURCES = sorted({n.module for n in ast.walk(_GTREE)
                   if isinstance(n, ast.ImportFrom) and n.level == 0
                   and (n.module or "").startswith("agent.")
                   and n.module != "agent.graph"})
check("从 graph.py 的 AST 现取到了它的本仓依赖面（取不到 ⇒ 下面那条没有判据，直接红）",
      len(_SOURCES) >= 20, f"{len(_SOURCES)} 个：{_SOURCES[:6]}…")

_G_TOP = top_names(_GTREE)
# `logger` 是**写下来的**例外：每族各写一行 `logging.getLogger("agent.graph")`，
# 故意让日志来源仍是图文件（否则搬一次家，`logging.getLogger("agent.graph")` 的
# 套件会取到 0 条记录而**不报错**——见 `tests/test_turn_window.py` 那一课）。
_LOGGER_EXC = {"logger"}
_dup: dict[str, list[str]] = {}
for _mod in _SOURCES:
    _p = ROOT / (_mod.replace(".", "/") + ".py")
    if not _p.exists():
        continue
    _both = sorted(_G_TOP & top_names(_parse(_p.read_text(encoding="utf-8"))))
    if _both:
        _dup[_mod] = _both
_dup_extra = {m: [n for n in ns if n not in _LOGGER_EXC] for m, ns in _dup.items()}
_dup_extra = {m: ns for m, ns in _dup_extra.items() if ns}
check("⭐ 没有任何名字在 graph.py 与它 import 的本仓模块里**各定义一次**",
      not _dup_extra,
      "；".join(f"{m}: {ns}" for m, ns in sorted(_dup_extra.items())))
check("  例外只有 `logger`，且它**确实**出现在至少一处（例外表过期了要销账）",
      any(ns == ["logger"] for ns in _dup.values()), f"{_dup}")


# ══════════════════════════════════════════════════════════════════
print("\n③ 正控：六种写法各喂一个合成样本，必须一个不多一个不少")
_SAMPLES = {
    "from-import": "from agent.graph import _lexicon",
    "from-import-as": "from agent.graph import _lexicon as _lx",
    "import-as": "import agent.graph as g\ng._lexicon('x')",
    "from-agent-as": "from agent import graph as gg\ngg._lexicon('x')",
    "import-plain": "import agent.graph\nagent.graph._lexicon('x')",
    "from-agent-plain": "from agent import graph\ngraph._lexicon('x')",
    "getattr": "import agent.graph as g\ngetattr(g, '_lexicon')",
    "setattr": "import agent.graph as g\nsetattr(g, '_lexicon', 1)",
}
for _tag, _code in _SAMPLES.items():
    _got = set(refs_from_graph(_parse(_code)))
    check(f"正控·{_tag} ⇒ 只取到 `_lexicon`", _got == {"_lexicon"}, f"取到 {sorted(_got)}")

# 反控：别的模块的别名、以及指向**别的**包的 `g`，一个都不许混进来
_NOISE = ("import agent.name_grounding as g\ng._lexicon()\n"
          "import other.graph as g2\ng2._lexicon()\n"
          "from agent.graph import _lexicon\nx = _lexicon\n")
check("反控·指向别的模块的别名与裸名调用都不混进来",
      set(refs_from_graph(_parse(_NOISE))) == {"_lexicon"},
      f"取到 {sorted(refs_from_graph(_parse(_NOISE)))}")
check("反控·`g.settings.agent_evidence` 只取第一段（门牌是 `settings`，"
      "它后面的属性不归 graph.py 保证）",
      set(refs_from_graph(_parse("import agent.graph as g\ng.settings.agent_evidence = 1")))
      == {"settings"})

# 反控·判据本身会红：拿一个**不存在**的名字喂进去，① 那一条必须报出来
_ghost = refs_from_graph(_parse("import agent.graph as g\ng._no_such_name_zzz()"))
check("正控·幽灵名字确实被扫到（否则 ① 是空过）",
      set(_ghost) == {"_no_such_name_zzz"} and not hasattr(_G, "_no_such_name_zzz"))

# ══════════════════════════════════════════════════════════════════
print()
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print(f"✅ 全绿（扫了 {len(_hits)} 个名字 / {len(_alias_files)} 个别名文件）")
