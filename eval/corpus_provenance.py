# -*- coding: utf-8 -*-
"""两件"地还是不是那块地"的事。
"""
import json
import os
import re

CORPUS_PROVENANCE_FILE = "eval/golden/provenance.json"
CORPUS_PROV_OK = "ok"
CORPUS_PROV_FOREIGN = "foreign"
CORPUS_PROV_UNKNOWN = "unknown"


def provenance_path_for(golden_file: str) -> str:
    """出处声明住在**golden 文件旁边**：`<golden 目录>/provenance.json`。

    这条契约是给"自己写一套 golden"的人用的（`eval/fixtures/` 就是第一份例子）：用例与
    它的出处声明必须一起搬——声明说的是"这套用例锚在哪块地上"，两者拆开就会变成用 A 的
    声明去判 B 的用例（错得还很安静）。默认那份 golden 在 `eval/golden/`，算出来正是
    `eval/golden/provenance.json`，与 `CORPUS_PROVENANCE_FILE` 指向同一个文件。
    """
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
    """语料快照（`[{type,id,title,content}]`）；**取不到与是空的都返回 `None`**，不抛。

    取法（`get_index()` → 空则 `build()` → `docs_snapshot()`）与"取不到就 None"这两条
    语义原来长在 `run_golden.judge_corpus()` 里，这里搬出来是为了让两个跑法共用一处——
    `judge_corpus()` 现在只是它外面那层给判据看的打印。

    取不到时**打一行**（带异常类型），因为"为什么没有语料"是要人去查的第一件事；
    "确实是空的"不打——那不是故障，`corpus_check` 那边已经在报篇数了。
    """
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
    """标题归一：**去掉全部空白**再比。

    库里那条 `ESP32-S3 OTA 问题与解决记录 ` 带一个尾空格——逐字节比会红在一个
    "看起来一模一样"的标题上，而那种红比不判还坏（它会教人把这条判据关掉）。
    """
    return re.sub(r"\s+", "", t or "").casefold()


# Share title normalization between corpus and entity-premise checks.
norm_title = _norm_title


def check_corpus_premises(docs, provenance_file: "str | None" = None) -> tuple:
    """语料出处前提 ⇒ `(state, detail, row)`；`docs` = `snapshot_docs()` 的快照。

    `provenance_file` 不传就用本模块默认那一份；`--golden <别的用例文件>` 时由跑法传
    `provenance_path_for(golden)`（用例与它的声明必须同目录，见那个函数的注）。
    """
    path = provenance_file or CORPUS_PROVENANCE_FILE
    prov = load_corpus_provenance(path)
    anchors = [a for a in (prov.get("anchors") or [])
               if isinstance(a, dict) and (a.get("title") or "").strip()]
    row = {"file": path,
           "declared_for": (prov.get("declared_for") or {}).get("api_base", ""),
           "anchors": len(anchors), "present": [], "missing": [], "state": ""}
    if not anchors:
        # 声明缺了/写坏了：判不了 ⇒ 不动（与"快照取不到"同一条纪律，都走 unknown）。
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
# 声明里必须写 why 的下限（同 `run_golden` 的 `premise_no_popup.capability_boundary`）：
# 那一半是"为什么这条判据要求这件东西在场"，机器判不了，但至少要写下来。
ENTITY_WHY_MIN = 8
# 锚可以是**标题**也可以是 **note id**，两者只写一个（都写了按 id 核，见下面那个 `if`）：
#   title   —— 判据（或主人那句话）**按标题**点名那篇文章时的形状；
#   note_id —— 判据**按 id** 点名时的形状（`require_exec_args: article_id`、或
#              用例把文章 id 写在 `context.current_url` 里 ⇒ `get_article_detail(<id>)`）。
# ⚠ 这里的 id 与语料出处闸"不许拿 id 当锚点"那条**不矛盾**：那条说的是**跨站点**认不出
# 同一块地（别人的库 id 完全另排）；这一条问的是**同一块地内部**"这件东西还在不在"，
# 而 id 正是工具出口与 `current_url` 用的那个坐标——按 id 核比按标题核更稳（改名不误伤）。
# 键名就一个（`note_id`）：多一个别名就是第二个会漂移的读法。


def check_entity_premises(cases: list, docs) -> tuple:
    """逐条核验"判据点名的实体还在不在"，返回 (留下的用例, 未评估的 id, 逐条结论)。

    结论 `state="changed"` = 那件东西**已经不在公开面上了**（被改成 private / 删了 /
    改了名）⇒ 该用例未评估。声明本身写坏（kind 不在 `ENTITY_KINDS`、锚一个都没写、why 太短）
    **也归 changed**：那是"判据坏了"，方向必须偏到"未评估"那一侧（静默放行就是
    `run_golden` 那段注释说的"判据看着在、其实不在"）。

    锚两种写法二选一（见上面 `ENTITY_KINDS` 那段）：`note_id` 优先（精确、改名不误伤），
    给了 id 就**只**按 id 核；没给才按 `title` 去语料里找。

    `docs` = `snapshot_docs()` 的快照，**由调用方取一次、与语料出处闸共用**：两处问的是
    同一份数据，各取一次就是第二个会漂移的地方。
    """
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
            # 快照取不到/为空 ⇒ **判不了**，照跑（见上面那条 ⚠）。这一栏要单列：
            # "没报错"不等于"核过了"（同 `premise_absent` 的 unchecked 那栏）。
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
    """把实体前提的结论翻成要打的那几行（两个跑法共用；各写一份措辞早晚会漏那句话）。"""
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
        # 「没报错」不等于「核过了」：快照取不到的那些，哨兵判不了。**照跑**那一句必须
        # 在行上（同语料闸 unknown 那行）：不说的话，读的人会把"这一栏没红"读成"核过了"。
        lines.append(f"[entity] {len(unknown)} 条实体前提判不了"
                     f"（公开语料快照取不到或为空）——**照跑**；这一栏没红不等于核过了："
                     f"{unknown}")
    return lines


def report_lines(state: str, detail: str, row: dict) -> list:
    """把结论翻成要打的那几行（两个跑法共用；各写一份措辞早晚会漏掉最关键的那句）。"""
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
        # 「不知道」不等于「不可用」：照跑（同 identity_preflight 那条纪律）。
        return [f"[corpus-premise] ⚠ 语料出处判不了：{detail}"
                " —— 照跑；本轮若大面积飘红，先看这一行"]
    return []
