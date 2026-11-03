"""Validate maintained documentation links, index entries and current source references."""

import pathlib
import re

from _judge import collector

FAILS: list[str] = []
check = collector(FAILS)


def _historical_documents(root):
    index = (root / "docs/README.md").read_text(encoding="utf-8")
    return {root / "docs" / name for name in re.findall(
        r"^\| \[([^]]+\.md)\].*\| 记录型[^|]*\|", index, re.M)}


def _historical_source(path, historical, root):
    return path in historical or path.parent == root / "docs/adr"


def _check_source_paths(root, handwritten, skip_parts):
    print("\n④ docs/ 里点名的仓内 .py 路径存在（历史叙述除外）")
    _TOPS = {d.name for d in root.iterdir()
             if d.is_dir() and d.name not in skip_parts and not d.name.startswith(".")}
    _MARKERS = ("已删", "删除", "待建", "未建", "占位", "xxx")
    _PY_RE = re.compile(r"`([A-Za-z0-9_./\-]+/[A-Za-z0-9_./\-]+\.py)`")
    # Recorded audits and ADRs cite their historical source snapshots. All other
    # documents remain checked, including newly added current documentation.
    historical = _historical_documents(root)
    _ghost: list[str] = []
    for p in handwritten:
        if _historical_source(p, historical, root):
            continue
        if p.parent == root and p.name != "README.md":
            continue                          # 仓根只查 README（见文件头注：CHANGELOG 是记录型）
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for m in _PY_RE.finditer(line):
                t = m.group(1)
                if t.startswith("/") or ".." in t or t.split("/")[0] not in _TOPS:
                    continue                  # 库内路径（langgraph/…）与父仓路径不归本套件管
                if (root / t).exists() or any(k in line for k in _MARKERS):
                    continue
                _ghost.append(f"{p.relative_to(root)}:{i} → {t}")
    check("没有指向不存在文件的仓内 .py 路径（除已标已删/待建/未建的）",
          not _ghost, "；".join(_ghost))


def test_docs_links():
    ROOT = pathlib.Path(__file__).resolve().parent.parent
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
        ("docs/identity-and-permissions.md", r"^### 3\.4\b", "§3.4（父仓 security-boundary）"),
        ("docs/identity-and-permissions.md", r"^### 3\.6\b", "§3.6（父仓 security-boundary / zako_role 迁移）"),
        ("docs/identity-and-permissions.md", r"^### 5\.2\b", "§5.2（父仓 security-boundary）"),
        ("docs/identity-and-permissions.md", r"^### 5\.3\b", "§5.3（父仓 security-boundary）"),
        ("docs/agent-architecture.md", r"^## 3\. 一次对话的完整链路\s*$", "《3. 一次对话的完整链路》（父仓 README）"),
    ]
    for rel, pat, why in _ANCHORS:
        txt = (ROOT / rel).read_text(encoding="utf-8")
        check(f"{rel} 仍有 {why}", bool(re.search(pat, txt, re.M)), f"找不到 /{pat}/m")
    _check_source_paths(ROOT, HANDWRITTEN, SKIP_PARTS)
    print()
    if FAILS:
        print(f"FAILS({len(FAILS)}):")
        for f in FAILS:
            print(f"  - {f}")
        raise SystemExit(1)
    print("全部通过。")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_docs_links()
