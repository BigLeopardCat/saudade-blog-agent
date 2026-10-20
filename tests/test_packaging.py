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


def test_packaging():
    PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    PROJECT_SEC = _section(PYPROJECT, "project")
    EVAL_YML = ROOT / ".github" / "workflows" / "eval.yml"
    WHEEL_SEC = _section(PYPROJECT, "tool.hatch.build.targets.wheel")
    RALL = (ROOT / "tests" / "run_all.py").read_text(encoding="utf-8")
    _m = re.search(r'^version\s*=\s*"([^"]+)"', PROJECT_SEC, re.M)
    PROJECT_VERSION = _m.group(1) if _m else None
    print("① wheel 包列表 == 磁盘事实（列错不会报错，所以必须有人看着）")
    _pkgs = re.findall(r'"([^"]+)"', WHEEL_SEC)
    check("pyproject 里有 wheel 包列表", bool(_pkgs), WHEEL_SEC.strip()[:60])
    check("每一项都是**真实存在**的目录（`chains/` 就是这么被写进去的）",
          all((ROOT / p).is_dir() for p in _pkgs),
          "、".join(p for p in _pkgs if not (ROOT / p).is_dir()))
    _implicit = [p for p in _pkgs if not (ROOT / p / "__init__.py").is_file()]
    check("每一项都有 `__init__.py`（隐式命名空间包会让 mypy 拒绝整个目录）",
          not _implicit, "、".join(_implicit))
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
    print("\n④ 类型检查棘轮真的接在 CI 上，且 mypy 版本只有一处事实源")
    _gate = ROOT / "eval" / "type_gate.py"
    _base = ROOT / "eval" / "mypy_baseline.json"
    check("eval/type_gate.py 在位", _gate.is_file())
    check("eval/mypy_baseline.json 在位（棘轮的量就在这份账上）", _base.is_file())
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
    _tg_src = _gate.read_text(encoding="utf-8")
    _tgt = ast.parse(_tg_src)
    _pe_lists = [n for n in ast.walk(_tgt)
                 if isinstance(n, ast.List) and any(
                     isinstance(e, ast.Constant) and e.value == "--python-executable"
                     for e in n.elts)]
    check("  对照：源码里确实有 `--python-executable` 那个字面量（否则下面两条是空跑）",
          len(_pe_lists) == 1, f"扫到 {len(_pe_lists)} 处")
    _paired = [n for n in _pe_lists if any(
        isinstance(e, ast.Attribute) and e.attr == "executable"
        and isinstance(e.value, ast.Name) and e.value.id == "sys" for e in n.elts)]
    check("⭐ mypy 命令恒带 `--python-executable sys.executable`（把「去哪找三方库」钉在启动器上）",
          len(_paired) == 1,
          "有字面量但没有绑 sys.executable" if _pe_lists else "没有这一项")
    check("⭐ 有「启动器解释器版本 == python_target」的守卫（否则量的是「旧库 + 新语法」）",
          _reads_runtime_version(_tgt), "源码里没有读 sys.version_info 的地方")
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
    _tg_guard_calls = [n for n in ast.walk(_tgt)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "_baseline_guards"]
    check("  反控：main() 真的**调用**了 `_baseline_guards`（否则守卫是死代码）",
          bool(_tg_guard_calls), "" if _tg_guard_calls else "源码里没有调用点")
    _eval_code2 = "\n".join(ln for ln in EVAL_YML.read_text(encoding="utf-8").splitlines()
                            if not ln.lstrip().startswith("#"))
    check("CI 有一步**会执行**的 eval/type_gate.py（不是只在注释里提一句）",
          "eval/type_gate.py" in _eval_code2)
    check("  CI 那一行不手写 mypy 版本（版本只从 dev 分组来）",
          "uvx mypy" not in _eval_code2 and "mypy==" not in _eval_code2)
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
    print("\n⑤ 覆盖率那一档（`run_all.py --coverage`）：pin 要带 extra，量的面要与打包的面同源")
    _cov_pin = re.search(r'"coverage(\[[^\]]*\])?==([0-9][^"]*)"', _dg_sec)
    check("pyproject 的 dev 分组钉了 coverage 版本", _cov_pin is not None,
          _cov_pin.group(2) if _cov_pin else "dependency-groups 里没有钉死的 coverage==")
    check("⭐ pin 带 `[toml]` extra（3.12 上它已多余，但写下来的是「配置读得进来」这个形状）",
          _cov_pin is not None and _cov_pin.group(1) == "[toml]",
          _cov_pin.group(0) if _cov_pin else "—")
    _run_sec = _section(PYPROJECT, "tool.coverage.run")
    _src = re.search(r"^source\s*=\s*\[(?P<v>.*?)\]", _run_sec, re.M | re.S)
    COV_SOURCE = re.findall(r'"([^"]+)"', _src.group("v")) if _src else []
    check("⭐ `[tool.coverage.run].source` == wheel 包列表（同一件事：生产包）",
          sorted(COV_SOURCE) == sorted(_pkgs),
          f"覆盖={sorted(COV_SOURCE)} 打包={sorted(_pkgs)}")
    check("  对照：那一节确实在（正则没匹配到空串时上面那条会「绿得没有理由」）",
          bool(_run_sec.strip()), f"{len(COV_SOURCE)} 项")
    check("`[tool.coverage.run].parallel` 开着（否则各子进程互相覆盖）",
          re.search(r"^\s*parallel\s*=\s*true", _run_sec, re.M | re.I) is not None)
    check("`[tool.coverage.report]` 在场（`show_missing` 那份人看的表就长在这节里）",
          bool(_section(PYPROJECT, "tool.coverage.report").strip()))
    _gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    check("`.coverage-data/` 既在 run_all.py 里、也在 .gitignore 里（两处同一个名字）",
          "COV_DIR = ROOT / \".coverage-data\"" in RALL and ".coverage-data/" in _gi)
    check("  run_all.py 给子进程挂上了 `tests/`（否则覆盖率档下同目录助手 import 不到）",
          'env["PYTHONPATH"] = str(TESTS)' in RALL)
    check("  配方不写死 `lib/python3.X/site-packages`（改问 `coverage.__file__`）",
          "lib/python3." not in RALL and "SITE=$(/tmp/cov/bin/python -c" in RALL)
    check("  起不来时打出来的三行配方也**按当前解释器算**（`sys.executable`，不写死版本号）",
          "--python {sys.executable}" in RALL and "--python {v}" not in RALL)
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
    _ci_py = re.search(r'^\s*python-version:\s*"([^"]+)"', _eval_code2, re.M)
    CI_PY = _ci_py.group(1) if _ci_py else None
    check(f"⭐ CI 的 `python-version` == {PY_FLOOR}",
          CI_PY == PY_FLOOR, f"CI={CI_PY}")
    if _base.is_file():
        # 与 ④ 的 mypy 版本同款：这两项都**改变错误集**，换了就必须重出账。
        check(f"⭐ 棘轮基线的 `python_target` == {PY_FLOOR}（换目标就换了一套错误集）",
              _b.get("python_target") == PY_FLOOR, str(_b.get("python_target")))
    _ul = re.search(r'^requires-python\s*=\s*">=\s*([0-9]+\.[0-9]+)"',
                    (ROOT / "uv.lock").read_text(encoding="utf-8"), re.M)
    UVLOCK_PY = _ul.group(1) if _ul else None
    check(f"⭐ `uv.lock` 的 `requires-python` == {PY_FLOOR}（生成物与声明要同源）",
          UVLOCK_PY == PY_FLOOR, f"uv.lock={UVLOCK_PY}")
    print("\n⑦ 静态检查棘轮真的接在 CI 上，且它的三个量都只有一处事实源")
    _lint_gate = ROOT / "eval" / "lint_gate.py"
    _lint_base = ROOT / "eval" / "lint_baseline.json"
    check("eval/lint_gate.py 在位", _lint_gate.is_file())
    check("eval/lint_baseline.json 在位（棘轮的量就在这份账上）", _lint_base.is_file())
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
        # 幽灵键（20261018 补，与 ④ 的 type_gate 同一族）：账里**有**、盘上却没有的键。
        # 它不进 `lint_gate.judge()` 的任何一条判据、也不影响 `total`（total 是 ruff 汇总行的
        # 数），只会让账变脏，还被"已经清零，可以销账"那句报成好消息。判据同样两层：
        # ① 盘上这份账现在没有幽灵键；② 那条守卫真的会拦 + `main()` 真的调用了它。
        _lg_spec = importlib.util.spec_from_file_location("_lint_gate_under_test", _lint_gate)
        _lg_mod = importlib.util.module_from_spec(_lg_spec)
        _lg_spec.loader.exec_module(_lg_mod)        # 只跑模块体（常量 + 定义），无副作用
        _lg_ghosts = _lg_mod.ghost_keys(_lb.get("by_file") or {})
        check("⭐ 基线里的每个文件键在盘上都存在（删除的残留会被读成「已清零」）",
              not _lg_ghosts, f"盘上没有：{_lg_ghosts}")
        _lg_bad = _lg_mod.ghost_keys({"eval/this_file_was_deleted.py": 3})
        check("  反控：喂一个盘上不存在的键 ⇒ 必须被抓到",
            bool(_lg_bad), "" if _lg_bad else "返回了空")
        _lg_ok = _lg_mod.ghost_keys({"eval/lint_gate.py": 1})
        check("  反控：盘上**有**的键不许被抓（否则是「见谁都红」）", not _lg_ok,
              "" if not _lg_ok else "把真文件判成幽灵了")
        _lg_tree = ast.parse(_lint_gate.read_text(encoding="utf-8"))
        _lg_calls = [n for n in ast.walk(_lg_tree)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == "ghost_keys"]
        check("  反控：main() 真的**调用**了 `ghost_keys`（否则守卫是死代码）",
              bool(_lg_calls), "" if _lg_calls else "源码里没有调用点")
    check("CI 有一步**会执行**的 eval/lint_gate.py（不是只在注释里提一句）",
          "eval/lint_gate.py" in _eval_code2)
    check("  CI 那一行不手写 ruff 版本（版本只从 dev 分组来）",
          "uvx ruff" not in _eval_code2 and "ruff==" not in _eval_code2)
    _lbd = (ROOT / "docs/lint-baseline.md").read_text(encoding="utf-8")
    check("  docs/lint-baseline.md 不再写着「机械闸（待建）」",
          "机械闸（**待建**）" not in _lbd and "eval/lint_gate.py" in _lbd)
    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
        sys.exit(1)
    print("✅ 全绿")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_packaging()
