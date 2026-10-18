# -*- coding: utf-8 -*-
"""工程配置的六条判据（20261013，行业规范收口批 2；⑥ 同日升 3.12 时补）。

**为什么单起一套**：这几处此前的失效形状**一模一样**——"文件里写着一句话，而那句
话没有任何东西看着"：

  ① `pyproject.toml` 的 wheel 包列表列了并不存在的 `chains/`、漏了生产代码真的
     import 的 `rag/`。列错**不会报错**，因为本仓从不构建 wheel（部署走 git pull）。
  ② `server.py` 的 `version="1.0.0"` 与 `pyproject.toml` 的 `0.2.0` **差着两个大版本**
     ——`/openapi.json` 上挂着哪个，全看读的是哪一份。
  ③ `SECURITY.md` 把上游 CVE 明确推给"Dependabot"，而仓库里从来没有
     `.github/dependabot.yml` ⇒ 那句承诺当时是空的（依赖又全 `==` 钉死，没有它
     什么都不会升级）。
  ④ `[tool.mypy]` 配了却**从不执行**（mypy 不在依赖、不在 lock、CI 没有这一步）
     ⇒ 函数签名注释了约 78% 而没有一个被校验。补成真之后，"棘轮真的接在 CI 上"
     与"三处写的 mypy 版本是同一个"同样需要有人看着——否则它会以 ① 那种方式再死一次。
  ⑤ 覆盖率那一档（`run_all.py --coverage`）的 pin 与配置：pin 少写 `[toml]` extra ⇒
     coverage 读不了 `pyproject.toml`、**每个套件都退 1**（现场像整批代码坏了；
     当年是 3.10，标准库没有 TOML）；`[tool.coverage.run].source` 少列一个包 ⇒
     那个包**整份**从报告里消失，而"哪些文件一直没人碰"这个问题的答案就悄悄少一块。
     两处都是"同一件事写两遍"。
  ⑥ **解释器版本**：这一条一度是上面五条里唯一**没有**机器锁的，而它恰好是写法最
     分散的一项——`requires-python`、`.python-version`、`[tool.ruff].target-version`、
     `[tool.mypy].python_version`、CI 的 `python-version`、棘轮基线的 `python_target`，
     **六处**。20261013 把 3.10 升到 3.12 时逐处手改才看清：任何一处漏掉都不报错，
     只会以"CI 装了 3.10 而代码声明 3.12"这种**假绿**的形式存在（反过来更糟：
     声明 3.12、CI 只验 3.10 ⇒ 升版这件事压根没被验过）。另外还有 `uv.lock` 这份
     **生成物**（不是手写的第七处）：它同样记着 `requires-python`，改声明忘重跑
     `uv lock` 会落在 CI 的 `uv sync --frozen` 上，而本机没有这条出口可验。

判据一律读**磁盘事实**（目录在不在、源码里还有没有第二份字面量），不读"文档里写了
这一条"——后者的失效方式正是"名单对了、事情没做"。

用法：.venv/bin/python tests/test_packaging.py
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def _strip_comments(text: str) -> str:
    """把每行的 `#` 之后丢掉——**但引号内的 `#` 不算注释**。

    ⚠️ 这一步不能省：本仓的 pyproject 在注释里用 ASCII 双引号引中文
    （`# 现在只有"声明我有哪些包"的价值`）。不剥注释的话，下面那句
    `"([^"]+)"` 会把**注释里的中文**当包名读出来，断言就报出一个磁盘上
    从来没有过的"包"——正是"注释伪装成代码骗过断言"那一族（同
    `docs/lint-baseline.md` 记的 CSS 注释关掉整块规则）。引号计数只是够用，
    不是 TOML 词法：本文件的字符串值里没有 `#`。
    """
    out = []
    for line in text.splitlines():
        i = line.find("#")
        if i >= 0 and line[:i].count('"') % 2 == 0:
            line = line[:i]
        out.append(line)
    return "\n".join(out)


def _section(text: str, header: str) -> str:
    """取 TOML 里 `[header]` 那一节的正文（到下一个行首 `[` 为止），**已剥注释**。

    刻意不引 toml 解析器：**20261013 升到 3.12 之后理由从"没有"变成了"不必"**——
    以前是本机 Python 3.10 没有 `tomllib`，现在是标准库里有（3.11+）但这里用不着。
    两个理由都成立且都指向同一件事：这六条判据里有几条**正是为了在升版前后都能跑**
    才写的（⑥ 要能告诉你 `requires-python` 是不是写歪了），让它先依赖新解释器才有的
    标准库，等于把"验升版"的那把尺子和"升版"绑在一起。这里的用法始终只有"读一个节
    里的一个字符串键"，剥掉注释后正则够用；真正需要机器读 TOML 的地方（`uv` 本身）
    读的是同一个文件。
    """
    m = re.search(rf"^\[{re.escape(header)}\](?P<body>.*?)(?=^\[|\Z)", text, re.M | re.S)
    return _strip_comments(m.group("body")) if m else ""


PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
PROJECT_SEC = _section(PYPROJECT, "project")
EVAL_YML = ROOT / ".github" / "workflows" / "eval.yml"
WHEEL_SEC = _section(PYPROJECT, "tool.hatch.build.targets.wheel")
RALL = (ROOT / "tests" / "run_all.py").read_text(encoding="utf-8")

_m = re.search(r'^version\s*=\s*"([^"]+)"', PROJECT_SEC, re.M)
PROJECT_VERSION = _m.group(1) if _m else None

# ══════════════════════════════════════════════════════════════════════════
print("① wheel 包列表 == 磁盘事实（列错不会报错，所以必须有人看着）")

_pkgs = re.findall(r'"([^"]+)"', WHEEL_SEC)
check("pyproject 里有 wheel 包列表", bool(_pkgs), WHEEL_SEC.strip()[:60])
check("每一项都是**真实存在**的目录（`chains/` 就是这么被写进去的）",
      all((ROOT / p).is_dir() for p in _pkgs),
      "、".join(p for p in _pkgs if not (ROOT / p).is_dir()))
# 每一项还得是**普通包**（有 `__init__.py`），不是 PEP 420 隐式命名空间包。
# 判据是 20261013 那次"第一次真跑 mypy"给的：`rag/` 没有 `__init__.py`，mypy 当场
# 拒绝整个目录（`Source file found twice under different module names: "search" and
# "rag.search"`）——所有 Python import 都写 `rag.xxx` 所以运行期一直没坏，只有
# **静态分析器**看得出来。列进 `packages` 的目录尤其不该留这种余地。
_implicit = [p for p in _pkgs if not (ROOT / p / "__init__.py").is_file()]
check("每一项都有 `__init__.py`（隐式命名空间包会让 mypy 拒绝整个目录）",
      not _implicit, "、".join(_implicit))

# 更强的另一半：生产代码**真的 import 了**的顶层包，一个都不许漏。
# 这正是 `rag/` 的那个洞——它被 `agent/graph.py` 等十几处 import，却不在列表里。
_LOCAL_DIRS = {p.name for p in ROOT.iterdir()
               if p.is_dir() and not p.name.startswith(".")
               and not p.name.startswith("__")}
_RUNTIME_SRC = [ROOT / "server.py"]
for _d in ("agent", "config", "models", "rag", "tools", "utils"):
    _RUNTIME_SRC += sorted((ROOT / _d).rglob("*.py"))
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)
_used: set[str] = set()
for _p in _RUNTIME_SRC:
    _used |= {m for m in _IMPORT_RE.findall(_p.read_text(encoding="utf-8"))}
_used &= _LOCAL_DIRS                       # 只留"是本仓顶层目录"的那些
_missing = sorted(_used - set(_pkgs))
check("生产代码 import 的本仓顶层包全在列表里（`rag/` 就是漏掉的那一个）",
      not _missing, "漏：" + "、".join(_missing))
check("  对照：这个集合非空（否则上面那条是空跑）", len(_used) >= 5, str(sorted(_used)))


# ══════════════════════════════════════════════════════════════════════════
print("\n② 版本号只有一个来源：`pyproject.toml` 的 `[project].version`")

check("pyproject 的 [project] 声明了 version", PROJECT_VERSION is not None,
      str(PROJECT_VERSION))

_imported = False
try:
    import server  # noqa: PLC0415  （判据就是"运行期真的报哪个版本"）
    _imported = True
except Exception as e:  # pragma: no cover - 依赖缺失也要把话说清楚
    check("server 能 import（版本判据的前提）", False, f"{type(e).__name__}: {e}")

if _imported:
    check("⭐ `server.app.version` == pyproject 的版本（此前是 1.0.0 vs 0.2.0）",
          server.app.version == PROJECT_VERSION,
          f"运行期={server.app.version} pyproject={PROJECT_VERSION}")
    check("  `_app_version()` 与 app 上挂的是同一个值（不是两处各取一次）",
          server._app_version() == server.app.version)
    check("  **装好的环境里不许**落进 `0.0.0+source` 那个兜底（落了说明这台机器的"
          "dist-info 与源码对不上）",
          server.app.version != "0.0.0+source")

_src = (ROOT / "server.py").read_text(encoding="utf-8")
# 用 AST 找 `xxx(version=...)`，而不是正则扫文本：正则会把**注释和 docstring 里**
# 提到的版本号也算成"第二份事实源"（本仓的注释恰恰爱写这些），报出来的假红比
# 漏判更坏——它会让人把判据关掉。AST 只认真正的关键字实参。
_kw_version = [kw for node in ast.walk(ast.parse(_src)) if isinstance(node, ast.Call)
               for kw in node.keywords if kw.arg == "version"]
check("没有任何一处把版本号当**字面量**传给 version=（`version=\"1.0.0\"` 那种）",
      not any(isinstance(kw.value, ast.Constant) for kw in _kw_version),
      "、".join(ast.unparse(kw.value) for kw in _kw_version
                if isinstance(kw.value, ast.Constant)))
check("  正控：确实有一处传 version=（否则上面那条是空跑）",
      any(not isinstance(kw.value, ast.Constant) for kw in _kw_version),
      "、".join(ast.unparse(kw.value) for kw in _kw_version))
check("  它读的是安装元数据（`importlib.metadata`），不是自己解析 toml",
      "from importlib.metadata import version as _pkg_version" in _src)


# ══════════════════════════════════════════════════════════════════════════
print("\n③ SECURITY.md 说 CVE 交给 Dependabot ⇒ 那份配置必须在场")

_dep = ROOT / ".github" / "dependabot.yml"
check(".github/dependabot.yml 在位", _dep.is_file())
if _dep.is_file():
    _dt = _dep.read_text(encoding="utf-8")
    check("格式版本是 2", re.search(r"^version:\s*2\s*$", _dt, re.M) is not None)
    check("覆盖 pip（跟着 pyproject + uv.lock）",
          'package-ecosystem: "pip"' in _dt)
    check("覆盖 github-actions（workflow 里钉的 action 版本没有 lock 文件管）",
          'package-ecosystem: "github-actions"' in _dt)
    check("  两边都有 schedule（没有 schedule 的 update 配置不生效）",
          _dt.count("schedule:") >= 2)

_sec = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
check("对照：SECURITY.md 确实把这件事推给了 Dependabot（否则本套件名不副实）",
      "Dependabot" in _sec)


# ══════════════════════════════════════════════════════════════════════════
print("\n④ 类型检查棘轮真的接在 CI 上，且 mypy 版本只有一处事实源")

_gate = ROOT / "eval" / "type_gate.py"
_base = ROOT / "eval" / "mypy_baseline.json"
check("eval/type_gate.py 在位", _gate.is_file())
check("eval/mypy_baseline.json 在位（棘轮的量就在这份账上）", _base.is_file())

# 版本按**dev 分组体**取，不写 `dev = ["mypy==X"]` 那种"顺手把唯一一项也判进去"的
# 正则——20261005 那次（见 test_ci_suite_list.py ①b 的注释）正是这么坏的：
# 给 dev 组加第二个包就会让判据红，而它红的时候说的是"没找到那一行"。
_dg_sec = _section(PYPROJECT, "dependency-groups")
_mypy_pin = re.search(r'"mypy==([0-9][^"]*)"', _dg_sec)
MYPY_PIN = _mypy_pin.group(1) if _mypy_pin else None
check("pyproject 的 dev 分组钉了 mypy 版本", MYPY_PIN is not None,
      MYPY_PIN or "dependency-groups 里没有钉死的 mypy==")

if _base.is_file():
    _b = json.loads(_base.read_text(encoding="utf-8"))
    # ⚠️ 版本对不上不是"变差"而是**换了一个量**：换 mypy 版本就换了一套错误集，
    # 拿旧账比新读数毫无意义。`eval/type_gate.py` 因此在这里也退 3（两道）。
    check(f"⭐ 基线的 mypy 版本 == dev 分组钉的（{MYPY_PIN}）",
          _b.get("mypy_version") == MYPY_PIN,
          f"基线={_b.get('mypy_version')} dev={MYPY_PIN}")
    check("  基线里记着读数（total 与逐文件计数都在）",
          isinstance(_b.get("total"), int) and isinstance(_b.get("by_file"), dict)
          and bool(_b["by_file"]), f"total={_b.get('total')}")
    # 查谁：基线的 targets 与棘轮里的 TARGETS 必须同源，否则"量到的面"和"账上的面"
    # 会各自漂——两边都看不出对方变了（同 `rag/` 那个洞的形状）。
    _tg = ast.parse(_gate.read_text(encoding="utf-8"))
    _targets = None
    for _n in _tg.body:
        if isinstance(_n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TARGETS" for t in _n.targets):
            _targets = ast.literal_eval(_n.value)
    check("  基线的 targets == 棘轮里的 TARGETS（否则账与量各漂各的）",
          _targets is not None and _targets == _b.get("targets"),
          f"棘轮={_targets} 基线={_b.get('targets')}")

# ⚠️ **第三个量：mypy 从哪儿看到三方库**（20261013 上线当天就被 CI 抓到的洞）。
# `eval/type_gate.py` 拼出来的那条 mypy 命令必须**恒带**
# `--python-executable <启动本脚本的解释器>`。不传时，mypy 认的是"我自己跑在哪个环境里"
# ⇒ 同一条判据、同一个 mypy 版本、同一个 `python_version`，**只换启动器就能读出两个数**：
#   `<项目解释器> -m mypy` → 220（找得到 langgraph / openai / fastapi ⇒ 有真类型）
#   `uvx mypy@X …`          → 212（uvx 是只装了 mypy 的隔离环境 ⇒ 三方 import 全落到
#                            `ignore_missing_imports` ⇒ 退化成 Any ⇒ 少报 8 个）
# 最初那份 212 的基线就是这么量出来的，CI 拿真环境一跑当场红。
# **更要命的是它坏起来不像坏**：脱掉这个 flag 读数只会**变小** ⇒ 棘轮说的是
# 「比基线少了 8 个，记账可以收得更紧」⇒ 照着 `--update` 就把错的数烤进账里了。
# 棘轮自己看不见这个形状（"变好"本来就不报红），所以只能由这里看着。
# 读**程序**用 AST（别用正则扒源码文本——本仓为此踩过三次，见 `_strip_comments` 一族）。
_tg_src = _gate.read_text(encoding="utf-8")
_tgt = ast.parse(_tg_src)
_pe_lists = [n for n in ast.walk(_tgt)
             if isinstance(n, ast.List) and any(
                 isinstance(e, ast.Constant) and e.value == "--python-executable"
                 for e in n.elts)]
check("  对照：源码里确实有 `--python-executable` 那个字面量（否则下面两条是空跑）",
      len(_pe_lists) == 1, f"扫到 {len(_pe_lists)} 处")
# 值必须是 `sys.executable`（**启动本脚本的那个解释器**），不是写死的路径、也不是
# `"python3"`：写死就失去了"跟着启动器走"这个唯一的用处。
_paired = [n for n in _pe_lists if any(
    isinstance(e, ast.Attribute) and e.attr == "executable"
    and isinstance(e.value, ast.Name) and e.value.id == "sys" for e in n.elts)]
check("⭐ mypy 命令恒带 `--python-executable sys.executable`（把「去哪找三方库」钉在启动器上）",
      len(_paired) == 1,
      "有字面量但没有绑 sys.executable" if _pe_lists else "没有这一项")
# 光有 flag 还不够：`--python-executable` 一旦指向**版本不同的**解释器，量到的就是
# 「3.10 的库 + 3.12 的语法」这第四个量。所以要有一条守卫**去读运行解释器自己的版本号**
# 与 `python_target` 对撞（`--update` 路径也必须过）。
# 只认「读了 sys.version_info」这个形状，不认某一种写法：`sys.version_info[0]`、
# `sys.version_info.major`、`sys.version_info[:2]` 都算——判据要钉的是"读了版本"，
# 不是"必须用我偏爱的那种读法"（钉写法会把等价改写判红）。


def _reads_runtime_version(tree: ast.AST) -> bool:
    def _is_version_info(node: ast.AST) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr == "version_info"
                and isinstance(node.value, ast.Name) and node.value.id == "sys")
    for n in ast.walk(tree):
        if isinstance(n, ast.Subscript) and _is_version_info(n.value):
            return True
        if isinstance(n, ast.Attribute) and n.attr in ("major", "minor") \
                and _is_version_info(n.value):
            return True
    return False


check("⭐ 有「启动器解释器版本 == python_target」的守卫（否则量的是「旧库 + 新语法」）",
      _reads_runtime_version(_tgt), "源码里没有读 sys.version_info 的地方")
# 上面那条必须在 `--update` 路径上也跑得到：把错的读数烤进基线走的正是这条路。
# ⚠️ 判这条要认**调用点**、不能搜函数名——第一版就是搜 `_launcher_guard(`，结果被
# `def _launcher_guard(...)` 自己满足了（定义在 `main()` 之前 ⇒ 恒在 `if args.update:`
# 之前 ⇒ 把调用整行删掉也全绿）。**是反控当场戳穿的**，不是读出来的。
_upd_ifs = [n for n in ast.walk(_tgt)
            if isinstance(n, ast.If) and isinstance(n.test, ast.Attribute)
            and n.test.attr == "update"]
_guard_calls = [n for n in ast.walk(_tgt)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_launcher_guard" and hasattr(n, "lineno")]
check("  `_launcher_guard` 的**调用**排在 `if args.update:` 之前（否则 `--update` 是后门）",
      bool(_upd_ifs) and bool(_guard_calls)
      and min(c.lineno for c in _guard_calls) < min(u.lineno for u in _upd_ifs),
      f"调用 {[c.lineno for c in _guard_calls]} vs `if args.update:` "
      f"{[u.lineno for u in _upd_ifs]}")

# ⚠️ **第四类静默失效：账上留着一个盘上不存在的文件**（20261018 补）。
# `by_file` 的键只被用来做 `get(f, 0)` ⇒ 改名/删除之后旧键永远留在账上：它不出现在
# `grew` 里、也不影响 `total`（总数取自 mypy 的汇总行），只会落进那句「这些文件已经清零，
# 可以销账」里被**当成好消息报出来**。真正的后果是同一个文件改名之后计数从 0 重新开始
# ——一次改名买回一份免费额度（现场：`agent/adminops.py` 拆成 8 个模块）。
# 判据分两层，两层都要：① 盘上那份账**现在**没有幽灵键；② 那条守卫**真的会拦**。
# 少了 ②，把守卫整段删掉也全绿——本文件上面刚记过同形状的教训（搜函数名会被定义本身满足）。
_b = json.loads(_base.read_text(encoding="utf-8")) if _base.is_file() else {}
_ghosts = sorted(k for k in (_b.get("by_file") or {}) if not (ROOT / k).is_file())
check("⭐ 基线里的每个文件键在盘上都存在（改名/删除的残留会被读成「已清零」）",
      not _ghosts, f"盘上没有：{_ghosts}")

_spec = importlib.util.spec_from_file_location("_type_gate_under_test", _gate)
_tg_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tg_mod)          # 只跑模块体（常量 + 定义），无副作用
_sink = io.StringIO()
with contextlib.redirect_stdout(_sink):
    _real_ok = _tg_mod._baseline_guards(_b, _tg_mod._pinned_mypy(), _tg_mod._pinned_python())
check("  正控：真账喂进去必须放行（否则下面那条是「见谁都红」）",
      _real_ok is True, f"返回 {_real_ok}；{_sink.getvalue().strip()[:120]}")
_fake = dict(_b)
_fake["by_file"] = dict(_b.get("by_file") or {})
_fake["by_file"]["agent/this_file_was_deleted.py"] = 3
_sink = io.StringIO()
with contextlib.redirect_stdout(_sink):
    _fake_ok = _tg_mod._baseline_guards(_fake, _tg_mod._pinned_mypy(), _tg_mod._pinned_python())
check("  反控：喂一个盘上不存在的键 ⇒ 必须拦（守卫不是装饰）",
      _fake_ok is False, f"返回 {_fake_ok}")

_eval_code2 = "\n".join(ln for ln in EVAL_YML.read_text(encoding="utf-8").splitlines()
                        if not ln.lstrip().startswith("#"))
check("CI 有一步**会执行**的 eval/type_gate.py（不是只在注释里提一句）",
      "eval/type_gate.py" in _eval_code2)
# 与 ①b 同款：会执行的那一行不许自己手写 mypy（`uvx mypy@X` / `mypy==X`），
# 版本只从 pyproject 的 dev 分组来 —— "同一件事写两处"就是改一处忘另一处的形状。
check("  CI 那一行不手写 mypy 版本（版本只从 dev 分组来）",
      "uvx mypy" not in _eval_code2 and "mypy==" not in _eval_code2)

# 本机命令散在注释与文档里写的是 `uvx mypy@X`（隔离环境，不碰产线 venv）：**必须同版**。
# 判据扫**整仓**的源码/文档，不只是 README——`uvx mypy@X` 这轮就落在三个文件里
# （pyproject 注释、eval.yml 注释、type_gate.py 的用法示例）。
# ⚠️ 本文件也在扫描范围内，所以下面这条命令字面**拆开写**：不然它自己是唯一一处
# 逃过判据的落点（自己给自己开后门）。
_MYPY_CMD_RE = re.compile("uvx " + r"mypy@([0-9][0-9A-Za-z.\-]*)")
_SWEEP_EXT = {".py", ".toml", ".md", ".yml", ".yaml", ".sh"}
_SKIP_DIRS = {".git", ".venv", ".mypy_cache", "__pycache__", "node_modules"}
_hits: list[tuple[str, str]] = []
for _p in ROOT.rglob("*"):
    if not _p.is_file() or _p.suffix not in _SWEEP_EXT:
        continue
    if _SKIP_DIRS & set(_p.relative_to(ROOT).parts):
        continue
    for _v in _MYPY_CMD_RE.findall(_p.read_text(encoding="utf-8", errors="ignore")):
        _hits.append((str(_p.relative_to(ROOT)), _v))
check("  对照：整仓确实扫到了 `uvx mypy@X`（否则上面那条是空跑）",
      len(_hits) >= 3, f"{len(_hits)} 处：" + "、".join(f"{f}@{v}" for f, v in _hits))
_bad_ver = [f"{f}@{v}" for f, v in _hits if v != MYPY_PIN]
check(f"  整仓的 `uvx mypy@X` 与 dev 分组同版（{MYPY_PIN}）——升版只改一处就会被这条抓住",
      not _bad_ver, "、".join(_bad_ver))


# ══════════════════════════════════════════════════════════════════════════
print("\n⑤ 覆盖率那一档（`run_all.py --coverage`）：pin 要带 extra，量的面要与打包的面同源")

# pin 写成 `coverage[toml]==X`。20261013 真踩了一次才写下的这条（`run_all.py` 的探针
# 也是那次加的）：当年是 3.10、**没有标准库 TOML**，装了 coverage 却没这个 extra 时
# 它连 `pyproject.toml` 都打不开，而且不是"跳过配置接着跑"，是**构造 Coverage 对象时
# 就抛 ConfigError、退出码 1** ⇒ 每个子套件都退 1，现场看着像"这批代码全坏了"。
# 升到 3.12 后 `tomllib` 进了标准库、这个 extra 变成**多余但无害**（它带的 `tomli`
# 只在 <=3.11 装）。判据**照样留着**：要固定的是「配置必须读得进来」这个形状，
# 而不是"这一版解释器恰好不用装东西"——后者会随解释器版本自己变，正是不该当判据的东西。
_cov_pin = re.search(r'"coverage(\[[^\]]*\])?==([0-9][^"]*)"', _dg_sec)
check("pyproject 的 dev 分组钉了 coverage 版本", _cov_pin is not None,
      _cov_pin.group(2) if _cov_pin else "dependency-groups 里没有钉死的 coverage==")
check("⭐ pin 带 `[toml]` extra（3.12 上它已多余，但写下来的是「配置读得进来」这个形状）",
      _cov_pin is not None and _cov_pin.group(1) == "[toml]",
      _cov_pin.group(0) if _cov_pin else "—")

# 量的面 == 打包的面：`source` 少列一个包，那个包会**整份**从报告里消失（不报错，
# 只是不出现——"哪些文件一直没人碰"这个问题的答案就悄悄少了一块）；多列一个不存在的
# 名字则什么也不量。两处都是"生产包"这一件事，所以判据是**集合相等**。
_run_sec = _section(PYPROJECT, "tool.coverage.run")
_src = re.search(r"^source\s*=\s*\[(?P<v>.*?)\]", _run_sec, re.M | re.S)
COV_SOURCE = re.findall(r'"([^"]+)"', _src.group("v")) if _src else []
check("⭐ `[tool.coverage.run].source` == wheel 包列表（同一件事：生产包）",
      sorted(COV_SOURCE) == sorted(_pkgs),
      f"覆盖={sorted(COV_SOURCE)} 打包={sorted(_pkgs)}")
check("  对照：那一节确实在（正则没匹配到空串时上面那条会「绿得没有理由」）",
      bool(_run_sec.strip()), f"{len(COV_SOURCE)} 项")
# `parallel = true`：子套件是**独立进程**（run_all.py 的形状），不开这个后一个会整份
# 覆盖前一个的数据，报出来永远是"最后一个套件跑了什么"。
check("`[tool.coverage.run].parallel` 开着（否则各子进程互相覆盖）",
      re.search(r"^\s*parallel\s*=\s*true", _run_sec, re.M | re.I) is not None)
check("`[tool.coverage.report]` 在场（`show_missing` 那份人看的表就长在这节里）",
      bool(_section(PYPROJECT, "tool.coverage.report").strip()))
# 并行数据文件的落点：`run_all.py` 里那个目录名与会 .gitignore 里的那条必须对得上——
# 只写一处的话，另一处会以"git status 里一堆新文件"或"报告读到上次的残留"的形式出现。
_gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
check("`.coverage-data/` 既在 run_all.py 里、也在 .gitignore 里（两处同一个名字）",
      "COV_DIR = ROOT / \".coverage-data\"" in RALL and ".coverage-data/" in _gi)
# ⚠️ `coverage run <脚本>` 往 `sys.path` 里放的是**当前目录**、不是脚本目录（`python <脚本>`
# 才是脚本目录）。不显式把 `tests/` 挂上，覆盖率档里所有 `import _parent_repo` 的守卫都会
# 以 ModuleNotFoundError 变红（实测一次：130 个套件里红 17 个，看着像整批守卫坏了）。
check("  run_all.py 给子进程挂上了 `tests/`（否则覆盖率档下同目录助手 import 不到）",
      'env["PYTHONPATH"] = str(TESTS)' in RALL)
# 配方**不许写死解释器路径**：`lib/python3.X/site-packages` 这种写法升一次版本就烂一次，
# 而这一档起不来时是**拒绝运行**（一个套件都不跑）⇒ 症状会是"覆盖率整个用不了"。
# 20261013 从 3.10 升 3.12 时这里正是坏的。改成问包自己装在哪，配方就跟着解释器走。
check("  配方不写死 `lib/python3.X/site-packages`（改问 `coverage.__file__`）",
      "lib/python3." not in RALL and "SITE=$(/tmp/cov/bin/python -c" in RALL)
# 光判头注不够：**真被抄走的是拒绝消息里那三行**（头注是人读的、拒绝消息是发生故障时
# 直接贴进终端的）。两处判同一件事——解释器从 `sys.executable` 来、版本号一个字都不写死。
# `--python {v}` 那一形是 20261013 之前的写法：它让"建临时环境的那一侧"与"跑套件的那一侧"
# 可以是**两个不同版本**，而这正是 `[toml]` extra 那件事按谁家规则来的分岔口。
check("  起不来时打出来的三行配方也**按当前解释器算**（`sys.executable`，不写死版本号）",
      "--python {sys.executable}" in RALL and "--python {v}" not in RALL)


# ══════════════════════════════════════════════════════════════════════════
print("\n⑥ 解释器版本只有一处事实源（六处手写 + 一份生成物，必须是同一个值）")

_pf = re.search(r'^requires-python\s*=\s*">=\s*([0-9]+\.[0-9]+)"', PROJECT_SEC, re.M)
PY_FLOOR = _pf.group(1) if _pf else None
check("pyproject 的 `requires-python` 是个 `>=X.Y` 的下限", PY_FLOOR is not None, str(PY_FLOOR))

_pyver_file = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
check(f"⭐ `.python-version` == requires-python 的下限（{PY_FLOOR}）",
      _pyver_file == PY_FLOOR, f".python-version={_pyver_file}")

_ruff_tv = re.search(r'^target-version\s*=\s*"([^"]+)"',
                     _section(PYPROJECT, "tool.ruff"), re.M)
RUFF_TV = _ruff_tv.group(1) if _ruff_tv else None
check(f"⭐ `[tool.ruff].target-version` == `py{(PY_FLOOR or '').replace('.', '')}`"
      f"（同一个值，去掉小数点）",
      PY_FLOOR is not None and RUFF_TV == "py" + PY_FLOOR.replace(".", ""), str(RUFF_TV))

_mypy_tv = re.search(r'^python_version\s*=\s*"([^"]+)"',
                     _section(PYPROJECT, "tool.mypy"), re.M)
MYPY_TV = _mypy_tv.group(1) if _mypy_tv else None
check(f"⭐ `[tool.mypy].python_version` == {PY_FLOOR}（这一项决定 mypy 报哪一套错误）",
      MYPY_TV == PY_FLOOR, str(MYPY_TV))

# ⚠️ 这一条的存在本身就是结论：**CI 装的解释器与代码声明的必须是同一个**。只声明不验
# ⇒ "升级"这件事没有任何东西验过（CI 拿 3.10 跑一套声明 3.12 的代码，绿得毫无意义）；
# 只验不声明 ⇒ 装的版本迟早与写着的分叉。
_ci_py = re.search(r'^\s*python-version:\s*"([^"]+)"', _eval_code2, re.M)
CI_PY = _ci_py.group(1) if _ci_py else None
check(f"⭐ CI 的 `python-version` == {PY_FLOOR}",
      CI_PY == PY_FLOOR, f"CI={CI_PY}")

if _base.is_file():
    # 与 ④ 的 mypy 版本同款：这两项都**改变错误集**，换了就必须重出账。
    check(f"⭐ 棘轮基线的 `python_target` == {PY_FLOOR}（换目标就换了一套错误集）",
          _b.get("python_target") == PY_FLOOR, str(_b.get("python_target")))

# 第七处，但它**不是手写的那六处之一**：`uv.lock` 是 `uv lock` 生成的，里面同样记着
# `requires-python`（还有由它算出来的 `resolution-markers`，例如 `tomli` 那条
# `python_full_version <= '3.11'` 的 marker）。**改了声明没重跑 `uv lock`** 会当场红在
# CI 的 `uv sync --frozen`（它拒绝在锁过期时跑）——但那要等到 CI；本机没有 `uv sync`
# 这条出口（`.venv` 是产线 venv），等于本地无处可验。所以这里直接把两处字面钉在一起。
_ul = re.search(r'^requires-python\s*=\s*">=\s*([0-9]+\.[0-9]+)"',
                (ROOT / "uv.lock").read_text(encoding="utf-8"), re.M)
UVLOCK_PY = _ul.group(1) if _ul else None
check(f"⭐ `uv.lock` 的 `requires-python` == {PY_FLOOR}（生成物与声明要同源）",
      UVLOCK_PY == PY_FLOOR, f"uv.lock={UVLOCK_PY}")


# ══════════════════════════════════════════════════════════════════════════
print("\n⑦ 静态检查棘轮真的接在 CI 上，且它的三个量都只有一处事实源")

_lint_gate = ROOT / "eval" / "lint_gate.py"
_lint_base = ROOT / "eval" / "lint_baseline.json"
check("eval/lint_gate.py 在位", _lint_gate.is_file())
check("eval/lint_baseline.json 在位（棘轮的量就在这份账上）", _lint_base.is_file())

# 版本按 dev 分组体取，理由同 ④ 里那段（别写 `dev = ["ruff==X"]` 那种一次就坏的正则）。
RUFF_PIN = (re.search(r'"ruff==([0-9][^"]*)"', _dg_sec).group(1)
            if re.search(r'"ruff==([0-9][^"]*)"', _dg_sec) else None)
check("pyproject 的 dev 分组钉了 ruff 版本", RUFF_PIN is not None,
      RUFF_PIN or "dependency-groups 里没有钉死的 ruff==")

if _lint_base.is_file():
    _lb = json.loads(_lint_base.read_text(encoding="utf-8"))
    # 三个量都是"换了它就不是同一个量"（同 ④ 里 mypy 版本那一条的道理）：
    # ruff 版本换规则实现；line-length 一条 E501 就占掉全仓六成条目；select 多一族当场多一片。
    check(f"⭐ 基线的 ruff 版本 == dev 分组钉的（{RUFF_PIN}）",
          _lb.get("ruff_version") == RUFF_PIN,
          f"基线={_lb.get('ruff_version')} dev={RUFF_PIN}")
    _ruff_sec = _section(PYPROJECT, "tool.ruff")
    _ll = re.search(r"^line-length\s*=\s*(\d+)", _ruff_sec, re.M)
    check(f"⭐ 基线的 line-length == pyproject 的（{_ll.group(1) if _ll else None}）",
          _ll is not None and _lb.get("line_length") == int(_ll.group(1)),
          f"基线={_lb.get('line_length')} pyproject={_ll.group(1) if _ll else None}")
    # ⚠️ `select` 住在 `[tool.ruff.lint]`，**不是** `[tool.ruff]`——`_section` 按行首 `[`
    # 截断，取错那一节会读到空串、判据当场红成"pyproject 是 None"（这条就是这么抓到的）。
    _sel = re.search(r"^select\s*=\s*\[([^\]]*)\]",
                     _section(PYPROJECT, "tool.ruff.lint"), re.M)
    _sel_list = (re.findall(r'"([^"]+)"', _sel.group(1)) if _sel else None)
    check(f"⭐ 基线的 select == pyproject 的（{_sel_list}）",
          _sel_list is not None and _lb.get("select") == _sel_list,
          f"基线={_lb.get('select')} pyproject={_sel_list}")
    check("  基线里记着读数（total 与逐文件计数都在）",
          isinstance(_lb.get("total"), int) and isinstance(_lb.get("by_file"), dict)
          and bool(_lb["by_file"]), f"total={_lb.get('total')}")
    # 逐文件计数之和必须等于 total：两个口径分岔时棘轮会悄悄量另一个东西
    # （`lint_gate._counts` 里那条 ValueError 是运行时的同一道，这里是落盘的这一道）。
    check("  逐文件计数之和 == total（账自己得自洽）",
          sum(_lb["by_file"].values()) == _lb["total"],
          f"和={sum(_lb['by_file'].values())} total={_lb['total']}")

check("CI 有一步**会执行**的 eval/lint_gate.py（不是只在注释里提一句）",
      "eval/lint_gate.py" in _eval_code2)
check("  CI 那一行不手写 ruff 版本（版本只从 dev 分组来）",
      "uvx ruff" not in _eval_code2 and "ruff==" not in _eval_code2)
# 上面那条 `_eval_code2` 是**剥掉注释**之后的正文——本仓踩过"只在注释里提一句也算接线"
# 的坑（`test_ci_suite_list.py` 头注那一段）。这里再补一条反控：文档里那两条待建/已建的
# 措辞必须真的翻面，否则"落地说"只活在提交信息里。
_lbd = (ROOT / "docs/lint-baseline.md").read_text(encoding="utf-8")
check("  docs/lint-baseline.md 不再写着「机械闸（待建）」",
      "机械闸（**待建**）" not in _lbd and "eval/lint_gate.py" in _lbd)


# ══════════════════════════════════════════════════════════════════════════
print()
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print("✅ 全绿")
