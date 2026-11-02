#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ruff error-count ratchet over repository Python sources.

New files must be clean; total and per-file counts may only decrease. Ruff
version, selected rules and line length must match pyproject.toml and the baseline.
Missing baseline files or invalid scanner output fail closed.

Run ``python eval/lint_gate.py`` in the locked development environment. Use
``--ruff COMMAND`` for an alternate launcher and ``--update`` only for a reviewed
baseline change. Exit codes: 0 pass, 1 regression, 3 invalid setup or baseline.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shlex
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.source_files import python_sources  # noqa: E402

BASELINE = ROOT / "eval" / "lint_baseline.json"

_ERR_RE = re.compile(r"^(?P<file>[^:\n]+):\d+:\d+: (?P<code>[A-Z]+\d+) ")
_SUM_RE = re.compile(r"^Found (?P<n>\d+) errors?", re.M)
_VER_RE = re.compile(r"ruff (?P<v>[0-9][^\s]*)")


def _ruff_cfg() -> tuple[str | None, int | None, list[str] | None]:
    """Read the pinned Ruff version and relevant configuration from pyproject.toml."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = data.get("dependency-groups", {}).get("dev", [])
    pin = next((re.search(r"^ruff==(.+)$", d).group(1)
                for d in dev if isinstance(d, str) and re.match(r"^ruff==", d)), None)
    ruff = data.get("tool", {}).get("ruff", {})
    return pin, ruff.get("line-length"), ruff.get("lint", {}).get("select")


def _counts(out: str) -> dict[str, int]:
    """Parse scanner findings and verify that detail counts match the reported total."""
    by_file: dict[str, int] = {}
    for line in out.splitlines():
        m = _ERR_RE.match(line)
        if m:
            f = m.group("file")
            by_file[f] = by_file.get(f, 0) + 1
    s = _SUM_RE.search(out)
    if not s:
        if not by_file:
            return {}
        raise ValueError("ruff 的输出里认不出汇总行（输出格式变了？）")
    total = int(s.group("n"))
    if total != sum(by_file.values()):
        raise ValueError(f"汇总行说 {total} 条，逐行只数出 {sum(by_file.values())} 条 "
                         f"—— 两个口径分岔了，先查 `--output-format` 是不是变了")
    return by_file


def judge(by_file: dict[str, int], base_files: dict[str, int]) -> list[str]:
    """Return regressions against the baseline; independent file counts cannot offset each other."""
    bad: list[str] = []
    for f in sorted(by_file):
        now, was = by_file[f], base_files.get(f, 0)
        if f not in base_files:
            bad.append(f"新增文件带 error  {f}  {now} 条（纪律 1：新文件必须零 error）")
        elif now > was:
            bad.append(f"变脏        {f}  {now} 条（基线 {was}，只许缩小）")
    return bad


def ghost_keys(base_files: dict) -> list[str]:
    """Return baseline files that no longer exist; stale keys make the baseline invalid."""
    return sorted(k for k in base_files if not (ROOT / k).is_file())


def _self_check() -> None:
    """Verify parsing and ratchet behavior with positive and negative controls."""
    out = (
        "tools/base.py:12:1: E501 Line too long (120 > 100)\n"
        "tools/base.py:30:5: F401 [*] `os` imported but unused\n"
        "agent/graph.py:9:1: I001 [*] Import block is un-sorted or un-formatted\n"
        "\nFound 3 errors.\n"
        "[*] 2 fixable with the `--fix` option.\n"
    )
    got = _counts(out)
    assert got == {"tools/base.py": 2, "agent/graph.py": 1}, f"逐文件计数错了：{got}"
    assert _counts("") == {}, "全清（空输出）应当是 0 条，不是异常"
    try:
        _counts("a.py:1:1: E501 x\n\nFound 9 errors.\n")
    except ValueError:
        pass
    else:
        raise AssertionError("汇总行与逐行对不上却没抛 —— 口径分岔会被静默吞掉")

    base = {"a.py": 5, "b.py": 2}
    assert not judge({"a.py": 5, "b.py": 2}, base), "没动却红了"
    assert not judge({"a.py": 4, "b.py": 2}, base), "只变小却红了（棘轮不该这样）"
    assert judge({"a.py": 6, "b.py": 2}, base), "存量文件变脏没红"
    assert judge({"a.py": 5, "b.py": 2, "c.py": 1}, base), "新文件带 error 没红"
    assert judge({"a.py": 6, "b.py": 1}, base), "互相抵消（+1/-1）没红"
    assert any("新增文件" in b for b in judge({"c.py": 1}, base)), \
        "新文件那条要与'变脏'分开报（两种事故）"

    assert ghost_keys({"eval/this_file_was_deleted.py": 3}) == ["eval/this_file_was_deleted.py"], \
        "盘上没有的键没被抓到 —— 幽灵键守卫是空的"
    assert ghost_keys({"eval/lint_gate.py": 1}) == [], \
        "盘上**有**的键被判成幽灵 —— 这是「见谁都红」"


def main() -> int:
    ap = argparse.ArgumentParser(description="ruff error 数棘轮（只许变小）")
    ap.add_argument("--ruff", default="", help='用哪条命令跑 ruff（默认 "<python> -m ruff"）')
    ap.add_argument("--update", action="store_true", help="重出基线（唯一放宽通道）")
    args = ap.parse_args()
    prog = shlex.split(args.ruff) if args.ruff else [sys.executable, "-m", "ruff"]
    pin, line_length, select = _ruff_cfg()

    try:
        _self_check()
    except AssertionError as e:
        print(f"❌ 自检没过——先修计数器/判据，再看扫描结果（否则读数不可信）：{e}")
        return 3

    v = subprocess.run(prog + ["--version"], cwd=ROOT, capture_output=True, text=True)
    vm = _VER_RE.search((v.stdout or "") + (v.stderr or ""))
    if v.returncode != 0 or not vm:
        print("❌ 跑不起来：" + " ".join(prog) + "\n"
              "  本机（产线 venv、不许 uv sync）请用隔离环境跑同版本：\n"
              f'    python eval/lint_gate.py --ruff "uvx ruff@{pin or "<版本>"}"')
        return 3
    running = vm.group("v")

    if not args.update:
        if not BASELINE.is_file():
            print(f"❌ 没有基线文件 {BASELINE.relative_to(ROOT)}——先跑 `--update` 出一次。")
            return 3
        base = json.loads(BASELINE.read_text(encoding="utf-8"))
        for key, now_v in (("ruff_version", running), ("line_length", line_length),
                           ("select", select)):
            if base.get(key) != now_v:
                print(f"❌ 基线里的 `{key}` 是 {base.get(key)!r}，现在是 {now_v!r} —— "
                      f"换它等于换一套条目集，先 `--update` 重出账。")
                return 3
        ghosts = ghost_keys(base.get("by_file") or {})
        if ghosts:
            print(f"❌ 基线里有 {len(ghosts)} 个键在盘上找不到对应文件（删除后的残留）："
                  f"{'、'.join(ghosts)}\n"
                  f"   它们不进 `judge()` 的任何一条判据、也不影响 `total`，只会让账变脏"
                  f"（下面那句「已经清零，可以销账」还会把它们报成好消息）。先 `--update`。")
            return 3

    sources = [path.relative_to(ROOT).as_posix() for path in python_sources(ROOT)]
    r = subprocess.run(prog + ["check", "--no-cache", "--output-format=concise", *sources],
                       cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    try:
        by_file = _counts(out)
    except ValueError as e:
        print(f"❌ {e}")
        return 3
    total = sum(by_file.values())

    if args.update:
        BASELINE.write_text(json.dumps({
            "_note": "ruff error 数的棘轮基线。只许变小；--update 是唯一放宽通道。"
                     "三个量（ruff 版本 / line-length / select）都必须与 pyproject.toml "
                     "同源——三者都会改变条目集。判据见 eval/lint_gate.py 头注；"
                     "纪律怎么理解见 docs/lint-baseline.md。",
            "ruff_version": running,
            "line_length": line_length,
            "select": select,
            "total": total,
            "by_file": dict(sorted(by_file.items())),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"✅ 基线已重出：{total} 条 / {len(by_file)} 个文件 → {BASELINE.relative_to(ROOT)}")
        return 0

    bad = judge(by_file, base["by_file"])
    print(f"[扫描] ruff {running} · select={select} · line-length={line_length}："
          f"{total} 条 / {len(by_file)} 个文件"
          f"（基线 {base['total']} / {len(base['by_file'])} 个）")
    for b in bad:
        print(f"  ❌ {b}")
    if total > base["total"]:
        print(f"  ❌ 总数 {total} > 基线 {base['total']}")
    if bad or total > base["total"]:
        print("\n失败：新代码按 `docs/lint-baseline.md` 的纪律 1 必须零 error；"
              "存量文件按纪律 2 不许把计数改大。真要放宽就 `--update` 并说清为什么。")
        return 1

    gone = sorted(set(base["by_file"]) - set(by_file))
    if gone:
        print(f"  💚 这些文件已经清零，可以销账：{'、'.join(gone)}（跑 --update）")
    if total < base["total"]:
        print(f"  💚 比基线少了 {base['total'] - total} 条 —— 记账可以收得更紧（跑 --update）")
    print("✅ 没有变差")
    return 0


if __name__ == "__main__":
    sys.exit(main())
