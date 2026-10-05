# -*- coding: utf-8 -*-
""""这一轮的语料是不是这套 golden 的那一份"（20261006）。

前面四条前提闸（身份 / 事实 / 禁卡 / trace）问的都是"这一轮的环境、供给、设计还在不在"。
这一条问得更外面一层：**判据脚下这块地，还是原来那块吗**。

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
