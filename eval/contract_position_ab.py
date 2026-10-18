#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""契约位置 A/B（20261017 重验「描述归 schema、契约归提示词正文」，四臂）。

**只读实验**：只发 LLM 请求，不碰数据库、不写 trace、不改任何生产状态。需要网络与
API key，**因此不入 L0 套件、不进夜间**（`tests/run_all.py` 只收 tests/*.py，本文件在 eval/）。

## 为什么重验

`docs/adr/adr-0003` 记的原始 A/B 是 `rag_talk_rag` 完整菜单 **5/5 绿** → slim **0/5 红**。
主人对那条结论的测试方式存疑。复盘出四条硬伤，其中**决定性的一条**是信息丢失而非位置：

  当时 slim 臂里 `content_query` 的 schema 描述**原样漏着未展开的占位标记**
  `__无参只读工具清单__`（`render_tool_marks` 当时只走散文那一路），而 `tools` 参数
  描述被 `planner_llm._SCHEMA_OVERRIDES[("content_query","tools")]` 覆盖成
  「无参只读数据工具的点名列表（闭集由本轮角色决定）」——**没有工具名**。
  ⇒ 两个工具名在 schema 里**两条路都没出现**。0/5 完全可以用「信息丢失」解释，
  不必用「位置」解释。那处标记今天已修（`planner_llm.build_tool_schema` 里
  `render_tool_marks(workflow.description, role)`）。

## 四臂（**只动契约那一句的位置**，散文菜单其余部分逐字节相同）

  ④ merged  产线原样（契约与描述同住 `description`）           **基线 = 已发出去的那个形状**
  ① prose   描述里那段摘掉，同一句回填进本轮提示词正文的 `  契约：` 行（= 改之前的形态）
  ② schema  同上，但追加在 `tools[].description` **末尾**（带 `契约：` 标记行）——上一轮
            测过的近似物，留在这里是为了不让"就差了三个字的标记"替代一次实测
  ③ none    整句不出现                                         **正控**：证明这句话本身承重

**决策已定（20261017）**：③ 掉、①②④ 逐条相同 ⇒ **位置无关、那句话本身承重**。所以
`planner_contract` 字段撤掉、并回 `description`，④ 从此就是产线形状（本文件对它
**一个字都不改**）。本文件因此从"待裁决的实验"变成**回归探针**：再有人把契约搬走时，
①/② 还能量出搬动有没有代价。

④ 与 ② 的差别现在只剩两处：④ 的契约在 `description` 里、且排在 `完成判定：` **之前**
（那是 `build_tool_schema` 的拼接顺序）；② 排在末尾、还多三个字的标记。两次都测，
是因为这次要动的正是产线提示词——**"只差一点"不是一次实测**。

## 观测层 = **planner 原始输出**（绕开图）

判据是 planner 这一轮**点出来的工具清单**，不是端到端通过率。这一层是刻意的：

  · 不变量问的是「planner 看得见什么、照着做了什么」——那是 planner 层的事实；
  · 图里还有确定性兜底会**掩掉**原始漏项（`graph._pair_dual_sources` 对
    content_query 的成对点名做确定性补齐，它自己的正控用的就是本文件用例 ① 的原话）
    ⇒ 端到端跑两臂今天都会是绿的，量不出任何差别。兜底仍在原位、未改；
    本文件量的是**它上面那一层**。

## 用例

`content_query` 是**唯一**一条契约内容不在 schema 里的候选，它那份契约有**两半**，
原 ADR 的机制（一）（二）正对应这两半，因此各有用例：

  · 「涉站内容必须先取回真实数据再作答」——失败形态：技能选对、**零检索工具**、
    拿通用知识直接答 ⇒ `route_git_svn` / `route_ota`；
  · 「必须**成对**点名两个数据源」——失败形态：技能选对、第一个源调了、
    **漏掉第二个** ⇒ `pair_one_guestbook` / `pair_one_talks`。

⚠️ 两半在提示词正文的**规划规则 3** 里都有第二份，而那份**三臂都在**（本探针只摘
`  契约：` 行）⇒ 量到的是"技能旁边那一句在规则之外还加了多少"，与原 ADR 的问题同形
（它自己记着"规则 3 那份不够"）。

**用例必须避开"用户自己已经把答案说了"的形状**：首版三条（「留言板**和**说说」之类）
主人那句话里**两个数据源都点了名** ⇒ 契约加不了任何东西，三臂必然全绿。契约管的形状是
主人**只点一个**源（`pair_one_guestbook` / `pair_one_talks` 两条），要求模型把另一半也查了。

另外三条计划点名的候选（`admin_notes` 分类名、`tag_create` 一次多个名字、
`article_status` 一次多篇）**实测没有分辨力**：它们的契约实质**逐条复写**进了同名参数的
`inputs` 描述（schema 的 `properties`），而那份描述**三臂都在**
（如 `article_status.inputs.article_ids` 自己就写着「一次改好几篇时的文章 id 数组」）
⇒ ③ 臂并没有真的把信息拿走。如实记在这里，别再拿它们做这一问的用例。

## 这个探针自己栽过的两次「量错了」（都长得像"模型不行"）

1. **首版用裸 `openai` 客户端发请求，把 OpenAI 的响应对象喂给 `tool_calls_to_plan`** ——
   那个解析器只认 langchain 的 `AIMessage`，判不了就返回 `None` ⇒ `workflow` 全空，
   屏幕上一片 ❌（读起来正是"模型不点工具"）。而 `finish_reason=tool_calls` 明摆着工具调过。
   ⇒ 改走 `get_llm(...).bind_tools(...)`（与 `bind_planner_tools` 同路），并加一道闸：
   `workflow` 空的**不算样本**，全空则退出码 2「先修探针」而不是发判定。
2. **判据只认一种形状**：`article_status.article_ids` 模型交的是 `"[22, 23]"`（字符串），
   生产用 `_pos_int_list` **两类实参都收**（生产 trace `20261015T080030` 就是这么记的），
   而首版判据只认数组 ⇒ 三臂 36/36 全红。⇒ 判据改调生产那个解析器，并把**原始参数**
   落进每条记录（不存原样的话，"模型没填对"与"判据太窄"在报告里长得一模一样）。

## 跑法（cd saudade-blog-agent，需网络）

  .venv/bin/python eval/contract_position_ab.py --dry-run     # 只打四臂的差异，不发请求
  .venv/bin/python eval/contract_position_ab.py --trials 12   # 每臂每句 12 遍，四臂交替
  .venv/bin/python eval/contract_position_ab.py --trials 12 --json

报告落 `eval/report/contract_position_<ts>.md` + 同名 `.json`（`.json` 的 `arms` 形状可直接
喂 `eval/landing_gate.ab_compare`）。**本脚本不改任何文档**，结论由人手写进
`docs/adr/adr-0003` 的补记与 `tests/test_slim_workflows.py` 的 ②b 头注。

⚠️ **生产 planner 温度是 0.0**（`settings.planner_temperature`，20261006 起）⇒ 同一句
同一臂的多遍**多半逐字节相同**，n 只反映服务端抖动、不是模型方差。所以：判据读**计数**
不读 p 值；`ab_compare` 在本实验上会判 `underpowered`（它要每臂 ≥35 条**独立**采样），
那是**如实**的——别把它的 `no_regression` 当成本实验的结论。
"""
import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime

from langchain_core.messages import HumanMessage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.graph as G  # noqa: E402
from agent.arg_refs import ref_hints  # noqa: E402
from agent.context import _doc_anchors, _frame_texts, _recent_tail, _short_reply_hint  # noqa: E402
from agent.planner_llm import build_tool_schema, tool_calls_to_plan  # noqa: E402
from agent.workflow_defs import WORKFLOW_MAP, visible_workflows  # noqa: E402
from agent.workflow_expand import _pos_int_list  # noqa: E402
from agent.workflow_menu import build_planner_context  # noqa: E402
from config.settings import settings  # noqa: E402
from models.llm import get_llm  # noqa: E402

REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report")

ARMS = ("prose", "schema", "merged", "none")
ARM_DESC = {"merged": "④ 并进 description（产线原样）",
            "prose": "① 回填正文（改之前的形态）",
            "schema": "② 追加在 description 末尾（带标记）",
            "none": "③ 整句删掉（正控）"}

# 契约行在 `build_planner_context(slim=True)` 里的形状（① 臂把它回填进正文时用；
# 20261017 之后产线正文里**已经没有**这一行了——契约与描述同住 schema）
_CONTRACT_PREFIX = "  契约："

# 成对点名的两个数据源（与 `graph._DUAL_SOURCE_PAIR` 同值；本文件**不 import 它**——
# 那个常量住在图里，探针绕开图，抄一份并在这里钉住：两处不同名就说明契约被改过）
PAIR = ("list_guestbook", "list_talks")


def _all_tools(dec) -> set[str]:
    """这一轮点出来的工具全集：`tools` 点名的 + `calls` 带参调的。"""
    out: set[str] = set()
    if dec is None:
        return out
    p = dec.params or {}
    out |= {t.strip() for t in (p.get("tools") or []) if isinstance(t, str)}
    for c in (p.get("calls") or []):
        if isinstance(c, dict) and c.get("tool"):
            out.add(str(c["tool"]).strip())
    return out


def _need_pair(dec):
    got = _all_tools(dec)
    miss = [t for t in PAIR if t not in got]
    return (not miss), sorted(got), miss


# 检索族（原 ADR 机制（二）的失败形态 = 技能选对、**零检索工具**、拿通用知识直接答）
RETRIEVAL_TOOLS = ("search_notes", "rag_search", "get_article_detail", "list_notes")


def _need_retrieval(dec):
    """判据 = 选了 `content_query`，且**至少点了一个检索工具**。

    这条量的是契约的另一半（「涉站内容必须先取回真实数据再作答」）。**注意它测的是
    "技能旁边那一句在规划规则之外还加了多少"**：同一件事在提示词正文的规划规则 3 里
    本来就有第二份，而那一份**三臂都在**（本探针只摘 `  契约：` 行）——原 ADR 自己
    记着"规则 3 那份不够"。
    """
    if dec is None:
        return False, [], ["(不可解析)"]
    if dec.workflow != "content_query":
        return False, [], [f"选了 {dec.workflow}"]
    got = _all_tools(dec)
    hit = [t for t in RETRIEVAL_TOOLS if t in got]
    return bool(hit), sorted(got), ([] if hit else ["零检索工具"])


def _need_workflow(name, key, val=None, *, ids=False):
    """判据 = 选了 `name`，且 `params[key]` 覆盖 `val`（`val=None` 时只判选了谁）。

    `ids=True`（只有 `article_status.article_ids` 用）⇒ 走**生产那个解析器**
    （`workflows._pos_int_list`）：它两类实参都收（真数组，或**一段写着清单的文本**）。
    首版判据只认数组 ⇒ 模型交 `"[22, 23]"`（生产 trace `20261015T080030` 实证的常见
    形状、生产**认**它）时判 ❌，于是 `article_status` 三臂 36/36 全红——**量的是我的
    判据，不是模型**。同族教训：「判据的落点要选判据别选字面」。

    ⚠️ 这条修法**只能挂在 id 那几格上**：`_pos_int_list` 把非数字片段一律判"认不出"，
    拿它去判 `tag_create.titles`（"Git"/"SVN"）或 `admin_notes.category`（"编程"）
    会把**本来就对的**答案判红（本轮实测：三臂从 12/36 一起掉到 0/36，而参数逐字没变）
    ——同一个"判据太宽"的病，换了个方向再犯一次。
    """
    def check(dec):
        if dec is None:
            return False, [], ["(不可解析)"]
        p = dec.params or {}
        got = _all_tools(dec)
        if dec.workflow != name:
            return False, sorted(got) or [dec.workflow], [f"选了 {dec.workflow}"]
        if val is None:
            return True, sorted(got), []
        if ids:
            cur, bad = _pos_int_list(p.get(key))
            miss = [v for v in val if v not in cur]
            if bad:                   # 「认不出的片段」与「没给」不是同一件事
                miss = miss + [f"认不出{bad}"]
        else:
            raw = p.get(key)
            cur = list(raw) if isinstance(raw, list) else ([] if raw in (None, "") else [raw])
            miss = [v for v in val if v not in cur]
        return (not miss), sorted(got), miss
    return check


# 公开身份用例：`(key, 主人那句话, 为什么是它, 判据)`
#
# ⚠️ 前三条的**设计缺陷**（20261017 首轮跑完才看出来，如实记在这里）：主人那句话**自己就把
# 两个数据源都点了名**（「留言板**和**说说」「说说**或者**留言」「碎语**和**河灯留言」）
# ⇒ 成对点名所需的词**已经在用户消息里**，契约那一句加不了任何东西。首轮三臂全 36/36、
# 每臂只有 1 种输出，正是这个原因——**不是**"契约没用功"，是这三条**在构造上就分辨不了**。
# 契约管的形状是后两条：主人**只点一个**数据源（或一个都不点），要求模型把另一半也查了。
CASES_PUBLIC = [
    # 机制（二）：知识型问题，消息里**没有任何"去查"的提示**（原 ADR 的 `rag_git_svn` 形状）
    ("route_git_svn", "Git 和 SVN 有什么区别",
     "知识型问题、零检索提示——契约（一）管的就是这个（原 ADR 5/9 vs 9/9 那条）",
     _need_retrieval),
    ("route_ota", "ESP32 的 OTA 是怎么配置的",
     "同上，换个领域", _need_retrieval),
    # 机制（一）：成对点名——主人**只点一个**数据源时，契约要求把另一半也查了
    ("pair_one_guestbook", "留言板里有人提过 OTA 吗",
     "**只点了一个**数据源（留言板）——契约管的正是这个形状", _need_pair),
    ("pair_one_talks", "有没有人写过关于 OTA 的说说",
     "只点了另一个数据源（说说）", _need_pair),
    ("pair_canonical", "留言板和说说里有没有人聊过 ESP32",
     "契约的原型句（`_pair_dual_sources` 正控用的就是它）；**两个源都在用户消息里**", _need_pair),
    ("pair_alt_word", "有没有人写过关于 RAG 的说说或者留言",
     "换词序（先说 说说、后说 留言）；两个源也都在消息里", _need_pair),
    ("pair_slang", "碎语和河灯留言里，有人提过 OTA 吗",
     "口语变体（碎语/河灯）；两个源也都在消息里", _need_pair),
]

# admin 身份用例 = **计划点名的那三条**（`admin_notes` / `tag_create` / `article_status`），
# 判据落在 planner 这一轮的**决定**上（选了谁 + 那个参数覆盖了几个目标）。
CASES_ADMIN = [
    ("tag_create_multi", "帮我建 Git 和 SVN 两个标签",
     "一次点多个名字只选一次（契约）", _need_workflow("tag_create", "titles", ("Git", "SVN"))),
    ("article_status_multi", "把 22 和 23 都设为私密",
     "一次点多篇只选一次（契约）",
     _need_workflow("article_status", "article_ids", (22, 23), ids=True)),
    ("admin_notes_category", "把「编程」这个分类下的文章都列出来",
     "分类名一字不差、不许占位词（契约）",
     _need_workflow("admin_notes", "category", ("编程",))),
]

ROLE = None
CASES = CASES_PUBLIC
ARGS = {"temperature": settings.planner_temperature,
        "max_tokens": settings.planner_native_max_tokens,
        "timeout": settings.planner_native_timeout}


def _contracts(role: str | None) -> dict[str, str]:
    """本轮可见技能里 `名字 → 契约句`（空的略掉）。"""
    out = {}
    for w in visible_workflows(role):
        c = w.contract
        if c:
            out[w.name] = c
    return out


def arm_context(arm: str, role: str | None) -> str:
    """该臂下 `build_planner_context` 的产物。

    ④ 是产线原样。① 要把契约句**回填**进正文——回填位置与 20261017 之前那份代码一致
    （紧跟该技能的 `- 名字：…` 那一行），否则①就不是"改之前的形态"了。
    """
    ctx = build_planner_context(role, slim=True)
    if arm != "prose":
        return ctx
    contracts = _contracts(role)
    lines = ctx.splitlines()
    out = []
    for ln in lines:
        out.append(ln)
        for name, c in contracts.items():
            if ln.startswith(f"- {name}："):
                out.append(f"{_CONTRACT_PREFIX}{c}")
                break
    return "\n".join(out)


def arm_tools(arm: str, role: str | None) -> list[dict]:
    """该臂下 `tools` 数组。

    ④ **一个字都不改**（`build_tool_schema` 的产物就是产线要发的那个形状——契约已经在
    `description` 里）。①/②/③ 都要先把那段摘出来：① 摘掉后回填正文、② 摘掉后追加到
    `description` 末尾（带 `契约：` 标记）、③ 摘掉就是摘掉。
    """
    tools = copy.deepcopy(build_tool_schema(role))
    if arm == "merged":
        return tools
    by_name = _contracts(role)
    for t in tools:
        fn = t["function"]
        c = by_name.get(fn["name"])
        if not c:
            continue
        d = fn["description"]
        assert d.count("\n" + c) == 1, f"{fn['name']} 的描述里找不到那一段契约"
        fn["description"] = d.replace("\n" + c, "", 1)
        if arm == "schema":
            fn["description"] = fn["description"] + "\n契约：" + c
    return tools


def render(arm: str, user_msg: str, role: str | None) -> str:
    """渲染**线上真正会发的那份** planner 提示词，只把技能块换成该臂的形态。

    `_render_planner_prompt` 内部直接调 `build_planner_context` ⇒ 这里按模块全局打桩
    （`agent.graph` 的名字空间，`from ... import` 过的名字也在那儿）。桩只活在这一句里。
    """
    msgs = [HumanMessage(content=user_msg)]
    orig = G.build_planner_context
    G.build_planner_context = lambda r=None, *, slim=False, deny=None: arm_context(arm, r)  # noqa: E731
    try:
        return G._render_planner_prompt(
            role=role,
            page_ctx="current_url=/\ncurrent_effects=（无）\ncurrent_darkmode=off",
            round_info="当前决策：第 1/4 轮。本轮尚无工具执行，是首轮决策。",
            user_msg=user_msg,
            intent_hints=G._intent_hints([], user_msg),
            doc_anchors=_doc_anchors(msgs),
            recent_context=_recent_tail(msgs),
            short_reply_hint=_short_reply_hint(msgs),
            pending_ledger="（本轮没有去读待办台账）",
            tool_results=_frame_texts([]),
            ref_hints=ref_hints([]),
            reflector_feedback="（本决策轮无复盘建议）",
            correction="（本决策轮无纠偏提示）")
    finally:
        G.build_planner_context = orig


def planner_llm(arm: str):
    """绑上**该臂**的 schema 的 planner 客户端。

    走 `langchain` 而不是裸 `openai`：`planner_node` 拿到的是 `AIMessage`，而
    `tool_calls_to_plan` 只认它的形状。首版用裸 client 发、再把 OpenAI 的响应对象喂给
    那个解析器 ⇒ **每一次都返回 None**（`tool_calls_to_plan` 判不了就返回 None），
    读数因此全是"零调用"——看着像"模型不点工具"，其实是解析器拿到的不是它认的对象。
    `tool_choice` / `parallel_tool_calls` 与 `bind_planner_tools` 逐字同值。
    """
    llm = get_llm(temperature=ARGS["temperature"],
                  max_tokens=settings.planner_native_max_tokens,
                  timeout=settings.planner_native_timeout,
                  enable_thinking=settings.planner_native_thinking)
    return llm.bind_tools(arm_tools(arm, ROLE), tool_choice="auto",
                          parallel_tool_calls=False)


def once(llm, arm: str, key: str, user_msg: str, check) -> dict:
    """发一次请求，回一行结构化记录。**异常也记成一行**（不中断整轮）。"""
    rec = {"arm": arm, "case": key, "http_ok": False, "error": "", "workflow": "",
           "named": [], "missing": [], "ok": False, "finish_reason": "",
           "undecided": False, "prompt_chars": 0, "ms": 0, "params": ""}
    full = render(arm, user_msg, ROLE)
    rec["prompt_chars"] = len(full)
    t0 = time.monotonic()
    try:
        resp = llm.invoke(full)
    except Exception as e:                      # noqa: BLE001 —— 探针要把失败也变成数据
        rec["ms"] = int((time.monotonic() - t0) * 1000)
        rec["error"] = f"{type(e).__name__}: {e}"[:300]
        return rec
    rec["ms"] = int((time.monotonic() - t0) * 1000)
    rec["http_ok"] = True
    rec["finish_reason"] = str((getattr(resp, "response_metadata", None) or {})
                               .get("finish_reason") or "")
    # 用**生产那个解析器**（`planner_node` 走的就是它），不自己数 raw tool_calls。
    dec = tool_calls_to_plan(resp, ROLE)
    rec["workflow"] = "" if dec is None else dec.workflow
    rec["undecided"] = bool(dec is not None and dec.undecided)
    # **原始参数**落进记录：判据（`_need_workflow`）只认一种形状，参数换个形状就成 ❌——
    # 不存原样的话，"模型没填对"与"我的判据太窄"在报告里长得一模一样（首轮 admin 的
    # `article_status` 36/36 全 ❌ 就是这个形状，必须能一眼看出是哪一种）。
    rec["params"] = "" if dec is None else json.dumps(dec.params or {},
                                                     ensure_ascii=False)[:300]
    rec["ok"], rec["named"], rec["missing"] = check(dec)
    return rec


def dry_run() -> int:
    """不发请求：打四臂的差异，证明"只动了契约那一句"。"""
    # ── 切法的前提（`Workflow.contract` 靠它成立；坏了要响，不能静默切错）──────
    with_nl = [w.name for w in visible_workflows(ROLE) if "\n" in w.description]
    declared = sorted(_contracts(ROLE))
    print(f"role={ROLE} 描述里含换行的技能 {len(with_nl)} 个 / 有契约的 {len(declared)} 个"
          f" —— 必须同一批：{sorted(with_nl) == declared}")
    multi = [w.name for w in visible_workflows(ROLE) if w.description.count("\n") > 1]
    print(f"含不止一段换行的技能：{multi}（必须为空——契约只接在末尾一段）")

    base = build_planner_context(ROLE, slim=True)
    prose = arm_context("prose", ROLE)
    print(f"\n正文：④ {len(base)} 字符 / ① 回填后 {len(prose)} 字符"
          f"（多 {len(prose) - len(base)}）")
    print(f"④ 正文里 `{_CONTRACT_PREFIX}` 行数 = {base.count(_CONTRACT_PREFIX)}（产线必须为 0）")
    for name, c in _contracts(ROLE).items():
        in_inputs = sum(str(v).count(c) for v in (WORKFLOW_MAP[name].inputs or {}).values())
        print(f"  · {name:<20} ① 正文 {prose.count(_CONTRACT_PREFIX + c)} 次 / "
              f"description {WORKFLOW_MAP[name].description.count(c)} 次 / inputs {in_inputs} 次")

    contracts = _contracts(ROLE)
    t4, t2, t3, t1 = (arm_tools(a, ROLE) for a in ("merged", "schema", "none", "prose"))
    desc4 = {t["function"]["name"]: t["function"]["description"] for t in t4}
    print(f"\n臂④ 就是产线 schema 原样：{t4 == build_tool_schema(ROLE)}")
    print(f"臂①/③ schema 逐字节相同（都没了那段）：{t1 == t3}")
    print(f"臂④ 的描述里都还带着契约：{all(c in desc4[n] for n, c in contracts.items())}")
    with_c = sum(1 for t in t2 if "契约：" in t["function"]["description"])
    print(f"臂② schema：{len(t2)} 个函数，其中 {with_c} 个末尾多了带标记的契约句"
          f"（应等于 {len(contracts)}）")
    # ② 与 ④ 的差别只有两处：那个标记、以及契约段排在哪（④ 在 `完成判定：` 之前，
    # ② 在末尾）。判据 = 把 ④ 的契约段挪到末尾、再去掉标记，应当逐字节等于 ②。
    fixed = copy.deepcopy(t4)
    for t in fixed:
        fn = t["function"]
        c = contracts.get(fn["name"])
        if c:
            fn["description"] = fn["description"].replace("\n" + c, "", 1) + "\n" + c
    unmapped = copy.deepcopy(t2)
    for t in unmapped:
        t["function"]["description"] = t["function"]["description"].replace("\n契约：", "\n", 1)
    print(f"臂④ 把契约段挪到末尾 == ② 去掉标记：{fixed == unmapped}")
    return 0


def _run_trials(client: dict, trials: int) -> list[dict]:
    """四臂交替跑完，逐条打一行。**外层是遍数**（本仓纪律：不并行、不各跑一段）。"""
    rows: list[dict] = []
    for i in range(trials):
        for key, msg, why, check in CASES:
            for arm in ARMS:
                rec = once(client[arm], arm, key, msg, check)
                rows.append(rec)
                print(f"  [{i + 1:>2}/{trials}] {ARM_DESC[arm]:<24} {key:<14} "
                      f"{rec['ms']:>6}ms {rec['workflow'] or '(不可解析)':<14} "
                      f"{'✅' if rec['ok'] else '❌'} 缺={rec['missing']} "
                      f"{rec['finish_reason']} {rec['error'][:60]}")
    return rows


def _guard_unparsed(rows: list[dict]) -> bool:
    """**"看着在跑、其实没跑"的闸**（回 True ⇒ 不发判定）。

    `tool_calls_to_plan` 判不了就返回 `None` ⇒ `workflow` 空。首版整个 18 条都落在这里，
    而屏幕上是清一色的 ❌（读起来像"模型不点工具"）。解析不了的一律**不算样本**，且必须
    响到脸上；全部落在这里 ⇒ 先修探针，别发判定。
    """
    unparsed = [r for r in rows if r["http_ok"] and not r["workflow"]]
    if not unparsed:
        return False
    print(f"\n⚠️ {len(unparsed)}/{len(rows)} 条响应不可解析（`workflow` 空）"
          f"——下面那些 ❌ 是**装置坏**，不是模型行为：")
    for r in unparsed[:3]:
        print(f"   {r['arm']}/{r['case']} finish={r['finish_reason']}")
    if len(unparsed) == len(rows):
        print("全部不可解析 ⇒ 不发判定，先修探针。")
        return True
    return False


def _arm_stats(rows: list[dict]) -> dict:
    """逐臂计数（**计数**，不是 p 值——见头注的温度说明）并打表。"""
    print("\n=== 逐臂合规计数（合规 = 两个数据源都点出来了）===")
    stats: dict[str, dict] = {}
    for arm in ARMS:
        rs = [r for r in rows if r["arm"] == arm]
        ok = [r for r in rs if r["ok"]]
        stats[arm] = {"n": len(rs), "ok": len(ok),
                      "rate": round(len(ok) / len(rs), 4) if rs else 0.0}
        per_case = {k: [r for r in rs if r["case"] == k] for k, *_ in CASES}
        detail = "  ".join(f"{k}={sum(1 for r in v if r['ok'])}/{len(v)}"
                           for k, v in per_case.items())
        # 有效样本量：温度 0.0 下多遍多半逐字节相同 ⇒ 报**不同输出**的条数（见头注）
        distinct = len({(r["workflow"], tuple(r["named"]), tuple(r["missing"])) for r in rs})
        stats[arm]["distinct"] = distinct
        print(f"  {ARM_DESC[arm]:<26} {len(ok):>3}/{len(rs):<3} = {stats[arm]['rate']:.2f} "
              f"（不同输出 {distinct} 种）   {detail}")
    errs = [r for r in rows if not r["http_ok"]]
    print(f"  请求失败 {len(errs)} 条" + (f"：{errs[0]['error'][:120]}" if errs else ""))
    return stats


def _write_reports(rows: list[dict], stats: dict, trials: int) -> tuple[str, str, dict]:
    """落 `.json`（形状可直接喂 `ab_compare`）与同名 `.md`；回 `(md 路径, json 路径, meta)`。"""
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    os.makedirs(REPORT_DIR, exist_ok=True)
    meta = {"ts": ts, "model": settings.active_llm_model, "provider": settings.llm_provider,
            "role": ROLE, "temperature": ARGS["temperature"],
            "thinking": bool(settings.planner_native_thinking),
            "max_tokens": ARGS["max_tokens"], "trials": trials, "arms": stats,
            "note": ("温度 0.0 ⇒ 同臂多遍多半逐字节相同，n 反映的是服务端抖动；"
                     "ab_compare 在本实验上会判 underpowered，那是如实的")}
    jpath = os.path.join(REPORT_DIR, f"contract_position_{ts}.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "rows": rows}, f, ensure_ascii=False, indent=1)

    mpath = os.path.join(REPORT_DIR, f"contract_position_{ts}.md")
    with open(mpath, "w", encoding="utf-8") as f:
        f.write(f"# 契约位置四臂 A/B {ts}\n\n")
        f.write(f"- 模型 `{meta['model']}` / provider `{meta['provider']}` / role `{ROLE}` / "
                f"温度 `{meta['temperature']}` / 思考 `{meta['thinking']}` / "
                f"max_tokens `{meta['max_tokens']}`\n")
        f.write(f"- 四臂 × {len(CASES)} 用例 × {trials} 遍，**交替**读计数\n")
        f.write("- 观测层 = planner 原始输出（绕开图；图里的 `_pair_dual_sources` "
                "会掩掉原始漏项，端到端量不出差别）\n\n")
        f.write("| 臂 | 合规 |\n|---|---|\n")
        for arm in ARMS:
            s = stats[arm]
            f.write(f"| {ARM_DESC[arm]} | {s['ok']}/{s['n']} = {s['rate']:.2f} |\n")
        f.write("\n## 逐条\n\n")
        f.write("| 臂 | 用例 | 决定 | 点出来的工具 | 原始参数 |\n|---|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['arm']} | {r['case']} | {r['workflow']} | {r['named']} | "
                    f"`{r['params']}` |\n")
    return mpath, jpath, meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=12, help="每臂每句多少遍（四臂交替）")
    ap.add_argument("--set", choices=("public", "admin"), default="public",
                    help="用例集：public=content_query 成对点名；admin=计划点名的那三条")
    ap.add_argument("--dry-run", action="store_true", help="只打四臂差异，不发请求")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    global ROLE, CASES
    ROLE = "admin" if args.set == "admin" else None
    CASES = CASES_ADMIN if args.set == "admin" else CASES_PUBLIC
    if args.dry_run:
        return dry_run()

    print(f"模型 {settings.active_llm_model} | provider {settings.llm_provider} | role {ROLE} | "
          f"温度 {ARGS['temperature']} | 思考 {settings.planner_native_thinking} | "
          f"max_tokens {ARGS['max_tokens']}")
    print(f"用例集 {args.set} | 四臂 × 用例 {len(CASES)} × {args.trials} 遍"
          f"（**交替**：每轮换一臂）\n")

    rows = _run_trials({arm: planner_llm(arm) for arm in ARMS}, args.trials)
    if _guard_unparsed(rows):
        return 2
    mpath, jpath, meta = _write_reports(rows, _arm_stats(rows), args.trials)
    print(f"\n报告：{mpath}\n      {jpath}")
    if args.json:
        print(json.dumps(meta, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
