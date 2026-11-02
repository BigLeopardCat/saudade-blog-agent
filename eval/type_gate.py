# -*- coding: utf-8 -*-
"""Mypy error-count ratchet over production Python source packages.

Total and per-file error counts may only decrease. The pinned Mypy version,
Python target and launcher interpreter must match the baseline. Mypy always uses
this process's interpreter to resolve installed dependency types. Missing source
files in the baseline and unrecognized scanner output fail closed.

Run ``python eval/type_gate.py`` in the locked development environment.
``--mypy COMMAND`` changes the launcher; ``--update`` writes a reviewed baseline.
Exit codes: 0 pass, 1 regression, 3 invalid setup or baseline.
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
sys.path.insert(0, str(ROOT))

from eval.source_files import python_sources  # noqa: E402

BASELINE = ROOT / "eval" / "mypy_baseline.json"

# Type checking covers production packages; evaluation and developer tools use other gates.
TARGETS = ["agent", "config", "models", "rag", "tools", "utils", "server.py"]

_ERR_RE = re.compile(r"^(?P<file>[^:\n]+):\d+(?::\d+)?: error: ")
_SUM_RE = re.compile(r"^Found (?P<n>\d+) errors? in (?P<files>\d+) files? "
                     r"\(checked (?P<checked>\d+) ", re.M)
_OK_RE = re.compile(r"^Success: no issues found in (?P<checked>\d+) ", re.M)
_VER_RE = re.compile(r"^mypy (?P<v>[0-9][^\s]*)", re.M)


def _pinned_mypy() -> str | None:
    """Read the pinned Mypy version from the development dependency group."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'"mypy==([0-9][^"]*)"', text)
    return m.group(1) if m else None


def _pinned_python() -> str | None:
    """Read the Python target from the Mypy configuration section, excluding comments."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"^\[tool\.mypy\](?P<body>.*?)(?=^\[|\Z)", text, re.M | re.S)
    if not m:
        return None
    body = "\n".join(ln.split("#", 1)[0] for ln in m.group("body").splitlines())
    v = re.search(r'^python_version\s*=\s*"([^"]+)"', body, re.M)
    return v.group(1) if v else None


def _counts(out: str) -> tuple[dict[str, int], int, int]:
    """Parse scanner findings and verify that detail counts match the reported total."""
    by_file: dict[str, int] = {}
    for line in out.splitlines():
        m = _ERR_RE.match(line)
        if m:
            by_file[m.group("file")] = by_file.get(m.group("file"), 0) + 1
    s = _SUM_RE.search(out)
    if s:
        return by_file, int(s.group("n")), int(s.group("checked"))
    ok = _OK_RE.search(out)
    if ok and not by_file:
        return {}, 0, int(ok.group("checked"))
    raise ValueError("mypy 的输出里认不出汇总行（格式变了？）")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    sources = [path.relative_to(ROOT).as_posix() for path in python_sources(ROOT, TARGETS)]
    return subprocess.run(cmd + sources, cwd=ROOT, capture_output=True, text=True)


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
    """Require the launcher interpreter to match the Python target, including baseline updates."""
    launcher = f"{sys.version_info[0]}.{sys.version_info[1]}"
    if py_target and launcher != py_target:
        print(f"❌ 本脚本跑在 Python {launcher} 上，而 `[tool.mypy].python_version` 是 "
              f"{py_target} —— mypy 会**照这个解释器**去找三方库，于是正在量的是"
              f"「{launcher} 的库 + {py_target} 的语法」。用 {py_target} 的解释器重新启动"
              f"（本机：在隔离环境里跑，见模块 docstring）。")
        return False
    return True


def _baseline_guards(base: dict, pin: str | None, py_target: str | None) -> bool:
    """Validate version, Python target and baseline source paths before invoking Mypy."""
    if base.get("mypy_version") != pin:
        print(f"❌ 基线是用 mypy {base.get('mypy_version')} 出的，"
              f"而 dev 组钉的是 {pin} —— 换版本就换了一套错误集，先 `--update` 重出。")
        return False

    if base.get("python_target") != py_target:
        print(f"❌ 基线是用 Python {base.get('python_target')} 的目标出的，"
              f"而 `[tool.mypy].python_version` 现在是 {py_target} —— 换解释器目标同样换"
              f"了一套错误集，先 `--update` 重出。")
        return False

    ghosts = sorted(k for k in base.get("by_file", {}) if not (ROOT / k).is_file())
    if ghosts:
        print(f"❌ 基线里有 {len(ghosts)} 个键在盘上找不到对应文件（改名/删除后的残留）："
              f"{'、'.join(ghosts)}\n"
              f"   它们不出现在 `grew` 里、也不影响 `total`，只会被当成「已清零」报出来 ——"
              f" 于是改名之后那个文件的计数从 0 重新开始。先 `--update` 重出账。")
        return False
    return True


def _running_mypy(prog: list[str]) -> str | None:
    """Read the actual scanner version rather than assuming the pinned launcher was used."""
    r = subprocess.run(prog + ["--version"], cwd=ROOT, capture_output=True, text=True)
    m = _VER_RE.search((r.stdout or "") + (r.stderr or ""))
    return m.group("v") if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description="mypy 错误数棘轮（只许变小）")
    ap.add_argument("--mypy", default="", help='用哪条命令跑 mypy（默认 "<python> -m mypy"）')
    ap.add_argument("--update", action="store_true", help="重出 baseline（唯一放宽通道）")
    args = ap.parse_args()
    prog = shlex.split(args.mypy) if args.mypy else [sys.executable, "-m", "mypy"]
    prog += ["--python-executable", sys.executable]
    pin = _pinned_mypy()
    py_target = _pinned_python()

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
