#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ruff BLE001 ratchet for broad exception handlers.

Total and per-file counts may only decrease; new files must have no findings.
The pinned Ruff version, selected rule and project ignore configuration must
match the baseline. Ignoring BLE001 is rejected. Narrow exception handlers only
when their recovery behavior is understood.

Run ``python eval/except_gate.py`` in the locked development environment.
``--ruff COMMAND`` changes the launcher; ``--update`` writes a reviewed baseline.
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
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.ruff_scan import counts, scan  # noqa: E402
from eval.source_files import python_sources  # noqa: E402

BASELINE = ROOT / "eval" / "except_baseline.json"

# Track BLE001 separately from the general lint rule set.
SELECT = ["BLE001"]

_VER_RE = re.compile(r"ruff (?P<v>[0-9][^\s]*)")


def _ruff_cfg() -> tuple[str | None, list[str] | None]:
    """Read the pinned Ruff version and relevant configuration from pyproject.toml."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = data.get("dependency-groups", {}).get("dev", [])
    pin = next((re.search(r"^ruff==(.+)$", d).group(1)
                for d in dev if isinstance(d, str) and re.match(r"^ruff==", d)), None)
    return pin, data.get("tool", {}).get("ruff", {}).get("lint", {}).get("ignore")


def _counts(out: str) -> dict[str, int]:
    return counts(out, ROOT)


def judge(by_file: dict[str, int], base_files: dict[str, int]) -> list[str]:
    """Return regressions against the baseline; independent file counts cannot offset each other."""
    bad: list[str] = []
    for f in sorted(by_file):
        now, was = by_file[f], base_files.get(f, 0)
        if f not in base_files:
            bad.append(f"新增文件带盲捕获  {f}  {now} 条（纪律 1：新文件必须零条）")
        elif now > was:
            bad.append(f"变多        {f}  {now} 条（基线 {was}，只许缩小）")
    return bad


def ghost_keys(base_files: dict) -> list[str]:
    """Return baseline files that no longer exist; stale keys make the baseline invalid."""
    return sorted(k for k in base_files if not (ROOT / k).is_file())


def _ignored_ble(ignore: list[str] | None) -> bool:
    """Check whether project ignore rules disable BLE001 or its entire rule family."""
    for x in ignore or []:
        if x in ("BLE001", "BLE"):
            return True
    return False


def _self_check() -> None:
    """Verify parsing and ratchet behavior with positive and negative controls."""
    out = json.dumps([
        {"filename": "tools/base.py", "code": "BLE001", "message": "finding",
         "location": {"row": row, "column": 1}} for row in (12, 30)])
    assert _counts(out) == {"tools/base.py": 2}
    assert _counts("[]") == {}
    for invalid in ("", "not JSON", '{"error": "configuration"}'):
        try:
            _counts(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid scanner output was accepted")

    base = {"a.py": 5, "b.py": 2}
    assert not judge({"a.py": 5, "b.py": 2}, base), "没动却红了"
    assert not judge({"a.py": 4, "b.py": 2}, base), "只变小却红了（棘轮不该这样）"
    assert judge({"a.py": 6, "b.py": 2}, base), "存量文件变多没红"
    assert judge({"a.py": 5, "b.py": 2, "c.py": 1}, base), "新文件带盲捕获没红"
    assert judge({"a.py": 6, "b.py": 1}, base), "互相抵消（+1/-1）没红"
    assert any("新增文件" in b for b in judge({"c.py": 1}, base)), \
        "新文件那条要与'变多'分开报（两种事故）"

    assert ghost_keys({"eval/this_file_was_deleted.py": 3}) == ["eval/this_file_was_deleted.py"], \
        "盘上没有的键没被抓到 —— 幽灵键守卫是空的"
    assert ghost_keys({"eval/except_gate.py": 1}) == [], \
        "盘上**有**的键被判成幽灵 —— 这是「见谁都红」"

    assert _ignored_ble(["BLE001"]), "ignore 里有 BLE001 却没认出来 —— 这条守卫是空的"
    assert _ignored_ble(["BLE"]), "ignore 里有整个 BLE 族却没认出来"
    assert not _ignored_ble(["E501"]), "无关的 ignore 被误判成关掉了 BLE001（见谁都红）"
    assert not _ignored_ble(None), "没有 ignore 表（None）被误判成关掉了"


def main() -> int:
    ap = argparse.ArgumentParser(description="盲捕获（BLE001）条数棘轮（只许变小）")
    ap.add_argument("--ruff", default="", help='用哪条命令跑 ruff（默认 "<python> -m ruff"）')
    ap.add_argument("--update", action="store_true", help="重出基线（唯一放宽通道）")
    args = ap.parse_args()
    prog = shlex.split(args.ruff) if args.ruff else [sys.executable, "-m", "ruff"]
    pin, ignore = _ruff_cfg()

    try:
        _self_check()
    except AssertionError as e:
        print(f"❌ 自检没过——先修计数器/判据，再看扫描结果（否则读数不可信）：{e}")
        return 3

    if _ignored_ble(ignore):
        print(f"❌ `pyproject.toml` 的 `[tool.ruff.lint].ignore` 里关掉了 BLE001：{ignore}\n"
              f"   本闸的扫描面就没了（会读到 0 条然后报绿）。要停用这个闸就明说，"
              f"别用 ignore 把它变成一条没有内容的判据。")
        return 3

    v = subprocess.run(prog + ["--version"], cwd=ROOT, capture_output=True, text=True)
    vm = _VER_RE.search((v.stdout or "") + (v.stderr or ""))
    if v.returncode != 0 or not vm:
        print("❌ 跑不起来：" + " ".join(prog) + "\n"
              "  本机（产线 venv、不许 uv sync）请用隔离环境跑同版本：\n"
              f'    python eval/except_gate.py --ruff "uvx ruff@{pin or "<版本>"}"')
        return 3
    running = vm.group("v")

    if not args.update:
        if not BASELINE.is_file():
            print(f"❌ 没有基线文件 {BASELINE.relative_to(ROOT)}——先跑 `--update` 出一次。")
            return 3
        base = json.loads(BASELINE.read_text(encoding="utf-8"))
        for key, now_v in (("ruff_version", running), ("select", SELECT),
                           ("project_lint_ignore", ignore)):
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
    try:
        by_file = scan(prog, ROOT, sources, select=SELECT)
    except ValueError as e:
        print(f"❌ {e}")
        return 3
    total = sum(by_file.values())

    if args.update:
        BASELINE.write_text(json.dumps({
            "_note": "盲捕获（BLE001）条数的棘轮基线。只许变小；--update 是唯一放宽通道。"
                     "三个量（ruff 版本 / select / pyproject 的 lint ignore）都必须与出账时"
                     "同源——任一都会改变条目集。**只记账、不批量改**：这些宽 except 大多是"
                     "故意的降级路径（工具层返回 unavailable、图里少一帧），"
                     "机械收窄会把降级改成崩溃。判据见 eval/except_gate.py 头注。",
            "ruff_version": running,
            "select": SELECT,
            "project_lint_ignore": ignore,
            "total": total,
            "by_file": dict(sorted(by_file.items())),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"✅ 基线已重出：{total} 条 / {len(by_file)} 个文件 → {BASELINE.relative_to(ROOT)}")
        return 0

    bad = judge(by_file, base["by_file"])
    print(f"[扫描] ruff {running} · select={SELECT}：{total} 条 / {len(by_file)} 个文件"
          f"（基线 {base['total']} / {len(base['by_file'])} 个）")
    for b in bad:
        print(f"  ❌ {b}")
    if total > base["total"]:
        print(f"  ❌ 总数 {total} > 基线 {base['total']}")
    if bad or total > base["total"]:
        print("\n失败：新代码必须零条盲捕获；存量文件不许把计数改大。"
              "**要收窄请一处一处看**（这里该不该吞、吞了要不要留痕），"
              "真要放宽就 `--update` 并说清为什么。")
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
