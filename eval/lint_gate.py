#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态检查棘轮：ruff 的 error 数**只许变小**（20261018，审计 A3）。

**为什么是棘轮而不是门禁**：`pyproject.toml` 从 20260927 起就写着
`[tool.ruff.lint] select = ["E", "F", "I", "W"]`，而 CI 至今只跑 `--select F821`
一条（未定义名，会炸的那一类）。剩下那一大片（E/F/I/W，**会烂的那一类**）的唯一约束
是 `docs/lint-baseline.md` 里那四条**散文纪律**——没有判据看着，靠自觉。
20261018 实测全规则读数：**882 errors / 221 个文件**（其中 534 条是 E501 行长）。
一上来当门禁 ⇒ CI 恒红 ⇒ 人人学会无视它。所以照同族三道（复杂度 / 类型 / 模块体量）
同款：**记下当前读数，只许它变小**。

**判据的形状**（三条，任一成立即红）：
  ① 总数变大；
  ② **某个文件**变大——防"这边修了三个、那边坏了两个"互相抵消（总数看不出来）；
  ③ 基线里**没有**的文件带着 error 出现（新文件按纪律 1 必须零 error；它的"基线"是 0，
     所以这一条其实是 ② 的特例，但**单独报**——新文件与"存量又脏了一点"是两种事故，
     混在一句里读者会以为是自己动过的那个老文件）。
变好**不**报红（标准的棘轮行为：绿着，并提示你可以 `--update` 把账收得更紧）。

**三个量必须同源，否则量的是另一件事**（同 `eval/type_gate.py` 的教训）：
  · **ruff 版本**——升版就换了一套规则实现与默认行为，基线里记着出账时的版本；
  · **`line-length`**——E501 一条就占掉全仓 60% 的条目，改它等于换一套账；
  · **`select`**——多加一个规则族，全仓当场多出一片条目。
三者任一与 `pyproject.toml` 对不上 ⇒ **退 3**（不是 1）：这不是"变差了"，是**换了一个量**。
它们的真值只有一处：`pyproject.toml`（`tests/test_packaging.py` ⑤ 盯着接线）。

**与 `docs/lint-baseline.md` 的分工**：那份文档是**参照点**（纪律怎么理解、存量怎么分批清），
本脚本是**闸**（那些纪律的机器化）。文档里"机械闸（待建）"那一条到本文件为止。

用法：
    python eval/lint_gate.py                    # CI：dev 组已装好 ruff，走 sys.executable -m ruff
    python eval/lint_gate.py --ruff "uvx ruff@0.14.4"   # 本机（.venv 是产线 venv，不许 uv sync）
    python eval/lint_gate.py --update           # 重出基线（唯一放宽通道）

退出码：0 通过 / 1 变差 / 3 跑不起来（缺 ruff、输出格式变了、版本或两个配置量对不上）。
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
BASELINE = ROOT / "eval" / "lint_baseline.json"

# `--output-format=concise` 的一行：`path:line:col: CODE message`。
# 刻意**只认**这个形状：认不出就抛（见 `_counts`），不许退化成"按行数估"。
_ERR_RE = re.compile(r"^(?P<file>[^:\n]+):\d+:\d+: (?P<code>[A-Z]+\d+) ")
_SUM_RE = re.compile(r"^Found (?P<n>\d+) errors?", re.M)
# `ruff --version` → `ruff 0.14.4`
_VER_RE = re.compile(r"ruff (?P<v>[0-9][^\s]*)")


def _ruff_cfg() -> tuple[str | None, int | None, list[str] | None]:
    """从 `pyproject.toml` 取（钉住的 ruff 版本, line-length, select）。

    用 `tomllib` 而不是正则抠文本：`line-length` / `select` 这两个名字在本文件里
    也作为**注释里的文字**出现过（`docs/lint-baseline.md` 与 `pyproject.toml` 都在讲它们），
    整篇搜会把注释当取值读——本仓踩过同形状的坑（见 `type_gate._pinned_python` 的注）。
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = data.get("dependency-groups", {}).get("dev", [])
    pin = next((re.search(r"^ruff==(.+)$", d).group(1)
                for d in dev if isinstance(d, str) and re.match(r"^ruff==", d)), None)
    ruff = data.get("tool", {}).get("ruff", {})
    return pin, ruff.get("line-length"), ruff.get("lint", {}).get("select")


def _counts(out: str) -> dict[str, int]:
    """ruff 的 concise 输出 ⇒ 逐文件计数。

    **认不出就抛**：`_SUM_RE` 拿不到总数时不许拿逐行加出来的数顶上去——两个口径
    一旦分岔，棘轮就在悄悄量另一个东西（`type_gate._counts` 里那条教训的同形）。
    """
    by_file: dict[str, int] = {}
    for line in out.splitlines():
        m = _ERR_RE.match(line)
        if m:
            f = m.group("file")
            by_file[f] = by_file.get(f, 0) + 1
    s = _SUM_RE.search(out)
    if not s:
        if not by_file:
            return {}                       # 全清：ruff 不打印汇总行，那是胜利不是故障
        raise ValueError("ruff 的输出里认不出汇总行（输出格式变了？）")
    total = int(s.group("n"))
    if total != sum(by_file.values()):
        raise ValueError(f"汇总行说 {total} 条，逐行只数出 {sum(by_file.values())} 条 "
                         f"—— 两个口径分岔了，先查 `--output-format` 是不是变了")
    return by_file


def judge(by_file: dict[str, int], base_files: dict[str, int]) -> list[str]:
    """三条判据（空 = 没变差）。纯函数：自带正反控，见 `_self_check()`。"""
    bad: list[str] = []
    for f in sorted(by_file):
        now, was = by_file[f], base_files.get(f, 0)
        if f not in base_files:
            bad.append(f"新增文件带 error  {f}  {now} 条（纪律 1：新文件必须零 error）")
        elif now > was:
            bad.append(f"变脏        {f}  {now} 条（基线 {was}，只许缩小）")
    return bad


def _self_check() -> None:
    """正控 + 反控各来一遍：**没有这一段，「闸其实是空的」没人看着**。

    同 `tests/test_module_budget.py::_self_check()` 的理由——本仓最贵的失效模式是
    "看着在跑、其实没判"。这里判的是：计数解析得对、三条判据该红的红、没动的绿。
    """
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
    # 汇总行与逐行对不上 ⇒ 必须抛（不许静默换口径）
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

    # 跑一次版本探测：它同时证明"ruff 起得来"，代价可忽略。
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
        # 三个量任一换掉 ⇒ 退 3 而不是 1：这不是"变差了"，是**换了一个量**。
        for key, now_v in (("ruff_version", running), ("line_length", line_length),
                           ("select", select)):
            if base.get(key) != now_v:
                print(f"❌ 基线里的 `{key}` 是 {base.get(key)!r}，现在是 {now_v!r} —— "
                      f"换它等于换一套条目集，先 `--update` 重出账。")
                return 3

    r = subprocess.run(prog + ["check", "--no-cache", "--output-format=concise", "."],
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
