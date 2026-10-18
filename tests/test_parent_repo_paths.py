#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注释里指向**父仓**的路径，必须当场说明它不在本仓（20261018，批 1 自净）。

**要回答的问题**：本仓是父仓 `Saudade-Blog` 里的一个子目录（同时是一个**独立公开仓库**），
两边各有一套同名目录。于是注释里写 `scripts/healthcheck.sh`（父仓的探针）时，读者**没法从
路径本身判断它是谁的**——而本仓**真的有一个 `scripts/`**（`nightly_regression.sh` 等十几件）、
**也真的有一个 `frontend/`**（只有一份 README）。读者 grep 一遍找不到，只能靠猜。

**为什么只判这三个前缀**（`scripts/` `deploy/` `frontend/`）：
  · `src/` 是最大的一族（代码里 70 多处），但本仓**结构上不可能有** `src/`——这是 Python
    agent 仓，入口是仓根的 `server.py`。读者不会去本仓找它 ⇒ 判它是纯噪声。
  · `iot/` 同理（本仓结构上没有，代码里 0 处命中）。
  · 剩下这三个恰好是**同名或形近**的那一族：`scripts/` 与 `frontend/` 本仓都有目录、
    `deploy/` 本仓没有而父仓有；而这三处的路径正是**读者会被指使去找**的东西
    （探针、迁移脚本、前端渲染契约的出处）。

**判法**：`*.py` 里**反引号**括起来的、以这三个前缀开头的路径，若**本仓没有这个文件**，
则**同一行**必须出现「父仓」或「不在本仓」。**逐行判**——跨行豁免会把判断权交回给读者，
而那正是这条判据要消掉的东西。

**射程划界（别把这条读成"父仓路径都有人管了"）**：
  · 只管 `*.py`。markdown 里那近 200 处（绝大多数是 `eval/report/**` 生成物）**不判**；
  · 只管**反引号**括起来的（本仓写路径的惯例）。裸写 `见 scripts/foo.sh` 它看不见；
  · `src/` 与 `iot/` 前缀**有意不判**（理由见上）；
  · 它**不**判断"这个路径在父仓里还在不在"——本机有父仓，CI 上没有（浅克隆 + 私有仓）。
    判据要能在 CI 上跑，所以只判"本仓有没有"这一半。想验另一半，用 `SAUDADE_PARENT_REPO`
    那族跨语言守卫（`tests/_parent_repo.py`）。

用法：.venv/bin/python tests/test_parent_repo_paths.py
退出码：0 全部带限定 / 1 有裸路径。
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 同名或形近、且本仓**可能**有目录的那三个前缀。理由见模块头注。
PARENT_PREFIXES = ("scripts/", "deploy/", "frontend/")
# 同一行出现任一个，就算说清了"这是谁的"。
_MARKERS = ("父仓", "不在本仓")

_PATH_RE = re.compile(r"`((?:" + "|".join(re.escape(p) for p in PARENT_PREFIXES)
                      + r")[A-Za-z0-9_./\-]+)`")

_SKIP_DIRS = {".venv", "node_modules", ".git", "__pycache__", ".mypy_cache"}

# 本仓确实存在的、同前缀的一个文件——用来证明"本仓有 ⇒ 不判"这一半不是空过。
# `_self_check` 会先断言它还在，改名了就当场喊，而不是让那一条静默变绿。
_LOCAL_SAMPLE = "scripts/nightly_regression.sh"


def scan_text(text: str) -> list[tuple[int, str]]:
    """一份源码里「裸着的父仓路径」= [(行号, 路径)]。**纯函数**——正控直接喂它。"""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        if any(m in line for m in _MARKERS):
            continue
        for rel in _PATH_RE.findall(line):
            if not (ROOT / rel).exists():
                out.append((i, rel))
    return out


def scan_repo() -> tuple[list[tuple[str, int, str]], int]:
    hits: list[tuple[str, int, str]] = []
    n_files = 0
    for p in sorted(ROOT.rglob("*.py")):
        if _SKIP_DIRS & set(p.relative_to(ROOT).parts):
            continue
        n_files += 1
        text = p.read_text(encoding="utf-8", errors="ignore")
        for line_no, rel in scan_text(text):
            hits.append((p.relative_to(ROOT).as_posix(), line_no, rel))
    return hits, n_files


def _self_check() -> None:
    """正控 + 反控 + 两条射程对照，缺一条这闸就有可能是空的。"""
    assert (ROOT / _LOCAL_SAMPLE).is_file(), \
        f"对照样本 {_LOCAL_SAMPLE} 不在本仓了 —— 换一个本仓同前缀的文件再跑"
    # 控制样本**拆开拼**：整条字面写在这里的话，本文件自己会被扫成"裸着的父仓路径"
    # （判据扫全仓 `*.py`，包括本文件）。这是本仓既有的做法，不是给自己开后门——
    # 下面每条断言都真跑了 `scan_text`，只是输入不是源码里的一个字面量。
    _bare = "# 见 `" + "scripts/" + "healthcheck.sh` 第 7 项\n"
    _marked = "# 见父仓 `" + "scripts/" + "healthcheck.sh` 第 7 项\n"
    _marked2 = "# 见 `" + "scripts/" + "healthcheck.sh`（不在本仓）\n"
    # 正控：裸着的父仓路径必须被抓住
    assert scan_text(_bare), "裸着的父仓路径没被抓住 —— 闸是空的"
    # 反控：同一行带上限定词就不该抓
    assert not scan_text(_marked), "带了「父仓」还红 —— 这是「见谁都红」"
    assert not scan_text(_marked2), "带「不在本仓」还红"
    # 对照一：**本仓有的**同前缀路径不判（否则这条判据会把本仓文件也扫成幽灵）
    assert not scan_text(f"# 见 `{_LOCAL_SAMPLE}`\n"), \
        f"{_LOCAL_SAMPLE} 本仓就有，却被判成父仓路径"
    # 对照二：**射程划界**——`src/` 有意不判，这一条把这个取舍钉在判据里
    assert not scan_text("# 见 `src/routes/chat.rs`\n"), \
        "`src/` 被扫进来了 —— 射程与头注说的不一致"
    # 对照三：前缀对但**不是**反引号包着的（本仓惯例之外）不判
    assert not scan_text("# 见 scripts/healthcheck.sh 第 7 项\n"), \
        "没反引号的也扫进来了 —— 射程比头注宽"


def main() -> int:
    _self_check()
    hits, n_files = scan_repo()
    print(f"扫描 {n_files} 个 .py；前缀 {list(PARENT_PREFIXES)}；"
          f"限定词 {list(_MARKERS)}（逐行判）")
    if not hits:
        print("✓ 没有裸着的父仓路径")
        return 0
    for rel, line_no, path in hits:
        print(f"✗ {rel}:{line_no}  `{path}` —— 本仓没有这个文件，"
              f"同一行没说是谁的（加「父仓」）")
    print(f"\n{len(hits)} 处。这些路径都在父仓 `Saudade-Blog` 里，本仓没有同名文件；"
          f"读者在本仓 grep 只会一无所获。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
