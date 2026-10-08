# -*- coding: utf-8 -*-
"""两件"地还是不是那块地"的事。

**（甲）这一轮的语料是不是这套 golden 的那一份**（20261006）。

前面四条前提闸（身份 / 事实 / 禁卡 / trace）问的都是"这一轮的环境、供给、设计还在不在"。
这一条问得更外面一层：**判据脚下这块地，还是原来那块吗**。

**（乙）判据点名的那件实体还在不在**（20261009）：同一块地上最直白的那个问题——
"这条判据锚的那篇文章还在公开面上吗"。实证、判据两半与三态语义见下面
`check_entity_premises` 上面那段。两条共用**同一次**语料快照（`snapshot_docs()`）：
都是"拿声明里的字面去公开语料里找"，各取一次就是第二个会漂移的地方。

这个仓是公开的，而 golden 的每一条期望都锚在**某一个站点**的文章上：`require_doc_terms`
的术语表从那些正文里派生、检索用例期望命中的就是那几篇、id 与标题只在那个库里对得上。
别人 clone 下来对着**自己的**博客跑，判据一定对不上——而症状与"模型退化"**长得一模一样**：
全表飘红，复审单上一条条写着模型的错话。缺的不是"更聪明的判据"，是**先说清这一轮
评的是哪块地**。（20261006 配套落地：工具出口地址变成 `BLOG_API_BASE` 可覆盖 + README /
CONTRIBUTING 点名 + `eval/fixtures/` 自包含夹具。）

判据分两半（与 `run_golden.premise_absent` 同一条纪律：**人写前提，机器算剩下那一半**）：
  人写：`eval/golden/provenance.json` 声明这套 golden 是为哪个站点、哪几篇写的；
  机器：拿**标题**去语料快照里找。**用标题不用 note id**——别人的库 id 完全另排，
        拿 id 当锚点等于把"id 恰好也叫 14"当成"语料是同一份"（那才是真的静默）。

三态（与 `identity_preflight` 同一套语义）：
  ok      锚点够 ⇒ 照跑；
  foreign **语料有东西，但锚点对不上** ⇒ 全部用例未评估（摘光 + 进 skipped_ids）；
  unknown 快照取不到 / 快照是空的 / 声明读不到 ⇒ **只 WARNING，照跑**——"不知道就不动"，
          与身份那条同源。**它同时是离线自测的安全阀**：`--only` 跑一条离线用例时
          语料本来就取不到，那时若判 foreign，"跑单条调试"会变成一件做不到的事。

⚠ **"语料是空的"归 unknown，不归 foreign**（20261006 定，别顺手"收紧"成 foreign）：
`tools/base._get` 把连不上 / 5xx / 坏 JSON 一律**吞成 `UPSTREAM_DOWN`**（不是抛异常），
`rag.search._fetch_corpus` 因此拿到一个非 list 就 break ⇒ `build()` **正常返回 0 篇**。
也就是说"站点接口不通""`BLOG_API_BASE` 指错地址""这篇博客还没有公开文章"三种情形在读数上
**长得一模一样**（一篇都没有，且不抛）。从空语料里能得出的结论只有"判不了"，
硬判 foreign 等于把一次网络故障说成"你换了语料"——而这条闸的整段理由就是反对这种归因。

⚠ 它是唯一一个**摘光全部**的前提族（其余四条各摘"要那个前提的那几条"）⇒ 它的出口不是
`run_golden.main()` 末尾那组 `if _precondition_bad or …`（那里够不着），而是**空分母那一支**
（全摘光 ⇒ `not cases`）：那里把退出码置 3 并说清原因。别在末尾再加一条够不着的分支。

**为什么是独立模块**（不是写在 run_golden.py 里）：这一个文件被三处读——`run_golden.py`、
`golden_full_run.py`（那个逐条子进程的跑法，必须在**起第一个子进程之前**判，否则一次
语料不对要白烧 18 分钟）与 `tests/test_golden_corpus_provenance.py`（离线秒级）。
前两个共用**同一个函数**（本仓纪律：闸的实现只有一处，不许"两份实现 + 一句口径一致的
注释"）；而离线套件只 import 得起这么一个轻模块——`run_golden` 一进来就是
`server` → `agent.graph` → langchain 一整串。所以本模块的**顶层**只许用标准库
（实测 13MB / 秒内）；取语料那次重导入（`rag.search`，实测 69MB）在 `snapshot_docs()`
**函数体内**，谁真去取数谁付这笔钱。
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


# 公开别名（20261009）：`run_golden.check_entity_premises` 也按标题问"那件实体还在不在场"
# ——同一个归一化只许有一处，两处归一化就是第二个会漂移的判据（同族教训见模块头注）。
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
        # 空快照 ⇒ **只能**报"判不了"（理由见模块头注：不抛的取数失败与真的没文章
        # 读数完全一样）。所以这里没有 `corpus_total` 可用，也不拿别的计数去补——
        # 缺键≠0，同族教训。
        row["state"] = CORPUS_PROV_UNKNOWN
        return (CORPUS_PROV_UNKNOWN,
                "语料快照取不到或为空——接口不通 / `BLOG_API_BASE` 指错地址 / 站点还没有"
                "公开文章 / 索引没建起来，这几种成因读数一样",
                row)
    titles = [_norm_title(d.get("title")) for d in docs]
    for a in anchors:
        na = _norm_title(a["title"])
        # 锚点取标题的**子串**即算命中：容忍标题末尾被加了后缀（如"（2026）"），
        # 但不容忍"完全不同的另一篇同义标题"。
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


# ── "判据点名的那件实体还在不在"（20261009）──────────────────────────────────
# 上面那条问的是"这一轮评的是哪块地"；这一条问的是同一块地上**最直白的那句**：
# **这条判据点名的那个东西，还在不在**。
#
# 一手证据：`rag_test4_cover`（「小猫咪，测试文章 TEST8 里画的是什么呀」，
# `require_tool_calls:[get_article_detail]`）从 20261008 13:56 起恒红。**红的不是模型**
# ——站主在 06:44:46 把 note 13（标题 TEST8）改成了 `status='private'`，模型此后每次
# **如实**答"站内没有 TEST8"，判据把它读成"没去读文章"。偶发的"绿"还是**假绿**：模型答
# 「站内**没有叫** TEST8 的测试文章」，恰好绕开了词表 `[没找到/找不到/没有这个]`，又顺手
# 套了个别的 `get_article_detail` ⇒ 判据此刻在**奖励乱读一篇、惩罚诚实回答**。
#
# 判据两半（同 `run_golden.premise_absent` 的纪律：**人写前提、机器算剩下那一半**）：
#   人写：用例里落笔 `premise_entity`——"这条判据锚在这件东西在场"；
#   机器：拿**标题**去**公开语料快照**（本模块的 `snapshot_docs()`，与语料出处闸同一个
#         取数口）里找。在场 ⇒ 照跑；不在 ⇒ **未评估**（摘用例 + 进 skipped_ids + 退出码 3）。
#
# 为什么锚"公开语料快照"而不是另开一个探针：那**正是工具出口那一侧的同一份真相**——
# `rag.search._fetch_corpus` 与 `tools/base._get` 读的是同一个公开接口，一篇被改成 private
# 在两边同时消失。另开一个探针只会造出第二份会各自漂移的判据（同本模块头注那条）。
#
# ⚠ **"快照取不到或为空"归 unknown（照跑），不归 changed**——同语料那条纪律：
# `tools/base._get` 把连不上/5xx/坏 JSON 一律吞成 `UPSTREAM_DOWN`（不抛），"接口不通"与
# "这篇文章真的没了"在读数上长得一模一样。不知道就不动；硬判"实体没了"等于把一次网络
# 故障说成站主删了文章。它同时是离线 `--only` 那条路的安全阀（那时语料本就取不到）。
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
            # **点名这一轮真正读的那份声明**（`row["file"]`），不是默认那份常量：
            # `--golden eval/fixtures/golden_smoke.jsonl` 时顺手抄常量会把修法指到另一个
            # 文件上（20261006 实跑 `--golden` 时抓到——两个跑法都错的同一句话）。
            f"只是标题改了名 ⇒ 修 {row.get('file') or CORPUS_PROVENANCE_FILE} 的 anchors",
        ]
    if state == CORPUS_PROV_UNKNOWN:
        # 「不知道」不等于「不可用」：照跑（同 identity_preflight 那条纪律）。
        return [f"[corpus-premise] ⚠ 语料出处判不了：{detail}"
                " —— 照跑；本轮若大面积飘红，先看这一行"]
    return []
