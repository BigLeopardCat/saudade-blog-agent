#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ReAct 试验线的定点 A/B：**同一批句子、同一个模型、同一套解码参数**，只换架构。

**这是离线实验资产，不是 CI 套件的一部分**（`tests/run_all.py` 只收 `tests/*.py`，
本文件在 `eval/experiments/`）。它需要网络与 API key，且一轮要烧几十万 token ——
**按需手跑，别接进夜间**。

## 三个臂（可以只跑其中几个，见 `--arms`）

  · `planner`：现网 `agent.graph.planner_node`（**只决策不执行**）——基线；
  · `react_assets`：`create_agent` ＋**资产装满**（45 个技能级 schema ＋ 当轮真 planner
    提示词）——结构与资产的差值；
  · `react_line`：上一个 ＋ 本分支的两件（收敛判据 `ConvergenceMiddleware`、工具级回执
    `WorkflowExecutor`）——**本分支要证的东西**。

## 安全

`react_assets` / `react_line` 臂挂的全是**桩**——与真工具同名同 description 同
args_schema，函数体只返回假结果，**任何真写都不会发生**；`planner` 臂压根不跑 execute。
`react_line` 的技能展开会把计划里的**工具**再走一遍 `_check_spec`，但执行体仍是桩。

## 判据（读**计数**，不读通过率；单遍结果一律当噪声）

  · **有调用**：这一句点到的技能/工具非空；
  · **命中族**（旧口径）：点到的工具 ∩ 该句**正确**该落的工具族（族表见文件下半部）；
  · **定向命中**（20261011 加）：命中族 **且** 没踩该句的**反向工具**（族表第三格）。
    族越宽旧口径白送越多——`账户` 族里封号与解封同族、「把它号封吧」点了
    `unfreeze_account` 照样算命中，方向正好反了。两个数**并列报**，不拿一个换另一个：
    "旧口径白送了多少"本身就是要能看见的东西；
  · **收不住**：`GraphRecursionError`（自由 ReAct 的默认形态是抛异常，用户拿到的是
    一条错误而不是回答）——`react_line` 臂的目标是把它压到 0，且**不许靠预算硬砍**
    把命中率一起砍掉；
  · **台账**：`react_line` 臂另有 receipts/blocked/stop_reason（生产那套 checker 的
    输出），也是这条线唯一给得出的可审计性。

## 20261011 的三处测量修正（判据本身也会错，这一节记的就是它错在哪）

  · **工具名折算不再扫子串**：旧写法 `{n for n in TOOL_NAMES if n in json.dumps(plan)}`
    一个子串扫过去，互为子串的两个名字一起命中。实测：`account_unfreeze` 的模板里
    只有 `unfreeze_account`，旧口径却同时给出 `freeze_account`（**白送一次命中，方向
    还是反的**）。改成读结构化的 `Workflow.plan`（`list[(工具名, 参数模板)]`）＋
    `extra_plan_tools` ⇒ `account_unfreeze` 只剩一件，`article_tags` 反过来**多出**
    `create_tag`（条件工具，旧口径永远折算不出来）。
  · **两条 ReAct 臂不再共用 planner 的提示词**：此前 monkeypatch `bind_planner_tools` 抓
    planner 当轮真渲染的那份，逐字喂给 ReAct 臂——等于拿一份它们结构上满足不了的契约
    考它们，量出来的差值里掺着"提示词不匹配"。现在各拿各的惯用提示词（`_react_prompt`）。
  · **反向工具进判据**（见上"定向命中"）。

跑法（**必须在仓根跑，且 PYTHONPATH 指到本分支**——venv 的 editable `.pth` 把主仓
钉在 sys.path 上，不指就静默用到主仓那份代码）：

    cd <本分支 worktree>
    PYTHONPATH=$PWD .venv/bin/python eval/experiments/react_line_ab.py --rounds 3 \
        --arms planner,react_assets,react_line --out /tmp/react_line_ab.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓根（eval/experiments/ -> 仓根）
sys.path.insert(0, str(ROOT))

from langchain.agents import create_agent  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

import agent.graph as G  # noqa: E402
import agent.react_arm as RA  # noqa: E402
from agent.planner_llm import build_tool_schema  # noqa: E402
from agent.principal import Principal  # noqa: E402
from agent.workflows import WORKFLOWS  # noqa: E402
from models.llm import get_llm  # noqa: E402
from tools import get_all_tools  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

# ── 解码参数：三臂**必须**一致 ────────────────────────────────────────────────
# 20261004 的教训：ReAct 臂若用裸 `get_llm()`（thinking=True、temp 0.7、8192 预算）而
# planner 用 thinking=False/temp 0.2/1200，比出来的是**解码参数**不是结构——那一版
# 曾得出"ReAct 贵 17×、33% 收不住"，同解码后是 3× / 8–25%。
DECODE = {"temperature": 0.2, "max_tokens": 1200, "timeout": 60, "enable_thinking": False}

# ReAct 臂的图级保险丝。它只是**兜底**——`react_line` 臂自己会在 budget 轮收尾
# （`ConvergenceMiddleware`），撞到这里说明判据没生效。给得宽（≈8 个模型轮）是
# **故意偏向臂 C′**：它只有这一道墙，而 D 有自带的 budget。
# ⚠️ 每个中间件钩子都是一个独立超步（before_model / after_model 各一个节点），
# 所以一轮≈4 个超步而不是 2 —— 20261004 试跑时按 14 算，D 在第 4 轮就被图掐死，
# 看板上显示成"中间件没生效"，其实是被保险丝先动了手。
REC_LIMIT = 24
BUDGET = 6

# ── 期望族：这一句**正确**的机器动作应该落在哪几个工具上 ──────────────────────
ACCT = {"freeze_account", "unfreeze_account", "set_account_role", "account_mute", "account_unmute"}
MODW = {"audit_board_comment", "delete_board_comment"}
MODR = {"get_moderation_status", "list_admin_board", "list_guestbook"}
MSG = {"read_notifications", "read_messages"}
OWN = {"list_my_favorites", "get_unread_summary", "list_notifications", "list_my_messages",
       "list_dashboard_todos"}
TODO = {"complete_dashboard_todo", "create_dashboard_todo", "reschedule_dashboard_todo",
        "list_dashboard_todos"}
OPS = {"get_server_status", "get_service_health"}
NAV = {"navigate_to"}

SENTS = [
    ("牛牛冻结账号给日程4标记为未完成", ACCT | TODO, {"unfreeze_account",
                                                  "complete_dashboard_todo"}),
    ("把它号封吧", ACCT, {"unfreeze_account"}),
    ("给他驳回请求", MODW, {"delete_board_comment"}),
    ("两个都通过吧", MODW, {"delete_board_comment"}),
    ("猫咪我的未读信息全部就标记为已读", MSG, set()),
    ("猫咪带我去你的设计文档", NAV, set()),
    ("小猫咪我都有哪些收藏", OWN, {"add_favorite", "remove_favorite"}),
    ("还有什么待办吗", OWN, {"create_dashboard_todo", "complete_dashboard_todo",
                          "reschedule_dashboard_todo"}),
    ("小猫咪！我有哪些未读通知呀", OWN, MSG),
    ("今天天气怎么样", {"get_weather"}, set()),
    ("小猫咪现在生产环境状态怎么样", OPS, set()),
    ("留言板那边还等着我点头的几件，你怎么看？", MODR, MODW),
    ("帮我看看后台有没有等着审核的留言", MODR, MODW),
    ("把那条写着「泠月喵好笨啊」的留言删掉吧", MODW, {"audit_board_comment"}),
    ("给小猫咪我读一下这篇文章", {"get_article_detail"}, set()),
    ("看看最新的几篇笔记的标题", {"list_notes", "get_top_notes", "search_notes"}, set()),
]
# 第三格是**反向工具**（20261011）：这一句踩了就是**做反了**。旧判据只问
# "点到的工具 ∩ 族 非空吗"，而族越宽白送越多——`ACCT` 里封号与解封同族、`MODW` 里
# 驳回与删除同族 ⇒「把它号封吧」点了 `unfreeze_account` 照样算命中（方向刚好相反），
# 「留言删掉吧」点了 `audit_board_comment` 也算。**只给真有反向对的句子挂**，其余的
# 留空——空集是"这句话没有方向可言"，不是"没查过"。
# 这不是判决：真正的行为级判据在 golden（`check_gold` 的 197 轮断言），这一份只是定点
# 探针，它给的数只用来**看方向错没错**，不用来判谁赢。

TOOL_NAMES = {t.name for t in get_all_tools()}


def _tools_of_workflow(s: object) -> set[str]:
    """技能被展开后**真正落到哪些工具**——从结构化模板取，不扫字符串。

    旧写法是 `{n for n in TOOL_NAMES if n in json.dumps(s.plan)}`：一个子串扫过去，
    互为子串的两个工具名一起命中。**实测复现过**（20261011）：`account_unfreeze` 的模板
    里只出现 `unfreeze_account`，那份旧代码却同时返回
    `{'freeze_account', 'unfreeze_account'}` —— 白送一次命中，方向还是反的。
    `Workflow.plan` 本来就是 `list[tuple[工具名, 参数模板]]`，取 `[0]` 即可；
    `extra_plan_tools`（条件工具，如 `article_tags` 的 `create_tag`）也算数，
    否则那一支永远折算不出来。
    """
    names = {str(t[0]) for t in (getattr(s, "plan", None) or []) if t}
    names |= {str(n) for n in (getattr(s, "extra_plan_tools", None) or ())}
    return names & TOOL_NAMES


# 技能 → 工具名（技能被展开后"真正落到哪些工具"由此折算，三臂才可比：
# `react_assets` 只看得见技能名，`planner` 看得见计划文本，`react_line` 看得见台账
# ——三种口径统一折算成工具名再比）
_WORKFLOW_TOOLS: dict[str, set[str]] = {s.name: _tools_of_workflow(s) for s in WORKFLOWS}


def _tools_in_args(args: object) -> set[str]:
    """调用参数里**恰好等于**某个工具名的字面值（按值精确比对，不扫子串）。

    这一格不能整段删：数据工具点名族（`content_query` 的 `tools`/`calls`）**确实**把
    工具名当参数值传（"留言板里有没有聊过 X"要成对点名 `list_guestbook` 与 `list_talks`）。
    但旧写法是 `n in json.dumps(args)`——与上面 `_WORKFLOW_TOOLS` 同一个病：参数里出现
    `unfreeze_account` 会让 `freeze_account` 一起命中。字符串按逗号/空白切开逐段比对，
    既是"点名"的原意，也不会再互相串味。
    """
    out: set[str] = set()

    def walk(v: object) -> None:
        if isinstance(v, str):
            out.update(p for p in re.split(r"[,\s]+", v) if p in TOOL_NAMES)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                walk(x)

    walk(args)
    return out

PRINCIPAL = Principal(uid=7, role="admin")
CFG = {"configurable": {"principal": PRINCIPAL,
                        "user_id": 7, "conversation_id": 4242, "stop_event": None}}


# ── 桩 ────────────────────────────────────────────────────────────────────────
def _stub_text(name: str) -> str:
    """桩载荷。**不许太贫**：只回一句"已执行"会让模型看不到内容而反复重调，
    把撞上限率从 15% 虚增到 33%（20261004 实测）。"""
    if name.startswith(("list_", "search_", "get_", "rag_")):
        return (f"「{name}」返回（离线桩，示例数据）：\n"
                "- 示例条目一：这是一条用于占位的说明文字\n"
                "- 示例条目二：第二条占位说明\n"
                "- 示例条目三：第三条占位说明\n"
                "（以上为离线桩数据，不是真实站内内容）")
    return f"「{name}」操作成功（离线桩，未产生真实写操作）。"


def raw_stubs() -> dict:
    """65 个裸工具 → 同名可调用体（工具级回执那条路要用）。

    ⚠️ 必须吃 `**kwargs`：`WorkflowExecutor` 是 `fn(**args)` 调的，签名收不下就会
    抛 TypeError —— 而那是**被包装层兜成错误帧**的（不会炸图），于是症状是
    "每个工具都 BLOCK error_frame"，看起来像判据在开火，其实是桩写错了。
    """
    return {t.name: (lambda *a, _n=t.name, **k: _stub_text(_n)) for t in get_all_tools()}


def workflow_stubs() -> list:
    """45 个**技能级** schema 的桩（臂 C′/D 的菜单：与 planner 看到的是同一份）。"""
    out = []
    for item in build_tool_schema("admin"):
        fn = (item.get("function") or item) if isinstance(item, dict) else {}
        name = fn.get("name")
        if not name:
            continue
        out.append(StructuredTool(
            name=name, description=fn.get("description") or "",
            args_schema=fn.get("parameters") or {"type": "object", "properties": {}},
            func=(lambda *a, _n=name, **k: _stub_text(_n))))
    return out


# ── 臂 A：现网 planner（只决策） ───────────────────────────────────────────────
def run_planner(msg: str, tid: str) -> dict:
    d = tempfile.mkdtemp(prefix="react_line_ab_")
    trace_mod.start_trace(tid, 7, "admin", {}, dir=d, by_day=False)
    out = {"error": "", "tools": [], "calls": 0, "text": "", "capped": False, "rounds": 0,
           "ledger": None}
    try:
        upd = G.planner_node({"messages": [HumanMessage(content=msg)], "plan_rounds": 0,
                              "executed": [], "tool_data": []}, CFG) or {}
    except Exception as e:  # noqa: BLE001
        upd = {}
        out["error"] = f"{type(e).__name__}: {e}"
    ev = trace_mod.events_of(tid)
    dec = [e for e in ev if e.get("node") == "planner" and e.get("event") == "native_decision"]
    workflows = [str(e.get("calls") or "") for e in dec]
    # ⚠️ 工具名**只能**从计划文本的 TOOLS 行取：`native_decision.calls` 里只有技能名，
    # 读族的工具全在 PARAMS 里 —— 读那格会把「我有哪些未读通知」误记成"零调用"。
    plan_txt = "".join(str(upd.get(k) or "") for k in ("plan",))
    tools = {n for n in re.findall(r"([a-z_]{4,})\s*\(", plan_txt) if n in TOOL_NAMES}
    for s in workflows:
        if s in TOOL_NAMES:
            tools.add(s)
    out.update(workflows=workflows, tools=sorted(tools), calls=len([s for s in workflows if s]),
               rounds=len(dec))
    return out


# ── 臂 B/C′/D：create_agent ───────────────────────────────────────────────────
def _react_prompt(sent: str) -> str:
    """两条 ReAct 臂的**惯用**提示词：它们跑生产时用的就是这一份。

    **20261011 改**：此前这里 monkeypatch `bind_planner_tools` 去抓 **planner 当轮真实渲染出来的
    提示词**，再**逐字喂给两条 ReAct 臂**——等于拿一份它们结构上满足不了的契约去考它们
    （那份提示词写的是"输出五行契约 / 只用技能不用工具"，而 ReAct 臂吃的是原生 tool_calls）。
    量出来的差值因此掺着"提示词不匹配"这一项，而它不是架构差。
    换成本线自己那份之后，两臂的**提示词不同源**——这是**修正**不是新偏差：每条臂该拿它
    自己最好的那一份，就像两条臂本来就该各拿各的解码参数（见 `DECODE` 的注）。
    """
    import agent.react_arm as _R
    return _R._system_prompt([HumanMessage(content=sent)], "admin", PRINCIPAL, CFG, sent)


def _collect(msgs: list, out: dict) -> None:
    calls, n_ai = [], 0
    for m in msgs:
        if isinstance(m, AIMessage):
            n_ai += 1
            for c in (m.tool_calls or []):
                calls.append(str(c.get("name") or ""))
                out["_raw"] |= _tools_in_args(c.get("args") or {})
                out["_raw"] |= _WORKFLOW_TOOLS.get(str(c.get("name") or ""), set())
    out["workflows"], out["calls"], out["rounds"] = calls, len(calls), n_ai


def _collect_line(last: dict, plans: list, out: dict) -> None:
    """真图那条臂的读数来源（与 `create_agent` 那条**故意不同源**）。

    `agent/react_arm.py` 的 `planner` 是**纯决策节点**：它不往消息表里写东西（那条
    docstring 解释了为什么——写了会让 `with_tool_call_pairs` 把工具帧认领进一条
    `tool_call_id` 对不上的合成 assistant，出站序列非法）。所以这一臂上：

      · **技能名**只在逐轮的 `plan_obj["calls"]` 里 ⇒ 得把每一格 `values` 都收一遍
        （只看最后一份会漏掉前几轮）；
      · **真工具名**只在工具帧（`ToolMessage.name`）里 ⇒ 直接收，**比 `_WORKFLOW_TOOLS`
        折算更准**（那张表是给"只看得到技能名"的 `create_agent` 臂用的）。

    两处都收，`tools` 才是"这一句真的调了哪些工具"。
    """
    out["_raw"] |= {str(getattr(m, "name", "") or "")
                    for m in (last.get("messages") or []) if isinstance(m, ToolMessage)}
    out["_raw"].discard("")
    for po in plans:
        for c in (po.get("calls") or []):
            name = str(c.get("workflow") or "")
            out["workflows"].append(name)
            out["_raw"] |= _tools_in_args(c.get("params") or {})
            out["_raw"] |= _WORKFLOW_TOOLS.get(name, set())
    out["calls"] = len([s for s in out["workflows"] if s])
    out["rounds"] = len(plans)


def run_react(sent: str, sys_prompt: str, *, line: bool) -> dict:
    """`line=True` ⇒ 走本分支（**真图**：planner/execute/model/gate 四节点）；否则是臂 C′
    （裸 `create_agent`）。

    **20261016 改**：本分支此前是 `create_agent` + `ConvergenceMiddleware` +
    `wrap_tools_with_receipts`。那三件整体退场了（`docs/adr/adr-0009-*.md`），臂现在是
    `agent/react_arm.build()` 编出来的一张真图 ⇒ 这里换入口。两件事照旧：

      · **同解码**（`DECODE`）：决策与 narrator 都换成探针这一份 `llm`——两臂比的必须是
        循环结构，不是解码参数（`DECODE` 的注记着 20261004 那次假读数）；
      · **一次运行一本账**：`make_ctx` 是 `react_ctx` 的唯一构造点，从 `configurable`
        注入（`ReactGoldenArm.stream` 认这一格；生产与单测都不传，各自现建一份）。
    """
    out = {"error": "", "workflows": [], "tools": [], "calls": 0, "text": "", "capped": False,
           "rounds": 0, "ledger": None, "_raw": set()}
    llm = get_llm(**DECODE)
    stubs = WORKFLOW_STUBS
    plans: list = []
    if line:
        RA._llm = lambda: llm              # noqa: E731 —— 同解码（见 docstring）
        RA._narrator_llm = lambda: llm     # noqa: E731
        agent = RA.build()
        st0 = G.graph_input([HumanMessage(content=sent)])
        ctx = RA.make_ctx(config=CFG, principal=PRINCIPAL, role="admin", user_msg=sent,
                          state=st0, real_tools=RAW_STUBS)
        ledger_holder["led"] = ctx["ledger"]
        cfg = {**CFG, "configurable": {**CFG["configurable"], "react_ctx": ctx},
               "recursion_limit": REC_LIMIT}
    else:
        agent = create_agent(model=llm, tools=stubs, system_prompt=sys_prompt,
                             name="ab_react_assets")
        cfg = {"recursion_limit": REC_LIMIT}
    last: dict = {}
    try:
        for st in agent.stream({"messages": [HumanMessage(content=sent)]}, cfg,
                               stream_mode="values"):
            last = st
            if line and st.get("plan_obj"):
                plans.append(st["plan_obj"])
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        out["capped"] = "Recursion" in type(e).__name__
    msgs = last.get("messages") or []
    if line:
        _collect_line(last, plans, out)
    else:
        _collect(msgs, out)
    out["tools"] = sorted(out["_raw"])
    out["text"] = str(getattr(msgs[-1], "content", ""))[:400] if msgs else ""
    if line:
        led = ledger_holder["led"]
        out["ledger"] = {"stop": led.stop_reason, "receipts": len(led.receipts),
                         "row_receipts": led.receipts, "blocked": led.blocked,
                         "wrap_up": led.wrap_up[:200]}
    out.pop("_raw", None)
    return out


WORKFLOW_STUBS: list = []
RAW_STUBS: dict = {}
ledger_holder: dict = {}


def score(r: dict, expected: set, must_not: set | None = None) -> dict:
    """逐句读数。

    `matched` 与 `matched_strict` **并列报**（不是用一个换掉另一个）：前者是旧口径
    （"落进族里了"），后者另加"没踩反向集"。两个数并排，读的人自己看族级口径白送了多少
    ——直接换掉会让本轮读数与 20261004 那份对不上，而"对不上"正是要能看见的东西。
    """
    tools, wrong = set(r["tools"]), set(r["tools"]) & set(must_not or ())
    hit = sorted(tools & expected)
    return {"called_any": r["calls"] > 0,
            "matched": bool(hit),
            "matched_strict": bool(hit) and not wrong,
            "hit": hit,
            "wrong_direction": sorted(wrong)}


def _frac(recs: list, key: str) -> str:
    """`x/y` 计数（**不写通过率**：单遍结果一律当噪声，同仓既有纪律）。"""
    return f"{sum(1 for x in recs if x[key])}/{len(recs)}"


def _ledger_counts(recs: list) -> dict:
    """受阻原因分布 + 停止原因分布（`react_line` 臂的台账才有的那两格）。"""
    bb: dict[str, int] = {}
    stops: dict[str, int] = {}
    for x in recs:
        for b in ((x.get("ledger") or {}).get("blocked") or []):
            k = b.get("reason", "?")
            bb[k] = bb.get(k, 0) + 1
        s = (x.get("ledger") or {}).get("stop") or ""
        stops[s or "自然收尾"] = stops.get(s or "自然收尾", 0) + 1
    return {"blocked": bb, "stops": stops}


def _summarize(arm: str, rnd: int, recs: list, secs: float) -> dict:
    """一轮 × 一条臂的汇总，并打印那一行。

    「回执」要分两种：`tool` 非空 = 真执行过某个工具；`tool` 为空 = 纯应答技能（chat）
    留的那条。混在一起会读成"这一轮办成了 6 件"。
    """
    row = {"called_any": _frac(recs, "called_any"),
           "matched": _frac(recs, "matched"),
           "matched_strict": _frac(recs, "matched_strict"),
           "wrong_direction": sum(1 for x in recs if x["wrong_direction"]),
           "capped": sum(1 for x in recs if x["capped"]),
           "llm_rounds": sum(int(x.get("rounds") or 0) for x in recs),
           "receipts": sum(sum(1 for r in (x["ledger"] or {}).get("row_receipts", [])
                               if r.get("tool")) for x in recs if x.get("ledger")),
           "secs": round(secs, 1), "rows": recs, **_ledger_counts(recs)}
    print(f"== {arm} r{rnd}: 有调用 {row['called_any']}  命中族 {row['matched']}"
          f"  定向命中 {row['matched_strict']}  反向 {row['wrong_direction']}"
          f"  撞上限 {row['capped']}  模型轮数 {row['llm_rounds']}"
          f"  回执 {row['receipts']}  受阻 {row['blocked']}"
          f"  停止 {row['stops']}  用时 {row['secs']}s", flush=True)
    return row


def _one(arm: str, rnd: int, sent: str, opp: set, exp: set, i: int,
         prompts: dict) -> dict:
    """跑一句并打分。`planner` 臂与两条 ReAct 臂的入口不同，此处是**唯一**的分叉点。"""
    if arm == "planner":
        rr = run_planner(sent, f"rl_{arm}_{rnd}_{i}")
    else:
        ledger_holder["led"] = None
        rr = run_react(sent, prompts[sent], line=(arm == "react_line"))
    rr["sent"] = sent
    rr.update(score(rr, exp, opp))
    note = ""
    if rr.get("ledger"):
        note = (f" stop={rr['ledger']['stop']} 回执={rr['ledger']['receipts']}"
                f" 受阻={len(rr['ledger']['blocked'])}")
    print(f"[{arm} r{rnd} {i}] 工具={rr['tools']} 命中={rr['hit']}"
          f"{' 反方向=' + str(rr['wrong_direction']) if rr['wrong_direction'] else ''}"
          f"{' CAPPED' if rr['capped'] else ''}{note} {sent[:16]}", flush=True)
    return rr


def _print_paired(R: dict, arms: list) -> None:
    """逐轮配对表：**读计数，不读通过率**。

    两臂在同一轮里交替跑（`order` 每轮反向），所以 (round, arm) 这一列就是配对坐标；
    报告里只放计数，谁高谁低留给读的人——这份文件是定点探针，不是判决。
    """
    print("\n=== 逐轮配对（round, arm, 定向命中, 反向, 撞上限, 模型轮数, 用时s）===")
    for arm in arms:
        print(" ", arm, [(x["round"], x["arms"][arm]["matched_strict"],
                          x["arms"][arm]["wrong_direction"], x["arms"][arm]["capped"],
                          x["arms"][arm]["llm_rounds"], x["arms"][arm]["secs"])
                         for x in R["rounds"]])
    print("  ⚠️ 两条 ReAct 臂的提示词与 planner 那份**不同源**（各拿各的惯用提示词，"
          "见 `_react_prompt`）；命中族列是旧口径，定向命中列才是本轮新加的判据。")


def _render_prompts(sents: list) -> dict:
    """两条 ReAct 臂的惯用提示词（与 `planner` 臂无关 ⇒ 只在需要时才渲染）。"""
    print(f"渲染两条 ReAct 臂的惯用提示词（{len(sents)} 句）…", flush=True)
    out = {}
    for i, (sent, _e, _o) in enumerate(sents, 1):
        out[sent] = _react_prompt(sent)
        print(f"  [prompt {i}/{len(sents)}] {len(out[sent])} 字  {sent[:16]}", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--arms", default="planner,react_assets,react_line")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 句（试跑用）")
    ap.add_argument("--out", default="/tmp/react_line_ab.json")
    a = ap.parse_args()
    global WORKFLOW_STUBS, RAW_STUBS
    WORKFLOW_STUBS, RAW_STUBS = workflow_stubs(), raw_stubs()
    sents = SENTS[:a.limit] if a.limit else SENTS
    arms = [s for s in a.arms.split(",") if s]
    prompts = _render_prompts(sents) if any(x != "planner" for x in arms) else {}

    R: dict = {"arms": arms, "decode": DECODE, "rec_limit": REC_LIMIT, "budget": BUDGET,
               "rounds": []}
    for r in range(1, a.rounds + 1):
        order = arms if r % 2 else list(reversed(arms))   # 两臂交替，抵消端点漂移
        row = {"round": r, "arms": {}}
        for arm in order:
            t0, recs = time.time(), []
            for i, (sent, exp, opp) in enumerate(sents, 1):
                recs.append(_one(arm, r, sent, opp, exp, i, prompts))
            row["arms"][arm] = _summarize(arm, r, recs, time.time() - t0)
        R["rounds"].append(row)
        Path(a.out).write_text(json.dumps(R, ensure_ascii=False, indent=1))

    _print_paired(R, arms)
    return 0


if __name__ == "__main__":
    sys.exit(main())
