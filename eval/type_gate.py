# -*- coding: utf-8 -*-
"""类型检查棘轮：mypy 的错误数**只许变小**（20261013，行业规范收口批 2b）。

**为什么是棘轮而不是门禁**：`[tool.mypy]` 那段配置在此之前**从不执行**——不在依赖、
不在 `uv.lock`、CI 没有这一步（CI 只有 `ruff --select F821` 一条）。配上以来第一次真跑
（20261013）读到 `220 errors in 21 files (checked 46)`：数量不大，但一次清零要动
21 个文件、其中 `agent/graph.py` 一个就 99 个，且大半是 `arg-type` / `attr-defined`
（langgraph / langchain / openai 那几族对象没有类型信息）。一上来就当门禁 ⇒ CI 恒红 ⇒
人人学会无视它。所以照 `tests/test_complexity_budget.py` + `eval/complexity_baseline.json`
同款：**记下当前读数，只许它变小**。

**判据的形状**（两条，任一成立即红）：
  ① 总数变大；
  ② **某个文件**变大——防"这边修了三个、那边坏了两个"互相抵消（总数看不出来）。
变好**不**报红（标准的棘轮行为：绿着，并提示你可以 `--update` 把账收得更紧）。
`--update` 是唯一的放宽通道，它会连"已经清零的文件"一起销掉。

**版本必须与 baseline 同源**：换 mypy 版本 ⇒ 报出来的错误集会变 ⇒ 棘轮不再是同一个量。
`baseline.mypy_version` 与 `pyproject.toml` 里 dev 组钉的版本对不上就直接退 3
（`tests/test_packaging.py` ④ 也断言这一条，两道）。

**⚠️ 上面那一条只比了"账 vs 钉住的版本"，漏了"**真正在跑的那个**"（20261020 补）**：
`--mypy` 传一个别的版本（形如 `uvx mypy@<另一个版本>`）过去会一路走到判据里，报出来的是
`agent/graph.py 81（基线 79）—— 这一处变多了` + 总数变大 ⇒ **退 1**。于是"换了一个量"
被读成"代码变差了"，而扫描行还把 `pin` 当版本印出来，日志上完全看不出区别。
同族的 `lint_gate` / `except_gate` 都读运行版本（`ruff --version`），只有这里漏了。
现在先读 `mypy --version`，与钉住的版本不一致**退 3**（不是 1），扫描行也改印运行版本。
（这段说明刻意不写具体版本号：`tests/test_packaging.py` 会整仓扫 `uvx mypy@X` 并要求
全部同版，写一个"错的例子"会被自己的说明文字绊倒。）

**解释器目标同理，而且它是后补的洞（20261013 升 3.12 时发现）**：本脚本调 mypy 时
**不传 `--python-version`** ⇒ 目标只来自 `pyproject.toml` 的 `[tool.mypy].python_version`。
而那一项**会改变报出来的错误集**（3.11+ 的 `Self`/`ExceptionGroup`、3.12 的泛型语法…）。
基线当时只记 `mypy_version` ⇒「把 3.10 改成 3.12」这种**换了一个量**的改动，在棘轮眼里
和「什么都没动」长得一模一样（错误数没涨就不报）。现在基线里也记 `python_target`，
对不上同样退 3。

**第三个量是「mypy 从哪看到三方库」，它比上面两个更隐蔽（20261013 上线当天被 CI 抓到）**：
mypy 靠 `--python-executable` 决定去谁的 site-packages 里找 `langgraph` / `openai` /
`fastapi`；不传时它就认**自己跑在哪个环境里**。于是同一条命令、同一个 mypy 版本、同一个
`python_version`，换个启动器读数就变：

    <项目解释器> -m mypy      → 220（找得到三方库 ⇒ 它们有真类型 ⇒ 报得出问题）
    uvx mypy@2.4.0 …          → 212（uvx 是个只装了 mypy 的隔离环境 ⇒ 三方 import
                                全落到 `ignore_missing_imports` ⇒ 退化成 Any ⇒ 少报 8 个）

最初那份 212 的基线就是这么量出来的（文档当时还把它当成"本机标准做法"写在这里），
推上 CI 当场红：**基线的读数取决于你怎么启动 mypy**，这正是棘轮最不该有的性质。
现在两条一起上：命令里**恒带** `--python-executable <本进程解释器>`（把"去哪找三方库"
钉死在启动 type_gate 的那个环境上），并且**要求这个环境的版本 == `python_target`**
（否则量的是"3.10 的库 + 3.12 的语法"，是第四个量）。

用法（⚠️ 本机 `.venv` 是**产线 venv**、不许 `uv sync` ⇒ 本机用隔离环境跑同一个版本，
  但**必须用隔离环境的解释器去启动本脚本**，让 `sys.executable` 指向它）：
    /tmp/xxx312/bin/python eval/type_gate.py --mypy "uvx mypy@2.4.0"   # 本机
    python eval/type_gate.py                       # CI：dev 组已装好，走 sys.executable -m mypy
    python eval/type_gate.py --update              # 重出 baseline

**第四类静默失效是账上留着一个不存在的文件（20261018 补）**：`by_file` 的键只被用来做
`get(f, 0)`，所以**改名/删除**之后旧键会永远留在账上——它不出现在 `grew` 里、也不影响
`total`（总数取自 mypy 的汇总行），只会被当成「这些文件已经清零，可以销账」**报成好消息**。
真正的后果是同一个文件改名之后计数从 0 重新开始：一次改名买回一份免费额度，与棘轮想买的
东西正好相反（现场：`agent/adminops.py` 拆成 8 个模块）。现在盘上找不到的键直接退 3。

退出码：0 通过 / 1 变差 / 3 跑不起来或**账不可用**（缺 mypy、汇总行认不出、版本 /
        解释器目标 / 启动器解释器版本对不上、账里有盘上不存在的键）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shlex
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE = ROOT / "eval" / "mypy_baseline.json"

# 查什么：**生产代码**。`tests/`、`eval/`、`scripts/` 不进棘轮——它们是判据与工具，
# 类型错误不会伤到线上；而且 `eval/` 自己就在跑这个棘轮，纳入会自我指涉。
TARGETS = ["agent", "config", "models", "rag", "tools", "utils", "server.py"]

_ERR_RE = re.compile(r"^(?P<file>[^:\n]+):\d+(?::\d+)?: error: ")
_SUM_RE = re.compile(r"^Found (?P<n>\d+) errors? in (?P<files>\d+) files? "
                     r"\(checked (?P<checked>\d+) ", re.M)
_OK_RE = re.compile(r"^Success: no issues found in (?P<checked>\d+) ", re.M)
# `mypy --version` 的一行：`mypy 2.4.0 (compiled: yes)`。
_VER_RE = re.compile(r"^mypy (?P<v>[0-9][^\s]*)", re.M)


def _pinned_mypy() -> str | None:
    """从 `pyproject.toml` 的 dev 组里读出钉住的 mypy 版本（不引 toml 解析器）。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'"mypy==([0-9][^"]*)"', text)
    return m.group(1) if m else None


def _pinned_python() -> str | None:
    """从 `[tool.mypy]` 里读出 `python_version`（不引 toml 解析器）。

    只取那一节的正文：`python_version` 这个名字在本文件别处也可能作为**注释里的文字**
    出现，整篇搜会把注释当取值读（本仓踩过同形状的坑，见 `test_packaging._strip_comments`）。
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"^\[tool\.mypy\](?P<body>.*?)(?=^\[|\Z)", text, re.M | re.S)
    if not m:
        return None
    body = "\n".join(ln.split("#", 1)[0] for ln in m.group("body").splitlines())
    v = re.search(r'^python_version\s*=\s*"([^"]+)"', body, re.M)
    return v.group(1) if v else None


def _counts(out: str) -> tuple[dict[str, int], int, int]:
    """mypy 的文本输出 ⇒（逐文件计数, 总数, 查过的文件数）。

    认不出汇总行就抛——**不许**拿逐行加出来的数当总数顶上去：mypy 的汇总行里
    「checked N source files」与逐行命中的文件数是两回事，静默换一个口径会让棘轮
    悄悄量错东西。
    """
    by_file: dict[str, int] = {}
    for line in out.splitlines():
        m = _ERR_RE.match(line)
        if m:
            by_file[m.group("file")] = by_file.get(m.group("file"), 0) + 1
    s = _SUM_RE.search(out)
    if s:
        return by_file, int(s.group("n")), int(s.group("checked"))
    # 一条错误都没有时 mypy 不打印汇总行，只印 `Success: …`——不认这一行的话，
    # **全清的代码库会让棘轮退 3**（"跑不起来"），那等于把胜利判成故障。
    ok = _OK_RE.search(out)
    if ok and not by_file:
        return {}, 0, int(ok.group("checked"))
    raise ValueError("mypy 的输出里认不出汇总行（格式变了？）")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd + TARGETS, cwd=ROOT, capture_output=True, text=True)


def _gather(prog: list[str]) -> tuple[dict[str, int], int, int]:
    r = _run(prog)
    out = (r.stdout or "") + (r.stderr or "")
    if "No module named mypy" in out or not out.strip():
        raise RuntimeError(
            "跑不起来：" + " ".join(prog) + "\n"
            "  本机（产线 venv、不许 uv sync）请用**隔离环境的解释器**启动本脚本，再让它去"
            '  跑同一个版本：\n    <隔离 3.12 环境>/bin/python eval/type_gate.py --mypy "uvx mypy@'
            + str(_pinned_mypy()) + '"')
    return _counts(out)


def _write_baseline(by_file: dict[str, int], total: int, checked: int) -> None:
    BASELINE.write_text(json.dumps({
        "_note": "mypy 错误数的棘轮基线。只许变小；--update 是唯一放宽通道。"
                 "三个量都必须与 pyproject.toml / 启动器同源：mypy 版本、解释器目标、"
                 "以及「mypy 照哪个解释器去找三方库」（命令里恒带 --python-executable "
                 "<启动本脚本的解释器>）——三者都会改变错误集。",
        "mypy_version": _pinned_mypy(),
        "python_target": _pinned_python(),
        "targets": TARGETS,
        "total": total,
        "files_checked": checked,
        "by_file": dict(sorted(by_file.items())),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _launcher_guard(py_target: str | None) -> bool:
    """③ 启动器解释器的版本（见模块 docstring 第三条）。`--python-executable` 把三方库的
    解析钉在**启动本脚本的解释器**上 ⇒ 它的版本必须与语法目标同源，否则量的是
    「3.10 的库 + 3.12 的语法」——**第四个量**，而它连基线都记不下来（在同一个 CI 上是
    恒定的，所以只有本机换启动器时才现形）。

    **`--update` 路径也必须过这一道**：否则正是这条路能把一个错的读数烤进基线
    （212 那份就是这么来的）。
    """
    launcher = f"{sys.version_info[0]}.{sys.version_info[1]}"
    if py_target and launcher != py_target:
        print(f"❌ 本脚本跑在 Python {launcher} 上，而 `[tool.mypy].python_version` 是 "
              f"{py_target} —— mypy 会**照这个解释器**去找三方库，于是正在量的是"
              f"「{launcher} 的库 + {py_target} 的语法」。用 {py_target} 的解释器重新启动"
              f"（本机：在隔离环境里跑，见模块 docstring）。")
        return False
    return True


def _baseline_guards(base: dict, pin: str | None, py_target: str | None) -> bool:
    """①②③ 与基线对着判的三道。**必须排在 `_gather()` 之前**——退 3 不该付一次完整 mypy
    跑（约一分钟）的代价，而这三条只读文本；反控"改一格 ⇒ 当场红"也就从一分钟变成一瞬。

    三条的共同点：**账本身不可用了**（换了一个量 / 记着一个不存在的文件），所以都是
    "先 `--update` 重出"而不是"你变差了"⇒ 退 3 不是退 1。
    """
    # ① 版本对不上 ⇒ 退 3 而不是 1：这不是"变差了"，是**换了一个量**。
    if base.get("mypy_version") != pin:
        print(f"❌ 基线是用 mypy {base.get('mypy_version')} 出的，"
              f"而 dev 组钉的是 {pin} —— 换版本就换了一套错误集，先 `--update` 重出。")
        return False

    # ② 同款、同因：解释器目标也换错误集。**多判这一条是本脚本 20261013 才补上的**——
    # 在此之前「3.10 改 3.12」在棘轮眼里是隐形改动（旧基线里根本没有这个键 ⇒ 这里会
    # 因为 `None != "3.12"` 而退 3，正是要的效果：逼你先 `--update` 换一套账）。
    if base.get("python_target") != py_target:
        print(f"❌ 基线是用 Python {base.get('python_target')} 的目标出的，"
              f"而 `[tool.mypy].python_version` 现在是 {py_target} —— 换解释器目标同样换"
              f"了一套错误集，先 `--update` 重出。")
        return False

    # ③ 台账里有、盘上却没有的键（20261018 补）。**这一族坏起来比①②更安静**：
    # `by_file` 的键只被用来做 `base["by_file"].get(f, 0)`，于是**改名或删除**之后旧键
    # 永远留在账上——它既不出现在 `grew` 里、也不影响 `total`（总数取自 mypy 的汇总行），
    # 只会落进下面那句"这些文件已经清零，可以销账"里，被**当成好消息报出来**。
    # 而真正的后果是：同一个文件改名之后，它的计数从 0 重新开始 —— 一次改名就买回
    # 一份免费额度，和棘轮想买的东西正好相反。
    # 现场：`agent/adminops.py` 拆成 8 个模块（20261018 批 3a），那一条就是这么被读成"清零"的。
    ghosts = sorted(k for k in base.get("by_file", {}) if not (ROOT / k).is_file())
    if ghosts:
        print(f"❌ 基线里有 {len(ghosts)} 个键在盘上找不到对应文件（改名/删除后的残留）："
              f"{'、'.join(ghosts)}\n"
              f"   它们不出现在 `grew` 里、也不影响 `total`，只会被当成「已清零」报出来 ——"
              f" 于是改名之后那个文件的计数从 0 重新开始。先 `--update` 重出账。")
        return False
    return True


def _running_mypy(prog: list[str]) -> str | None:
    """**真正在跑的那个 mypy 版本**（不是 `pyproject.toml` 里钉的那个）。

    这一段补的是「三个量」里的第一个：此前 `mypy_version` 只在**基线**与**钉住的版本**
    之间比，**从没读过跑起来的那个**。于是 `--mypy` 传一个别的版本这种"换了一个量"的
    调用会被读成**代码变差了**——实测它报的是 `agent/graph.py 81（基线 79）—— 这一处
    变多了` 加总数变大，退 1；而真相是换了个 mypy、错误集整个变了。同族的
    `lint_gate` / `except_gate` 都读运行版本，只有这里漏了，扫描行还把 `pin` 当版本印出来
    （日志上看不出区别）。**退 3 而不是 1**：这不是变差，是换了一个量。
    """
    r = subprocess.run(prog + ["--version"], cwd=ROOT, capture_output=True, text=True)
    m = _VER_RE.search((r.stdout or "") + (r.stderr or ""))
    return m.group("v") if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description="mypy 错误数棘轮（只许变小）")
    ap.add_argument("--mypy", default="", help='用哪条命令跑 mypy（默认 "<python> -m mypy"）')
    ap.add_argument("--update", action="store_true", help="重出 baseline（唯一放宽通道）")
    args = ap.parse_args()
    prog = shlex.split(args.mypy) if args.mypy else [sys.executable, "-m", "mypy"]
    # **恒带**：把"去哪找三方库"钉在启动本脚本的那个环境上。不给它，读数就跟着启动器走
    # ——同一条判据、同一个 mypy 版本能读出 212 也能读出 220（见模块 docstring 第三条）。
    prog += ["--python-executable", sys.executable]
    pin = _pinned_mypy()
    py_target = _pinned_python()

    # 先判三道量、再跑 mypy：`_gather()` 要一分钟，而这几条只读文本。
    if not _launcher_guard(py_target):
        return 3
    running = _running_mypy(prog)
    if running is None:
        print("❌ 跑不起来：" + " ".join(prog) + " —— 连 `--version` 都认不出。\n"
              f'  本机请用隔离环境的解释器启动：`…/bin/python eval/type_gate.py '
              f'--mypy "uvx mypy@{pin or "<版本>"}"`')
        return 3
    if pin and running != pin:
        print(f"❌ 正在跑的是 mypy {running}，而 `pyproject.toml` 的 dev 分组钉的是 {pin} —— "
              f"**换了一个量**（错误集整个变），不是代码变差。下面那些'变多了'会把你带到"
              f"错的结论上。要么跑同版本，要么明说要升版并 `--update` 重出账。")
        return 3
    if not args.update:
        if not BASELINE.is_file():
            print(f"❌ 没有基线文件 {BASELINE.relative_to(ROOT)}——先跑 `--update` 出一次。")
            return 3
        base = json.loads(BASELINE.read_text(encoding="utf-8"))
        if not _baseline_guards(base, pin, py_target):
            return 3

    try:
        by_file, total, checked = _gather(prog)
    except (RuntimeError, ValueError) as e:
        print(f"❌ {e}")
        return 3

    if args.update:
        _write_baseline(by_file, total, checked)
        print(f"✅ baseline 已重出：{total} 个错误 / {len(by_file)} 个文件"
              f"（查过 {checked} 个源文件）→ {BASELINE.relative_to(ROOT)}")
        return 0

    grew = {f: (n, base["by_file"].get(f, 0)) for f, n in by_file.items()
            if n > base["by_file"].get(f, 0)}
    print(f"[扫描] mypy {running} / py{py_target}：{total} 个错误 / {len(by_file)} 个文件"
          f"（基线 {base['total']} / {len(base['by_file'])} 个；查过 {checked} 个源文件）")
    for f, (now, was) in sorted(grew.items()):
        print(f"  ❌ {f}  {now}（基线 {was}）—— 这一处变多了")
    if total > base["total"]:
        print(f"  ❌ 总数 {total} > 基线 {base['total']}")
        return 1
    if grew:
        print("  ❌ 总数没涨，但上面这些文件涨了（互相抵消看不出来） ⇒ 一样红")
        return 1

    gone = sorted(set(base["by_file"]) - set(by_file))
    if gone:
        print(f"  💚 这些文件已经清零，可以销账：{'、'.join(gone)}（跑 --update）")
    if total < base["total"]:
        print(f"  💚 比基线少了 {base['total'] - total} 个 —— 记账可以收得更紧（跑 --update）")
    print("✅ 没有变差")
    return 0


if __name__ == "__main__":
    sys.exit(main())
