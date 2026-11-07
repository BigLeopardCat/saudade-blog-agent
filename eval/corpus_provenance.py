# -*- coding: utf-8 -*-
"""Check corpus provenance and case-specific source-entity premises."""
import json
import os
import re

CORPUS_PROVENANCE_FILE = "eval/golden/provenance.json"
CORPUS_PROV_OK = "ok"
CORPUS_PROV_FOREIGN = "foreign"
CORPUS_PROV_UNKNOWN = "unknown"


def provenance_path_for(golden_file: str) -> str:
    """Locate provenance.json beside the selected golden case file."""
    return os.path.join(os.path.dirname(str(golden_file)) or ".", "provenance.json")


def load_corpus_provenance(path: "str | None" = None) -> dict:
    """读出处声明；读不到/写坏 ⇒ 空 dict（判不了 ⇒ 走 unknown，不动）。"""
    path = path or CORPUS_PROVENANCE_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[corpus-premise] 读不到 {path}（{type(e).__name__}: {e}）")
        return {}
    return data if isinstance(data, dict) else {}


def snapshot_docs() -> "list | None":
    """Read the retrieval corpus snapshot, building an empty index once if needed.
    Return None for unavailable or empty snapshots; report retrieval exceptions."""
    try:
        from rag.search import get_index
        idx = get_index()
        snap = idx.docs_snapshot()
        if not snap:
            idx.build()
            snap = idx.docs_snapshot()
    except Exception as e:
        print(f"[corpus] 语料快照取不到（{type(e).__name__}: {e}）")
        return None
    return list(snap) if snap else None


def _norm_title(t: str) -> str:
    """Remove whitespace and case-fold titles before comparing source anchors."""
    return re.sub(r"\s+", "", t or "").casefold()


# Share title normalization between corpus and entity-premise checks.
norm_title = _norm_title


def check_corpus_premises(docs, provenance_file: "str | None" = None) -> tuple:
    """Compare a shared corpus snapshot with the selected provenance declaration.
    Return its state, diagnostic detail and structured evidence."""
    path = provenance_file or CORPUS_PROVENANCE_FILE
    prov = load_corpus_provenance(path)
    anchors = [a for a in (prov.get("anchors") or [])
               if isinstance(a, dict) and (a.get("title") or "").strip()]
    row = {"file": path,
           "declared_for": (prov.get("declared_for") or {}).get("api_base", ""),
           "anchors": len(anchors), "present": [], "missing": [], "state": ""}
    if not anchors:
        # Missing usable anchors leave corpus provenance unknown.
        row["state"] = CORPUS_PROV_UNKNOWN
        return CORPUS_PROV_UNKNOWN, "声明里没有可用的锚点（文件缺失或写坏了）", row
    if not docs:
        # An empty or unavailable snapshot cannot establish that an entity is absent.
        row["state"] = CORPUS_PROV_UNKNOWN
        return (CORPUS_PROV_UNKNOWN,
                "语料快照取不到或为空——接口不通 / `BLOG_API_BASE` 指错地址 / 站点还没有"
                "公开文章 / 索引没建起来，这几种成因读数一样",
                row)
    titles = [_norm_title(d.get("title")) for d in docs]
    for a in anchors:
        na = _norm_title(a["title"])
        # Title substring matching tolerates a source title suffix.
        hit = bool(na) and any(na in t for t in titles)
        (row["present"] if hit else row["missing"]).append(a["title"])
    need = int(prov.get("min_present") or max(1, len(anchors) // 2))
    row["state"] = CORPUS_PROV_OK if len(row["present"]) >= need else CORPUS_PROV_FOREIGN
    row["min_present"] = need
    row["corpus_total"] = len(docs)
    return (row["state"],
            f"语料里命中 {len(row['present'])}/{len(anchors)} 个申报锚点（需要 {need} 个）；"
            f"对不上的 {len(row['missing'])} 个：{row['missing']}；当前语料 {len(docs)} 篇",
            row)


# Entity premises distinguish missing source entities from model execution failures.
ENTITY_KINDS: tuple[str, ...] = ("note_visible",)
# Entity declarations must explain why the source is required.
ENTITY_WHY_MIN = 8
# Entity anchors use note_id when supplied, otherwise title.
# IDs identify entities within the verified corpus, not corpus provenance across sites.


def check_entity_premises(cases: list, docs) -> tuple:
    """Check source-entity premises and return eligible cases, unassessed IDs and evidence.

    Malformed declarations or missing entities are marked changed and unassessed.
    An unavailable snapshot is unchecked and does not establish entity absence.
    Prefer an explicit note_id over title matching. Reuse the corpus-premise
    snapshot so both checks observe the same source data."""
    kept: list = []
    skipped: list = []
    rows: list = []
    for c in cases:
        pe = c.get("premise_entity")
        if not isinstance(pe, dict):
            kept.append(c)
            continue
        kind = str(pe.get("kind") or "").strip()
        title = str(pe.get("title") or "").strip()
        nid = pe.get("note_id")
        why = str(pe.get("why") or "").strip()
        row = {"id": c.get("id"), "kind": kind, "state": "ok",
               "title": title, "note_id": nid, "hit": [], "why": why}
        if kind not in ENTITY_KINDS:
            row["hit"] = [f"kind 不是 {'/'.join(ENTITY_KINDS)} 之一：{kind!r}"]
        elif not title and nid is None:
            row["hit"] = ["声明里没写锚点（title 或 note_id 二选一）"]
        elif len(why) < ENTITY_WHY_MIN:
            row["hit"] = [f"必须写 why（≥{ENTITY_WHY_MIN} 字——为什么这条判据要求它在场，"
                          "机器判不了那一半）"]
        elif not docs:
            # Unavailable snapshots remain unchecked; do not infer absence or silently certify them.
            row["state"] = "unknown"
            rows.append(row)
            kept.append(c)
            continue
        elif nid is not None:
            if not any(str(d.get("id")) == str(nid) for d in docs):
                row["hit"] = [f"公开语料里没有 id={nid} 的文章"]
        else:
            nt = norm_title(title)
            if not any(nt and nt in norm_title(d.get("title")) for d in docs):
                row["hit"] = [f"公开语料里没有标题含「{title}」的文章"]
        if row["hit"]:
            row["state"] = "changed"
            skipped.append(c.get("id"))
        else:
            kept.append(c)
        rows.append(row)
    return kept, skipped, rows


def entity_report_lines(rows: list, skipped: list) -> list:
    """Render entity-premise states, including unchecked evidence, for evaluation reports."""
    lines: list = []
    for r in rows:
        if r.get("state") == "changed":
            _anchor = r.get("title") or f"note_id={r.get('note_id')}"
            lines.append(
                f"[entity] ⚠ {r['id']}：判据点名的那件东西**已不在公开面上**"
                f"（{r['kind']}「{_anchor}」——{(r.get('hit') or [])[:2]}）"
                " ⇒ 本条**未评估**（**这不是模型退化**：站主把文章改成 private、删了、"
                "或改了名，判据都会这样红——改锚到一件还在的实体，或改判据，"
                "见报告 entity_checks）")
    if skipped:
        lines.append(f"[entity] ⇒ {len(skipped)} 条用例本轮**未评估**：{skipped}")
    unknown = [r["id"] for r in rows if r.get("state") == "unknown"]
    if unknown:
        # Report unchecked premises explicitly even when evaluation continues.
        lines.append(f"[entity] {len(unknown)} 条实体前提判不了"
                     f"（公开语料快照取不到或为空）——**照跑**；这一栏没红不等于核过了："
                     f"{unknown}")
    return lines


def report_lines(state: str, detail: str, row: dict) -> list:
    """Render corpus-premise states and the provenance declaration used."""
    if state == CORPUS_PROV_FOREIGN:
        return [
            f"[corpus-premise] ⚠ 这一轮的语料**不是**这套 golden 的那一份：{detail}",
            "[corpus-premise]    这套 golden 的每条期望都锚在声明里那个站点上"
            f"（{row.get('declared_for') or '未声明'}）——语料一换，判据就对不上，"
            "**这不是模型退化**：红色里判的是判据自己",
            "[corpus-premise]    自己部署的人：这套用例要用自己的语料重做判据"
            "（或先跑自包含夹具，见 eval/fixtures/README.md）；"
            # Report the corpus declaration actually used, including custom corpus paths.
            f"只是标题改了名 ⇒ 修 {row.get('file') or CORPUS_PROVENANCE_FILE} 的 anchors",
        ]
    if state == CORPUS_PROV_UNKNOWN:
        # Unknown provenance is reported separately when evaluation continues.
        return [f"[corpus-premise] ⚠ 语料出处判不了：{detail}"
                " —— 照跑；本轮若大面积飘红，先看这一行"]
    return []
