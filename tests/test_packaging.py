# -*- coding: utf-8 -*-
"""工程配置的三条判据（20261013，行业规范收口批 2）。

**为什么单起一套**：这三处此前的失效形状**一模一样**——"文件里写着一句话，而那句
话没有任何东西看着"：

  ① `pyproject.toml` 的 wheel 包列表列了并不存在的 `chains/`、漏了生产代码真的
     import 的 `rag/`。列错**不会报错**，因为本仓从不构建 wheel（部署走 git pull）。
  ② `server.py` 的 `version="1.0.0"` 与 `pyproject.toml` 的 `0.2.0` **差着两个大版本**
     ——`/openapi.json` 上挂着哪个，全看读的是哪一份。
  ③ `SECURITY.md` 把上游 CVE 明确推给"Dependabot"，而仓库里从来没有
     `.github/dependabot.yml` ⇒ 那句承诺当时是空的（依赖又全 `==` 钉死，没有它
     什么都不会升级）。

判据一律读**磁盘事实**（目录在不在、源码里还有没有第二份字面量），不读"文档里写了
这一条"——后者的失效方式正是"名单对了、事情没做"。

用法：.venv/bin/python tests/test_packaging.py
"""

from __future__ import annotations

import ast
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

    刻意不引 toml 解析器：本机 Python 是 3.10（没有 `tomllib`），而为一处配置断言
    添一个依赖是本末倒置。这里的用法只有"读一个节里的一个字符串键"，剥掉注释后
    正则够用；真正需要机器读 TOML 的地方（`uv` 本身）读的是同一个文件。
    """
    m = re.search(rf"^\[{re.escape(header)}\](?P<body>.*?)(?=^\[|\Z)", text, re.M | re.S)
    return _strip_comments(m.group("body")) if m else ""


PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
PROJECT_SEC = _section(PYPROJECT, "project")
WHEEL_SEC = _section(PYPROJECT, "tool.hatch.build.targets.wheel")

_m = re.search(r'^version\s*=\s*"([^"]+)"', PROJECT_SEC, re.M)
PROJECT_VERSION = _m.group(1) if _m else None

# ══════════════════════════════════════════════════════════════════════════
print("① wheel 包列表 == 磁盘事实（列错不会报错，所以必须有人看着）")

_pkgs = re.findall(r'"([^"]+)"', WHEEL_SEC)
check("pyproject 里有 wheel 包列表", bool(_pkgs), WHEEL_SEC.strip()[:60])
check("每一项都是**真实存在**的目录（`chains/` 就是这么被写进去的）",
      all((ROOT / p).is_dir() for p in _pkgs),
      "、".join(p for p in _pkgs if not (ROOT / p).is_dir()))

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
print()
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print("✅ 全绿")
