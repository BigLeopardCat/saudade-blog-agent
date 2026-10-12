# -*- coding: utf-8 -*-
"""类型检查棘轮：mypy 的错误数**只许变小**（20261013，行业规范收口批 2b）。

**为什么是棘轮而不是门禁**：`[tool.mypy]` 那段配置在此之前**从不执行**——不在依赖、
不在 `uv.lock`、CI 没有这一步（CI 只有 `ruff --select F821` 一条）。配上以来第一次真跑
（20261013）读到 `212 errors in 21 files (checked 46)`：数量不大，但一次清零要动
21 个文件、其中 `agent/graph.py` 一个就 98 个，且大半是 `arg-type` / `attr-defined`
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

**解释器目标同理，而且它是后补的洞（20261013 升 3.12 时发现）**：本脚本调 mypy 时
**不传 `--python-version`** ⇒ 目标只来自 `pyproject.toml` 的 `[tool.mypy].python_version`。
而那一项**会改变报出来的错误集**（3.11+ 的 `Self`/`ExceptionGroup`、3.12 的泛型语法…）。
基线当时只记 `mypy_version` ⇒「把 3.10 改成 3.12」这种**换了一个量**的改动，在棘轮眼里
和「什么都没动」长得一模一样（错误数没涨就不报）。现在基线里也记 `python_target`，
对不上同样退 3。

用法（⚠️ 本机 `.venv` 是**产线 venv**、不许 `uv sync` ⇒ 本机用隔离的 uvx 跑同一个版本）：
    uvx mypy@2.4.0 …            # 本机：--mypy "uvx mypy@2.4.0"
    python eval/type_gate.py --mypy "uvx mypy@2.4.0"
    python eval/type_gate.py              # CI：dev 组已装好，走 sys.executable -m mypy
    python eval/type_gate.py --update     # 重出 baseline

退出码：0 通过 / 1 变差 / 3 跑不起来（缺 mypy、汇总行认不出、版本或解释器目标对不上）。
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
            "  本机（产线 venv、不许 uv sync）请用："
            '  python eval/type_gate.py --mypy "uvx mypy@'
            + str(_pinned_mypy()) + '"')
    return _counts(out)


def _write_baseline(by_file: dict[str, int], total: int, checked: int) -> None:
    BASELINE.write_text(json.dumps({
        "_note": "mypy 错误数的棘轮基线。只许变小；--update 是唯一放宽通道。"
                 "版本与解释器目标都必须与 pyproject.toml 同源——两者都会改变错误集。",
        "mypy_version": _pinned_mypy(),
        "python_target": _pinned_python(),
        "targets": TARGETS,
        "total": total,
        "files_checked": checked,
        "by_file": dict(sorted(by_file.items())),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="mypy 错误数棘轮（只许变小）")
    ap.add_argument("--mypy", default="", help='用哪条命令跑 mypy（默认 "<python> -m mypy"）')
    ap.add_argument("--update", action="store_true", help="重出 baseline（唯一放宽通道）")
    args = ap.parse_args()
    prog = shlex.split(args.mypy) if args.mypy else [sys.executable, "-m", "mypy"]

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

    if not BASELINE.is_file():
        print(f"❌ 没有基线文件 {BASELINE.relative_to(ROOT)}——先跑 `--update` 出一次。")
        return 3
    base = json.loads(BASELINE.read_text(encoding="utf-8"))

    # 版本对不上 ⇒ 退 3 而不是 1：这不是"变差了"，是**换了一个量**。
    pin = _pinned_mypy()
    if base.get("mypy_version") != pin:
        print(f"❌ 基线是用 mypy {base.get('mypy_version')} 出的，"
              f"而 dev 组钉的是 {pin} —— 换版本就换了一套错误集，先 `--update` 重出。")
        return 3

    # 同款、同因：解释器目标也换错误集。**多判这一条是本脚本 20261013 才补上的**——
    # 在此之前「3.10 改 3.12」在棘轮眼里是隐形改动（旧基线里根本没有这个键 ⇒ 这里会
    # 因为 `None != "3.12"` 而退 3，正是要的效果：逼你先 `--update` 换一套账）。
    py_target = _pinned_python()
    if base.get("python_target") != py_target:
        print(f"❌ 基线是用 Python {base.get('python_target')} 的目标出的，"
              f"而 `[tool.mypy].python_version` 现在是 {py_target} —— 换解释器目标同样换"
              f"了一套错误集，先 `--update` 重出。")
        return 3

    grew = {f: (n, base["by_file"].get(f, 0)) for f, n in by_file.items()
            if n > base["by_file"].get(f, 0)}
    print(f"[扫描] mypy {pin} / py{py_target}：{total} 个错误 / {len(by_file)} 个文件"
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
