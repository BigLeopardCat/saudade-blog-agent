"""`tests/` 这个目录的**形状**：入口按磁盘枚举它，"盘上有什么"就得有判据管着。

**为什么单起一套**（20261019，20261020 转 pytest 后重写）：收集规则是 pytest 的
`python_files = test_*.py`（`pyproject.toml` 的 `[tool.pytest.ini_options]`，本仓没覆盖）
——它**不递归**到子目录，且名字不以 `test_` 开头的文件**一个都不收**。两条都是好规矩
（套件清单只有一处事实源、助手模块不是套件），但它们各自带着一个**静默**盲区：

① **子目录**：谁把套件挪进 `tests/foo/test_bar.py`、或建个 `tests/fixtures/` 放数据，
   它**一个都不会跑**——而 CI 绿、README 齐全、`pytest -q` 的汇总行上也不缺什么
   （收集器压根不知道有这文件）。本仓最贵的失效形状就是这个："看着在跑、其实没跑"。
② **孤儿助手**：`_*.py` 被排除在套件之外是对的，但"没人 import 的 `_x.py`"说明要么是
   死代码、要么是**改名改了一半**（引用点还指着旧名 ⇒ 那份引用会在跑到时
   `ModuleNotFoundError`，而没人 import 意味着连这一步都省了）。
③ 反向的一格：**名字不是 `test_*` 的 `.py`**（比如随手放一个 `helpers.py`）——迁移前
   它会被当套件收走并执行，它没有 `__main__`，于是退出码 0、在汇总表上是一行平白的 `✓`
   （一条**没有内容的判据**，比不跑更坏）；今天 pytest 直接不收它，但**它仍然是一份
   没人跑的代码**，所以要报出来。
④ 第四格（转 pytest 之后新长出来的）：`test_*.py` **在盘上、却一个 `def test_*` 都没有**
   ⇒ pytest 收集 0 个用例，那个文件**静默地不跑**。这是本套件最贵的那条失效形状
   （同"看着在跑、其实没跑"），而它只有 AST 才看得出来。

⇒ 四条判据，外加把"收集规则"钉在**配置**上（而不是钉在它自己身上）的那一条。

分工：本套件管**目录形状**（谁在盘上、谁被收走）；`tests/test_ci_suite_list.py` 管
**入口有没有抄名单**（CI / nightly / README 里有没有第二个清单）。两者不重叠：
那边判"三处是不是同源"，这边判"那唯一的源头本身有没有漏"。

用法：.venv/bin/python tests/test_tests_dir_shape.py
"""


from __future__ import annotations

import ast
import pathlib
import sys

from _judge import collector

ROOT = pathlib.Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT / "tests"))


import tomllib  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


def subdirs(entries: list[str]) -> list[str]:
    """listing 里的一级子目录（目录自己那行、或它里面任何东西的路径，都算）。

    `__pycache__` **不算**：那是解释器自己拉的、不进 git（`.gitignore` 里有），
    把它判红等于让每台跑过测试的机器都红一次。判的是"人放进去的东西"。
    """
    return sorted({e.split("/", 1)[0] for e in entries if "/" in e
                   and e.split("/", 1)[0] != "__pycache__"})


def stray_files(entries: list[str]) -> list[str]:
    """既不是 `.py`、也不是子目录的一级条目（`.json` 夹具、`.txt` 语料…）。

    目录那两行（`sub/`、`__pycache__/`）由上面 `subdirs` 管，这里不重复报——
    同一条毛病报成两句不同的话，读的人会以为有两件事。
    """
    return sorted(e for e in entries
                  if "/" not in e and not e.endswith(".py") and e != "__pycache__")


def helpers(entries: list[str]) -> list[str]:
    """助手模块：一级、`.py`、`_` 开头。"""
    return sorted(e for e in entries
                  if "/" not in e and e.endswith(".py") and e.startswith("_"))


def suites_on_disk(entries: list[str]) -> list[str]:
    """盘上**该**被入口收走的：一级、`.py`、不以 `_` 开头、不是入口自己。

    `conftest.py` 除外：它是 pytest 的测试现场（环境钉子 / 跨套件还原层），只有装配
    逻辑、没有任何判据。⚠️ 这条例外与 `test_ci_suite_list.py` ④ 的"收集 == 磁盘"
    **同源**，改一处必须同步另一处。
    """
    return sorted(e for e in entries
                  if "/" not in e and e.endswith(".py")
                  and not e.startswith("_") and e != "conftest.py")


def non_test_suites(entries: list[str]) -> list[str]:
    """会被当成套件收走、但名字不是 `test_*` 的那些（判据 ③ 的靶子）。"""
    return sorted(e for e in suites_on_disk(entries) if not e.startswith("test_"))


def pytest_ini_options() -> dict:
    """`pyproject.toml` 里 `[tool.pytest.ini_options]` 的**现值**（不 import、不猜）。

    收集规则住在配置里，所以要判它就得读配置 —— 这是 ④ 那半条"把规则钉在**配置**上"。
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data.get("tool", {}).get("pytest", {}).get("ini_options", {})


def uncollectable(sources: dict[str, str]) -> list[str]:
    """盘上是 `test_*.py`、却**一个顶层 `def test_*` 都没有**的那些。

    这是转 pytest 之后新长出来的失效形状：收集器收到这个文件、却收 0 个用例 ⇒ 它**静默
    地不跑**（`pytest tests/` 因此退 5 / 汇总行上一个字都不提），而它在盘上、在 `git log`
    里、在目录清单里都长得跟别人一样。**只有 AST 看得出来**，跑一遍看不出来（跑一遍就是
    "没跑"本身）。

    语法错也算进来（收集期 ERROR，同样一条用例都跑不到）。`def check_x()` 这种助手名不算
    用例 —— 前缀是 `test_` 才算，与 pytest 的规则同一处。
    """
    out: list[str] = []
    for name, src in sorted(sources.items()):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            out.append(name)
            continue
        if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name.startswith("test_") for n in tree.body):
            out.append(name)
    return out


def imported_names(src: str) -> set[str]:
    """源码里可能指到 `tests/` 某个模块的名字（**顶层**，`tests/` 是平铺的、没有子模块）。

    三种写法都要认——本仓实际用到了三种，漏一种就会把在用着的助手判成孤儿：
      · `import _rust_src`                → `a.name.split(".")[0]`
      · `from _parent_repo import read`   → `n.module`
      · `from tests import _rust_src`     → **真名在 `n.names` 里，不在 `n.module` 里**
        （`n.module` 是 `tests`）。这一种 19 个套件在用，只取 `n.module` 会全部漏掉。
    刻意**只在 `module == "tests"` 时**收 `n.names`：否则 `from agent.graph import X`
    的 `X` 也会进集合，一个恰好与某助手同名的导入就能把真孤儿遮住。
    相对导入（`n.level > 0`）不在射程里：`tests/` 不是包，没有这种写法。
    """
    out: set[str] = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            out.update(a.name.split(".", 1)[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            top = n.module.split(".", 1)[0]
            out.add(top)
            if top == "tests":
                out.update(a.name for a in n.names)
    return out


def parse_all(sources: dict[str, str]) -> dict[str, set[str]]:
    """`{文件名: 它 import 的名字集}`。**只解析一遍**：176 个文件两遍是白搭的墙钟
    （判孤儿与判幽灵读的是同一份结果）。"""
    return {name: imported_names(src) for name, src in sources.items()}


def orphan_helpers(entries: list[str], imports: dict[str, set[str]]) -> list[str]:
    """没有任何**别的**文件 import 它的 `_*.py`。

    `imports` 必须覆盖 listing 里**每一个** `.py`——漏掉谁，谁就成了隐形引用者，
    于是真孤儿被判成"有人在用"（假绿）。调用处因此断言了两边集合相等。
    只被**自己** import 的不算被用（`_selfish.py` 里写 `import _selfish` 仍是孤儿）。
    """
    used: set[str] = set()
    for name, names in imports.items():
        used |= {m for m in names if m + ".py" != name}
    return [h for h in helpers(entries) if h[:-3] not in used]


def ghost_helpers(entries: list[str], imports: dict[str, set[str]]) -> list[str]:
    """被 import、盘上却没有对应文件的 `_*` 名字（`from tests import _已改名` 那种残留）。

    双下划线开头的不算（`__future__` 就是 `_` 开头，判成幽灵助手是纯噪声）。
    """
    have = {h[:-3] for h in helpers(entries)}
    seen: set[str] = set()
    for names in imports.values():
        seen |= {m for m in names if m.startswith("_") and not m.startswith("__")}
    return sorted(seen - have)


def test_tests_dir_shape():
    TESTS = ROOT / "tests"
    _ENTRIES = sorted(p.relative_to(TESTS).as_posix() + ("/" if p.is_dir() else "")
                      for p in TESTS.rglob("*"))
    _SOURCES = {e: (TESTS / e).read_text(encoding="utf-8")
                for e in _ENTRIES if e.endswith(".py")}
    print("① `tests/` 下没有子目录（glob 不递归 ⇒ 藏进去的套件一个都不跑，且没有任何东西会报）")
    _SUB = subdirs(_ENTRIES)
    check("一级子目录只有 `__pycache__`（已被排除）", not _SUB,
          "、".join(_SUB) or "无")
    _STRAY = stray_files(_ENTRIES)
    check("没有非 `.py` 的一级文件（数据夹具请套件自己开 tempfile）", not _STRAY,
          "、".join(_STRAY) or "无")
    check("反控：`tests/sub/test_x.py` ⇒ 报出子目录 `sub`",
          subdirs(["test_a.py", "sub/test_x.py"]) == ["sub"])
    check("反控：**空**子目录（只有 `sub/` 那一行）⇒ 同样报出来",
          subdirs(["test_a.py", "sub/"]) == ["sub"])
    check("正控：`__pycache__/x.pyc` ⇒ 不算子目录（解释器自己拉的，不进 git）",
          subdirs(["test_a.py", "__pycache__/x.pyc"]) == [])
    check("反控：一级多一个 `data.json` ⇒ 报出来",
          stray_files(["test_a.py", "data.json"]) == ["data.json"])
    check("正控：子目录**不**被重复报成「非 `.py` 的一级文件」（同一条毛病别说成两句）",
          stray_files(["test_a.py", "sub/", "sub/test_x.py"]) == [])
    print("\n② 每个 `_*.py` 助手都有人在用（没人 import ⇒ 死代码，或改名改了一半）")
    check("扫描覆盖了盘上每一个文件（漏谁，谁就成了隐形引用者 ⇒ 孤儿判成假绿）",
          set(_SOURCES) == {e for e in _ENTRIES if e.endswith(".py")},
          f"{len(_SOURCES)} 份源码 / {len(_ENTRIES)} 个条目")
    check("解析器不是空转（真扫到了助手，否则下面那条是恒真的空过）",
          len(helpers(_ENTRIES)) >= 3, f"{len(helpers(_ENTRIES))} 个助手")
    _IMPORTS = parse_all(_SOURCES)
    _ORPH = orphan_helpers(_ENTRIES, _IMPORTS)
    check("没有孤儿助手", not _ORPH, "、".join(_ORPH) or "无")
    _GHOST = ghost_helpers(_ENTRIES, _IMPORTS)
    check("没有被 import 却盘上不存在的 `_*` 名字（改名残留）", not _GHOST,
          "、".join(_GHOST) or "无")
    for _desc, _src in (("`import _x`", "import _x\n"),
                        ("`from _x import f`", "from _x import f\n"),
                        ("`from tests import _x`", "from tests import _x\n")):
        check(f"正控：{_desc} ⇒ 认得出这是在用 `_x`",
              orphan_helpers(["_x.py", "test_a.py"],
                             parse_all({"_x.py": "", "test_a.py": _src})) == [])
    check("反控：加一个没人 import 的 `_dead.py` ⇒ 必须报出来",
          orphan_helpers(["_used.py", "_dead.py", "test_a.py"],
                         parse_all({"_used.py": "", "_dead.py": "",
                                    "test_a.py": "import _used\n"})) == ["_dead.py"])
    check("反控：只被**自己** import ⇒ 仍是孤儿（自己不算引用者）",
          orphan_helpers(["_selfish.py"],
                         parse_all({"_selfish.py": "import _selfish\n"})) == ["_selfish.py"])
    check("反控：`from tests import _已改名` 而盘上没有 ⇒ 报出幽灵名",
          ghost_helpers(["test_a.py"],
                        parse_all({"test_a.py": "from tests import _已改名\n"})) == ["_已改名"])
    check("正控：`from __future__ import annotations` ⇒ 不算幽灵（双下划线开头）",
          ghost_helpers(["test_a.py"], parse_all(
              {"test_a.py": "from __future__ import annotations\n"})) == [])
    print("\n③ 会被收走的 `.py` 名字都是 `test_*`（否则是一行没有内容的 `✓`）")
    _NON = non_test_suites(_ENTRIES)
    check("一级非 `_` 开头的 `.py` 全是 `test_*`（`conftest.py` 除外）", not _NON,
          "、".join(_NON) or "无")
    check("反控：放一个 `helpers.py` ⇒ 必须报出来（没人收它 ⇒ 它是一份没人跑的代码）",
          non_test_suites(["test_a.py", "helpers.py"]) == ["helpers.py"])
    check("正控：`conftest.py` 自己不算（测试现场不是套件）",
          non_test_suites(["conftest.py", "test_a.py"]) == [])
    print("\n④ 收集规则钉在**配置**上，且盘上每个 `test_*.py` 真的收得到用例")
    # 这一格原来是「`run_all.suites()` 收走的就是盘上全部该收走的」——判据拿**入口自己**
    # 的枚举去比磁盘。今天枚举规则是 pytest 的，它写在 `pyproject.toml` 里，所以钉的是
    # 那份**配置**：谁把 `python_files` 改成 `test_*.py` 之外的东西、或把 `testpaths` 指走，
    # 都会在这里现形（"配置漂了"与"文件漏了"是两种不同的漏，判据分开报）。
    _CFG = pytest_ini_options()
    check("`python_files` 没被改掉（默认就是 `test_*.py`；改了等于换一套收集规则）",
          _CFG.get("python_files") in (None, ["test_*.py"]), repr(_CFG.get("python_files")))
    check("`testpaths` 覆盖 `tests/`（裸跑 `pytest` 也收得到这整套）",
          "tests" in (_CFG.get("testpaths") or ["tests"]), repr(_CFG.get("testpaths")))
    _UNC = uncollectable(_SOURCES)
    check("盘上每个 `test_*.py` 都至少有一个顶层 `def test_*`（否则收 0 个用例、静默不跑）",
          not _UNC, "、".join(_UNC) or "无")
    check("反控：空文件 ⇒ 必须报出来（在盘上、长得像套件、一条用例都跑不到）",
          uncollectable({"test_x.py": ""}) == ["test_x.py"])
    check("反控：只有助手 `def check_x()` ⇒ 仍报出来（前缀不是 `test_`，pytest 不收）",
          uncollectable({"test_x.py": "def check_x():\n    pass\n"}) == ["test_x.py"])
    check("反控：语法错 ⇒ 报出来（收集期 ERROR，同样一条都跑不到）",
          uncollectable({"test_x.py": "def test_x(:\n"}) == ["test_x.py"])
    check("正控：有 `def test_x()` ⇒ 不报（且 `conftest.py` 不在射程里）",
          uncollectable({"test_x.py": "def test_x():\n    pass\n",
                         "conftest.py": ""}) == [])
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_tests_dir_shape()
