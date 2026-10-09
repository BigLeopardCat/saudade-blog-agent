#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手写文档的**索引 / 链接 / 跨界锚点**（离线、秒级）。

要回答的问题：这一仓 12,000 行手写文档此前**没有任何机制**看着它们——链接指向不存在的
文件、`docs/` 新加一份却没人进索引、把父仓按编号引用的章节改名，三种都会**静默**发生
（没有任何东西会红）。这份文件把这三件事变成判据：

  ① 每份手写文档里的**相对 md 链接**都指得着（指到仓外的，例如父仓，不在此列）；
  ② `docs/README.md` 的索引与磁盘上的 `docs/*.md` **双向一致**（漏列、列了已删的都红）；
  ③ 那四组**跨仓引用锚点**还在（父仓按编号/标题引用它们，改了会静默打断父仓）；
  ④ 文档里点名的**仓内 `xxx.py` 路径**存在——除非同一行标了「已删 / 待建 / 未建 / 占位」。

**为什么不查 CHANGELOG 与仓根 md 的 ④**：那是记录型的文件，逐字保留了当时的事实
（例如"这次新增了 `agent/oled_draw.py`"——那个文件后来删了，但**当时确实新增过**）。
拿"文件今天在不在"去判历史叙述，是把记录型文档误当现状型文档读。

**红基线（自查用）**：
  - 删掉 `docs/README.md` 里 `multimodal-retrieval.md` 那一行 ⇒ ② 必红；
  - 把 `docs/问题记录.md` 的 `### 2.1` 改成 `### 2.2` ⇒ ③ 必红；
  - 把 `docs/lint-baseline.md` 的「（**待建**）：\`eval/lint_gate.py\`」里那对括号去掉 ⇒ ④ 必红。

用法：.venv/bin/python tests/test_docs_links.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {desc}")
    else:
        FAILS.append(f"{desc} —— {detail}")
        print(f"  ❌ {desc} —— {detail}")


# 只扫**手写**文档：仓根 md + docs/**（含 adr/）。排除生成物目录与依赖目录。
SKIP_PARTS = {".venv", "node_modules", ".git", "_parent", "runs", "__pycache__"}
HANDWRITTEN = [p for p in list(ROOT.glob("*.md")) + list(ROOT.glob("docs/**/*.md"))
               if not any(s in p.parts for s in SKIP_PARTS)]

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

print(f"① 手写文档里的相对 md 链接都指得着（扫 {len(HANDWRITTEN)} 份）")
_broken: list[str] = []
for p in HANDWRITTEN:
    for m in _LINK_RE.finditer(p.read_text(encoding="utf-8")):
        t = m.group(1).split("#")[0]
        if not t or not t.endswith(".md") or t.startswith(("http", "mailto:", "/")):
            continue
        r = (p.parent / t).resolve()
        try:
            r.relative_to(ROOT)          # 指到仓外（父仓）的不归本套件管
        except ValueError:
            continue
        if not r.is_file():
            _broken.append(f"{p.relative_to(ROOT)} → {t}")
check("没有指向不存在文件的相对链接", not _broken, "；".join(_broken))

print("\n② docs/README.md 索引 ↔ 磁盘 docs/*.md 双向一致")
INDEX = ROOT / "docs/README.md"
_index_txt = INDEX.read_text(encoding="utf-8")
_listed = {t for m in _LINK_RE.finditer(_index_txt)
           for t in [m.group(1)] if re.fullmatch(r"[^/()]+\.md", t)}
_disk = {p.name for p in (ROOT / "docs").glob("*.md")} - {INDEX.name}
check("索引把磁盘上每份 docs/*.md 都列了", not (_disk - _listed),
      f"漏列：{sorted(_disk - _listed)}")
check("索引里每一行都对应磁盘上真有的文件", not (_listed - _disk),
      f"列了不存在的：{sorted(_listed - _disk)}")
check("索引自己也提一下 adr/ 的索引",
      "adr/README.md" in _index_txt, "少了 adr/README.md 的链接")

print("\n③ 跨仓引用锚点仍在（父仓按编号/标题引，改了会静默打断）")
_ANCHORS = [
    ("docs/问题记录.md", r"^### 2\.1\b", "§2.1（父仓 iot-device-integration / 固件开发指南）"),
    ("docs/secretary.md", r"^### 3\.4\b", "§3.4（父仓 security-boundary）"),
    ("docs/secretary.md", r"^### 3\.6\b", "§3.6（父仓 security-boundary / zako_role 迁移）"),
    ("docs/secretary.md", r"^### 5\.2\b", "§5.2（父仓 security-boundary）"),
    ("docs/secretary.md", r"^### 5\.3\b", "§5.3（父仓 security-boundary）"),
    ("docs/agent-architecture.md", r"^## 3\. 一次对话的完整链路\s*$", "《3. 一次对话的完整链路》（父仓 README）"),
] + [("docs/toolcall-stability-roadmap.md", rf"^### D{n}\b", f"D{n}（execution_log_struct 迁移）")
     for n in range(1, 7)]
for rel, pat, why in _ANCHORS:
    txt = (ROOT / rel).read_text(encoding="utf-8")
    check(f"{rel} 仍有 {why}", bool(re.search(pat, txt, re.M)), f"找不到 /{pat}/m")

print("\n④ docs/ 里点名的仓内 .py 路径存在（历史叙述除外）")
_TOPS = {d.name for d in ROOT.iterdir()
         if d.is_dir() and d.name not in SKIP_PARTS and not d.name.startswith(".")}
_MARKERS = ("已删", "删除", "待建", "未建", "占位", "xxx")
_PY_RE = re.compile(r"`([A-Za-z0-9_./\-]+/[A-Za-z0-9_./\-]+\.py)`")
_ghost: list[str] = []
for p in HANDWRITTEN:
    if p.parent == ROOT and p.name != "README.md":
        continue                          # 仓根只查 README（见文件头注：CHANGELOG 是记录型）
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        for m in _PY_RE.finditer(line):
            t = m.group(1)
            if t.startswith("/") or ".." in t or t.split("/")[0] not in _TOPS:
                continue                  # 库内路径（langgraph/…）与父仓路径不归本套件管
            if (ROOT / t).exists() or any(k in line for k in _MARKERS):
                continue
            _ghost.append(f"{p.relative_to(ROOT)}:{i} → {t}")
check("没有指向不存在文件的仓内 .py 路径（除已标已删/待建/未建的）",
      not _ghost, "；".join(_ghost))

print()
if FAILS:
    print(f"FAILS({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    raise SystemExit(1)
print("全部通过。")
raise SystemExit(0)
