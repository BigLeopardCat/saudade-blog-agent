#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 `changelog.d/` 里的碎片汇总进 `CHANGELOG.md`（20261019 换的机制）。

## 为什么要换成碎片

`CHANGELOG.md` 只有**一个**插入点——当日那一节的组首。于是**任何两个同时开着的 PR
都必然在那里冲突**：实测 PR #21 / #22 / #23 连着三次撞的都是同一处，每次都要手工把
两侧的条目都留下。这不是谁不小心，是那个文件的形状决定的。

先试了行业里最常见的那条路：`.gitattributes` 写 `CHANGELOG.md merge=union`。
**实测不成立**——GitHub 不跑 gitattributes 里的合并驱动，同一个锚点两侧各加一条，
PR 上照样报 `CONFLICTING`（20261019 用一条 throwaway PR 量的，结论留在 PR #24 的
关闭说明里）。所以不能靠合并驱动，得换形状。

碎片把"插入点"从一个**位置**换成一个新**文件**：
`changelog.d/<YYYYMMDD>-<slug>.md`。两个 PR 各加一个新文件 ⇒ git 三路合并永远不冲突。
汇总在本脚本里，什么时候跑由人定（合并时、或发布前）。

## 碎片的写法

内容**与直接写进 `CHANGELOG.md` 的那一段逐字相同**：以 `- ` 开头的条目块，续行缩进两格。
别在里面写 `## ` 标题（那会破坏 CHANGELOG 的日期分节）。日期取文件名前八位，
是**本地（+08:00）日期**，与 `CHANGELOG.md` 的日期戳同源。

## 用法

    .venv/bin/python scripts/dev/changelog.py --new pr22-changelog-fragments
        # 开一个今天的碎片（写一份模板，自己去填）
    .venv/bin/python scripts/dev/changelog.py --check
        # 只看会做什么，不写盘
    .venv/bin/python scripts/dev/changelog.py
        # 汇总：折进 CHANGELOG.md，并删掉已汇总的碎片

判据在 `tests/test_changelog_fragments.py`（碎片本身合不合法、组装器对不对、幂等不幂等），
`tests/test_style_hygiene.py` ③ 判"最新记录日期 == 最后一笔提交日期"（CHANGELOG 那一节与
`changelog.d/` 里最新的碎片**合起来**算）。
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
FRAG_DIR = ROOT / "changelog.d"
CHANGELOG = ROOT / "CHANGELOG.md"

# 碎片文件名：`<8 位日期>-<ascii slug>.md`。slug 限 ASCII 小写/数字/连字符——
# 中文 slug 在文件名排序上不稳定（不同文件系统的归一化不同），而这里**靠文件名排序定次序**。
FRAG_RE = re.compile(r"^(?P<date>\d{8})-(?P<slug>[a-z0-9][a-z0-9-]*)\.md$")
SECTION_RE = re.compile(r"^##\s+(\d{8})\s*$")
CONFLICT_RE = re.compile(r"^(?:<{7}|={7}|>{7})", re.M)

TEMPLATE = """\
- **一句话说清这一笔改了什么**（⚠️ 有/无对外行为变更）。两件：
  ① 为什么改（失效形状是什么、代价是什么）；
  ② 判据在哪（哪个套件、判的是什么）。
"""


class FragmentError(ValueError):
    """碎片不合法。**响亮地报**，不要静默跳过——静默跳过等于这条变更没被记录。"""


def today_cn() -> str:
    """本仓钟面的今天（+08:00），与 `CHANGELOG.md` 的日期戳同源。"""
    return (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
            .strftime("%Y%m%d"))


def is_real_date(s: str) -> bool:
    try:
        datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return False
    return True


def body_problems(body: str) -> list[str]:
    """一段碎片正文的问题清单（空表 = 合法）。纯函数，判据直接喂合成输入。"""
    bad: list[str] = []
    if not body.strip():
        bad.append("内容为空")
    if not body.lstrip().startswith("- "):
        bad.append("内容必须以 `- ` 开头（它要被原样折进 CHANGELOG 的条目列表）")
    if CONFLICT_RE.search(body):
        bad.append("内容里有冲突标记（`<<<<<<<` / `=======` / `>>>>>>>`）")
    if re.search(r"^##\s", body, re.M):
        bad.append("内容里有 `## ` 标题（那会破坏 CHANGELOG 的日期分节）")
    return bad


def parse_fragment(name: str, text: str) -> tuple[str, str, str]:
    """`(日期, slug, 正文)`；不合法就抛 `FragmentError`（带文件名，便于定位）。"""
    m = FRAG_RE.match(name)
    if not m:
        raise FragmentError(
            f"{name}：文件名必须是 `<8 位日期>-<ascii-slug>.md`（slug 只许小写字母/数字/连字符）")
    date = m.group("date")
    if not is_real_date(date):
        raise FragmentError(f"{name}：前八位不是真实日期（{date}）")
    bad = body_problems(text)
    if bad:
        raise FragmentError(f"{name}：" + "；".join(bad))
    return date, m.group("slug"), text.strip()


def read_fragments(frag_dir: pathlib.Path | None = None) -> list[tuple[str, str, str]]:
    """`[(日期, 文件名, 正文)]`。任何一条不合法都抛——**不许跳过**。

    ⚠️ 缺省值**不能**写成 `frag_dir: Path = FRAG_DIR`：那是**定义时**求值的，套件把
    `changelog.FRAG_DIR` 指到临时目录之后它读的还是老路径 ⇒ 端到端那条判据会静默
    变成"没有碎片、什么都不用做"（本仓最贵的那类失效）。
    """
    frag_dir = FRAG_DIR if frag_dir is None else frag_dir
    if not frag_dir.is_dir():
        return []
    out: list[tuple[str, str, str]] = []
    for p in sorted(frag_dir.glob("*.md")):
        if p.name == "README.md":       # 目录自己的说明，不是碎片
            continue
        date, _slug, body = parse_fragment(p.name, p.read_text(encoding="utf-8"))
        out.append((date, p.name, body))
    return out


def _sections(lines: list[str]) -> list[tuple[str, int, int]]:
    """`[(日期, 标题行下标, 正文起始下标)]`（正文起始 = 标题之后跳过紧跟空行的那一行）。"""
    out: list[tuple[str, int, int]] = []
    for i, ln in enumerate(lines):
        m = SECTION_RE.match(ln)
        if not m:
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        out.append((m.group(1), i, j))
    return out


def assemble(changelog: str, fragments: list[tuple[str, str, str]]) -> str:
    """把碎片折进 CHANGELOG 文本。**纯函数**（判据拿合成输入直接驱动它）。

    次序：日期倒序；同一天内按**文件名**升序（文件名带日期前缀 ⇒ 稳定、可复现）。
    已存在的日期节 ⇒ 插在那一节的**组首**（与手写时的习惯一致）；不存在的 ⇒ 按日期倒序
    新建一节。**同一条正文出现两次就插两次**——去重是 `read_fragments` 那一侧的判据管的
    （见 `tests/test_changelog_fragments.py` ②），这里不做静默去重。
    """
    if not fragments:
        return changelog
    lines = changelog.splitlines()
    by_date: dict[str, list[str]] = {}
    for date, name, body in sorted(fragments, key=lambda f: (f[0], f[1])):
        by_date.setdefault(date, []).append(body)
    # 日期倒序处理：新建节时后面的插入不会挪动前面已记下的下标，但同一份 lines 是就地改的，
    # 所以**每次插入都重新推一遍分节表**，不缓存下标。
    for date in sorted(by_date, reverse=True):
        chunk = [ln for body in by_date[date] for ln in (body.splitlines() + [""])]
        while chunk and not chunk[-1].strip():
            chunk.pop()
        secs = _sections(lines)
        hit = next((s for s in secs if s[0] == date), None)
        if hit is not None:
            at = hit[2]
            lines[at:at] = chunk + [""]
        else:
            newer = [s for s in secs if s[0] < date]
            block = [f"## {date}", ""] + chunk + [""]
            if newer:
                lines[newer[0][1]:newer[0][1]] = block
            else:
                while lines and not lines[-1].strip():
                    lines.pop()
                lines += [""] + block
    text = "\n".join(lines)
    return text if text.endswith("\n") else text + "\n"


def build(*, apply: bool) -> tuple[int, str]:
    """→ (折进去的碎片数, 报告)。`apply=False` 只看不写。"""
    frags = read_fragments()
    if not frags:
        return 0, "changelog.d/ 里没有待汇总的碎片，什么都不用做。"
    text = CHANGELOG.read_text(encoding="utf-8")
    out = assemble(text, frags)
    names = "、".join(n for _d, n, _b in frags)
    if not apply:
        return len(frags), f"会折进 {len(frags)} 条：{names}（--check 不写盘）"
    CHANGELOG.write_text(out, encoding="utf-8")
    for _d, name, _b in frags:
        (FRAG_DIR / name).unlink()
    return len(frags), f"已折进 {len(frags)} 条并删掉碎片：{names}"


def new_fragment(slug: str) -> pathlib.Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug or ""):
        raise SystemExit(f"✗ slug 只许小写字母/数字/连字符，收到 {slug!r}")
    FRAG_DIR.mkdir(exist_ok=True)
    p = FRAG_DIR / f"{today_cn()}-{slug}.md"
    if p.exists():
        raise SystemExit(f"✗ 已经有一个了：{p.relative_to(ROOT)}")
    p.write_text(TEMPLATE, encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 changelog.d/ 的碎片汇总进 CHANGELOG.md")
    ap.add_argument("--check", action="store_true", help="只看会做什么，不写盘")
    ap.add_argument("--new", metavar="SLUG", help="开一个今天的碎片（写模板）")
    args = ap.parse_args(argv)
    try:
        if args.new:
            p = new_fragment(args.new)
            print(f"已开：{p.relative_to(ROOT)}（填好之后跑一次不带参数的本脚本）")
            return 0
        n, msg = build(apply=not args.check)
    except FragmentError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    print(msg)
    return 0 if (args.check or n >= 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
