#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盲捕获棘轮：`except Exception` 的条数**只许变小**（20261020，审计意见 §异常精细化）。

**为什么是棘轮，不是"开个规则"**：审计文档要的是"`BLE001` 开启且通过"。直接把它加进
`pyproject.toml` 的 `[tool.ruff.lint].select` 是**错的那条路**，两个理由：
  · `eval/lint_gate.py` 把 `select` 当"三个量"之一（换了它等于换一套条目集）⇒ 加一个
    规则族，那道闸当场退 3，而重出的账里 622 条会变成 700 多条、**两件事混进一个数**；
  · 更要紧的是它把 117 条存量**一次性**变成"红" ⇒ CI 恒红 ⇒ 人人学会无视它
    （`docs/lint-baseline.md` 开头那条"一上来当门禁就等于没有门禁"的同一条教训）。
所以照同族四道（lint / 类型 / 复杂度 / 模块体量）同款：**记下当前读数，只许它变小**。

**只记账、不批量改**（这是本闸最重要的一条纪律）：这些盲捕获**大多是对的**。
工具层与 RAG 层的宽 `except` 是**故意的**——一个工具读不到数据时要返回
`unavailable` 让模型换条路，而不是把整轮打成 500；`agent/graph.py` 那几处是"这一帧
取不到就少一帧"的降级。机械地按行号把 `except Exception` 换成 `except (KeyError, …)`
会把这些**降级路径**改成崩溃路径，而且改的时候没人看得出区别（判据全绿）。
真要收窄，得一处一处看"这里到底该不该吞、吞了要不要留痕"——那是人工活，
本闸只负责**让这件事的进度可见、且不许倒退**。

**判据的形状**（同 `eval/lint_gate.py`，四条）：
  ① 总数变大；② 某个文件变大（防"这边修三个、那边坏两个"互相抵消）；
  ③ 基线里**没有**的文件带着盲捕获出现（新文件按纪律 1 必须零条）；
  ④ 账里有、盘上却没有的键 ⇒ 退 3（账不可用，先 `--update`）。
变好**不**报红（棘轮行为：绿着并提示可以收紧）。

**三个量必须同源**（任一换掉 ⇒ **退 3**，不是 1 —— 那是"换了一个量"）：
  · **ruff 版本**——升版换一套规则实现；
  · **`select`**——本闸自己传的那份（`["BLE001"]`）。它**刻意不写进 `pyproject.toml`**：
    那条路会让 `lint_gate` 的账换量（见上面第一段），所以这份 select 的落点就是本文件；
  · **`[tool.ruff.lint].ignore`**——⚠️ 这一条是**真的洞**：`--select BLE001` 仍然服从
    pyproject 的 `ignore` 表，谁往里加一句 `"BLE001"`，本闸会静默读到 **0 条**、
    然后报"✅ 没有变差"。所以基线里记着出账时那份 ignore，对不上就退 3。
    （同族教训：`eval/type_gate.py` 的 `python_target` 就是为"换了个量却看不出来"补的。）
`line-length` **不记**：BLE001 与行长无关（`lint_gate` 记它是因为 E501 占了那边 60% 的账）。

用法：
    python eval/except_gate.py                  # CI：dev 组已装好 ruff，走 sys.executable -m ruff
    python eval/except_gate.py --ruff "uvx ruff@0.14.4"   # 本机（.venv 是产线 venv）
    python eval/except_gate.py --update         # 重出基线（唯一放宽通道）

退出码：0 通过 / 1 变差 / 3 跑不起来或**账不可用**（缺 ruff、输出格式变了、三个量对不上、
        账里有盘上不存在的键）。
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
BASELINE = ROOT / "eval" / "except_baseline.json"

# 本闸扫的规则族。**只有一条**：盲捕获。加第二条要先想清楚它是不是该进 `lint_gate` 的
# select（那边是"会烂的那一类"的总账），别把两件事混成一个数。
SELECT = ["BLE001"]

# `--output-format=concise` 的一行：`path:line:col: CODE message`。认不出就抛（见 `_counts`）。
_ERR_RE = re.compile(r"^(?P<file>[^:\n]+):\d+:\d+: (?P<code>[A-Z]+\d+) ")
_SUM_RE = re.compile(r"^Found (?P<n>\d+) errors?", re.M)
_VER_RE = re.compile(r"ruff (?P<v>[0-9][^\s]*)")


def _ruff_cfg() -> tuple[str | None, list[str] | None]:
    """从 `pyproject.toml` 取（钉住的 ruff 版本, 项目自己的 lint `ignore` 表）。

    用 `tomllib` 而不是正则抠文本：`ignore` 这个名字在本文件里也作为**注释里的文字**
    出现过，整篇搜会把注释当取值读（同 `lint_gate._ruff_cfg` 那条注）。
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = data.get("dependency-groups", {}).get("dev", [])
    pin = next((re.search(r"^ruff==(.+)$", d).group(1)
                for d in dev if isinstance(d, str) and re.match(r"^ruff==", d)), None)
    return pin, data.get("tool", {}).get("ruff", {}).get("lint", {}).get("ignore")


def _counts(out: str) -> dict[str, int]:
    """ruff 的 concise 输出 ⇒ 逐文件计数。**认不出就抛**（同 `lint_gate._counts`：
    两个口径一旦分岔，棘轮就在悄悄量另一个东西）。"""
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
            bad.append(f"新增文件带盲捕获  {f}  {now} 条（纪律 1：新文件必须零条）")
        elif now > was:
            bad.append(f"变多        {f}  {now} 条（基线 {was}，只许缩小）")
    return bad


def ghost_keys(base_files: dict) -> list[str]:
    """账里有、盘上却没有的键（删除后的残留）⇒ 账不可用，退 3。

    键只被用来做 `base_files.get(f, 0)` ⇒ 删掉一个文件之后旧键永远留在账上：不进
    `judge()` 的任何一条判据、也不影响 `total`，只是让账慢慢变脏，还会被"已经清零，
    可以销账"那句**报成好消息**（同 `eval/lint_gate.py` 第四类）。
    """
    return sorted(k for k in base_files if not (ROOT / k).is_file())


def _ignored_ble(ignore: list[str] | None) -> bool:
    """项目自己的 `ignore` 表里有没有把 `BLE001`（或整个 `BLE` 族）关掉。

    这一条是**真的洞**：`ruff check --select BLE001` 仍然服从 pyproject 的 `ignore`，
    谁加一句 `ignore = ["BLE001"]`，本闸静默读到 0 条并报"✅ 没有变差"。
    """
    for x in ignore or []:
        if x in ("BLE001", "BLE"):
            return True
    return False


def _self_check() -> None:
    """正控 + 反控各来一遍：**没有这一段，「闸其实是空的」没人看着**。"""
    out = (
        "tools/notes.py:12:9: BLE001 Do not catch blind exception: `Exception`\n"
        "tools/notes.py:30:9: BLE001 Do not catch blind exception: `Exception`\n"
        "agent/graph.py:9:9: BLE001 Do not catch blind exception: `Exception`\n"
        "\nFound 3 errors.\n"
    )
    got = _counts(out)
    assert got == {"tools/notes.py": 2, "agent/graph.py": 1}, f"逐文件计数错了：{got}"
    assert _counts("") == {}, "全清（空输出）应当是 0 条，不是异常"
    try:
        _counts("a.py:1:1: BLE001 x\n\nFound 9 errors.\n")
    except ValueError:
        pass
    else:
        raise AssertionError("汇总行与逐行对不上却没抛 —— 口径分岔会被静默吞掉")

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

    # 洞的反控：`ignore` 表里关掉 BLE001 ⇒ 必须认出来（否则本闸会静默读到 0 条还报绿）
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

    # 洞先堵：`ignore` 表关掉了 BLE001 ⇒ 下面那次扫描会读到 0 条、然后报"没有变差"。
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
        # 三个量任一换掉 ⇒ 退 3 而不是 1：这不是"变差了"，是**换了一个量**。
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

    r = subprocess.run(prog + ["check", "--no-cache", "--output-format=concise",
                               "--select", ",".join(SELECT), "."],
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
