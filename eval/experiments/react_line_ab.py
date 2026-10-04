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
    `SkillExecutor`）——**本分支要证的东西**。

## 安全

`react_assets` / `react_line` 臂挂的全是**桩**——与真工具同名同 description 同
args_schema，函数体只返回假结果，**任何真写都不会发生**；`planner` 臂压根不跑 execute。
`react_line` 的技能展开会把计划里的**工具**再走一遍 `_check_spec`，但执行体仍是桩。

## 判据（读**计数**，不读通过率；单遍结果一律当噪声）

  · **有调用**：这一句点到的技能/工具非空；
  · **命中族**：点到的工具 ∩ 该句**正确**该落的工具族（族表见文件下半部）；
  · **收不住**：`GraphRecursionError`（自由 ReAct 的默认形态是抛异常，用户拿到的是
    一条错误而不是回答）——`react_line` 臂的目标是把它压到 0，且**不许靠预算硬砍**
    把命中率一起砍掉；
  · **台账**：`react_line` 臂另有 receipts/blocked/stop_reason（生产那套 checker 的
    输出），也是这条线唯一给得出的可审计性。

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
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.native_plan import build_tool_schema  # noqa: E402
from agent.principal import Principal  # noqa: E402
from agent.react_line import RunLedger, SkillExecutor, build_agent  # noqa: E402
from agent.skills import SKILLS  # noqa: E402
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
    ("牛牛冻结账号给日程4标记为未完成", ACCT | TODO),
    ("把它号封吧", ACCT),
    ("给他驳回请求", MODW),
    ("两个都通过吧", MODW),
    ("猫咪我的未读信息全部就标记为已读", MSG),
    ("猫咪带我去你的设计文档", NAV),
    ("小猫咪我都有哪些收藏", OWN),
    ("还有什么待办吗", OWN),
    ("小猫咪！我有哪些未读通知呀", OWN),
    ("今天天气怎么样", {"get_weather"}),
    ("小猫咪现在生产环境状态怎么样", OPS),
    ("留言板那边还等着我点头的几件，你怎么看？", MODR),
    ("帮我看看后台有没有等着审核的留言", MODR),
    ("把那条写着「泠月喵好笨啊」的留言删掉吧", MODW),
    ("给小猫咪我读一下这篇文章", {"get_article_detail"}),
    ("看看最新的几篇笔记的标题", {"list_notes", "get_top_notes", "search_notes"}),
]

TOOL_NAMES = {t.name for t in get_all_tools()}

# 技能 → 它 plan 模板里出现的工具名（技能被展开后"真正落到哪些工具"由此折算，
# 三臂才可比：`react_assets` 只看得见技能名，`planner` 看得见计划文本，`react_line`
# 看得见台账——三种口径统一折算成工具名再比）
_SKILL_TOOLS: dict[str, set[str]] = {}
for _s in SKILLS:
    try:
        _txt = json.dumps(_s.plan, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        _txt = str(_s.plan)
    _SKILL_TOOLS[_s.name] = {n for n in TOOL_NAMES if n in _txt}

CFG = {"configurable": {"principal": Principal(uid=7, role="admin"),
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

    ⚠️ 必须吃 `**kwargs`：`SkillExecutor` 是 `fn(**args)` 调的，签名收不下就会
    抛 TypeError —— 而那是**被包装层兜成错误帧**的（不会炸图），于是症状是
    "每个工具都 BLOCK error_frame"，看起来像判据在开火，其实是桩写错了。
    """
    return {t.name: (lambda *a, _n=t.name, **k: _stub_text(_n)) for t in get_all_tools()}


def skill_stubs() -> list:
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
    skills = [str(e.get("calls") or "") for e in dec]
    # ⚠️ 工具名**只能**从计划文本的 TOOLS 行取：`native_decision.calls` 里只有技能名，
    # 读族的工具全在 PARAMS 里 —— 读那格会把「我有哪些未读通知」误记成"零调用"。
    plan_txt = "".join(str(upd.get(k) or "") for k in ("plan",))
    tools = {n for n in re.findall(r"([a-z_]{4,})\s*\(", plan_txt) if n in TOOL_NAMES}
    for s in skills:
        if s in TOOL_NAMES:
            tools.add(s)
    out.update(skills=skills, tools=sorted(tools), calls=len([s for s in skills if s]),
               rounds=len(dec))
    return out


# ── 臂 B/C′/D：create_agent ───────────────────────────────────────────────────
def _capture_prompt(sent: str, i: int) -> str:
    """抓 planner 当轮**真实**渲染出来的提示词（monkeypatch `bind_native`，行为委托）。"""
    cap: dict = {}
    orig = G.bind_native

    class _Rec:
        def __init__(self, inner):
            self._inner = inner

        def invoke(self, prompt, *a, **k):
            cap["prompt"] = prompt if isinstance(prompt, str) else str(prompt)
            return self._inner.invoke(prompt, *a, **k)

        def __getattr__(self, n):
            return getattr(self._inner, n)

    def _fake(llm, role, *, task_state=False):
        return _Rec(orig(llm, role, task_state=task_state))

    G.bind_native = _fake
    try:
        run_planner(sent, f"cap_{i}")
    finally:
        G.bind_native = orig
    return cap.get("prompt", "")


def _collect(msgs: list, out: dict) -> None:
    calls, n_ai = [], 0
    for m in msgs:
        if isinstance(m, AIMessage):
            n_ai += 1
            for c in (m.tool_calls or []):
                calls.append(str(c.get("name") or ""))
                out["_raw"] |= {n for n in TOOL_NAMES
                                if n in json.dumps(c.get("args") or {}, ensure_ascii=False)}
                out["_raw"] |= _SKILL_TOOLS.get(str(c.get("name") or ""), set())
    out["skills"], out["calls"], out["rounds"] = calls, len(calls), n_ai


def run_react(sent: str, sys_prompt: str, *, line: bool) -> dict:
    """`line=True` ⇒ 走本分支（收敛判据＋技能级回执）；否则是臂 C′（裸 create_agent）。"""
    out = {"error": "", "skills": [], "tools": [], "calls": 0, "text": "", "capped": False,
           "rounds": 0, "ledger": None, "_raw": set()}
    llm = get_llm(**DECODE)
    stubs = SKILL_STUBS
    if line:
        # 一次运行一本账：executor（技能展开）与包装层/中间件必须共用同一个实例。
        led = RunLedger()
        ledger_holder["led"] = led
        agent, led = build_agent(llm, stubs, system_prompt=sys_prompt, budget=BUDGET,
                                 executor=SkillExecutor(RAW_STUBS, led, role="admin"),
                                 ledger=led)
    else:
        agent = create_agent(model=llm, tools=stubs, system_prompt=sys_prompt,
                             name="ab_react_assets")
    last: dict = {}
    try:
        for st in agent.stream({"messages": [HumanMessage(content=sent)]},
                               {"recursion_limit": REC_LIMIT}, stream_mode="values"):
            last = st
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        out["capped"] = "Recursion" in type(e).__name__
    msgs = last.get("messages") or []
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


SKILL_STUBS: list = []
RAW_STUBS: dict = {}
ledger_holder: dict = {}


def score(r: dict, expected: set) -> dict:
    return {"called_any": r["calls"] > 0, "matched": bool(set(r["tools"]) & expected),
            "hit": sorted(set(r["tools"]) & expected)}


def main() -> int:  # noqa: C901
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--arms", default="planner,react_assets,react_line")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 句（试跑用）")
    ap.add_argument("--out", default="/tmp/react_line_ab.json")
    a = ap.parse_args()
    global SKILL_STUBS, RAW_STUBS
    SKILL_STUBS, RAW_STUBS = skill_stubs(), raw_stubs()
    sents = SENTS[:a.limit] if a.limit else SENTS
    arms = [s for s in a.arms.split(",") if s]

    prompts = {}
    if any(x != "planner" for x in arms):
        print(f"抓 planner 真提示词（{len(sents)} 句）…", flush=True)
        for i, (sent, _e) in enumerate(sents, 1):
            prompts[sent] = _capture_prompt(sent, i)
            print(f"  [capture {i}/{len(sents)}] {len(prompts[sent])} 字  {sent[:16]}", flush=True)

    R: dict = {"arms": arms, "decode": DECODE, "rec_limit": REC_LIMIT, "budget": BUDGET,
               "rounds": []}
    for r in range(1, a.rounds + 1):
        order = arms if r % 2 else list(reversed(arms))   # 两臂交替，抵消端点漂移
        row = {"round": r, "arms": {}}
        for arm in order:
            t0, recs = time.time(), []
            for i, (sent, exp) in enumerate(sents, 1):
                if arm == "planner":
                    rr = run_planner(sent, f"rl_{arm}_{r}_{i}")
                else:
                    ledger_holder["led"] = None
                    rr = run_react(sent, prompts[sent], line=(arm == "react_line"))
                rr["sent"] = sent
                rr.update(score(rr, exp))
                recs.append(rr)
                note = ""
                if rr.get("ledger"):
                    note = (f" stop={rr['ledger']['stop']} 回执={rr['ledger']['receipts']}"
                            f" 受阻={len(rr['ledger']['blocked'])}")
                print(f"[{arm} r{r} {i}/{len(sents)}] 工具={rr['tools']} 命中={rr['hit']}"
                      f"{' CAPPED' if rr['capped'] else ''}{note} {sent[:16]}", flush=True)
            n_any = sum(1 for x in recs if x["called_any"])
            n_hit = sum(1 for x in recs if x["matched"])
            n_cap = sum(1 for x in recs if x["capped"])
            n_ai = sum(int(x.get("rounds") or 0) for x in recs)
            # 「回执」要分两种：`tool` 非空 = 真执行过某个工具；`tool` 为空 = 纯应答
            # 技能（chat）留的那条。混在一起会读成"这一轮办成了 6 件"。
            n_rc = sum(sum(1 for r in (x["ledger"] or {}).get("row_receipts", [])
                           if r.get("tool")) for x in recs if x.get("ledger"))
            bb = {}
            for x in recs:
                for b in ((x.get("ledger") or {}).get("blocked") or []):
                    bb[b.get("reason", "?")] = bb.get(b.get("reason", "?"), 0) + 1
            stops = {}
            for x in recs:
                s = (x.get("ledger") or {}).get("stop") or ""
                stops[s or "自然收尾"] = stops.get(s or "自然收尾", 0) + 1
            row["arms"][arm] = {"called_any": f"{n_any}/{len(sents)}",
                                "matched": f"{n_hit}/{len(sents)}", "capped": n_cap,
                                "llm_rounds": n_ai, "receipts": n_rc, "blocked": bb,
                                "stops": stops, "secs": round(time.time() - t0, 1),
                                "rows": recs}
            print(f"== {arm} r{r}: 有调用 {n_any}/{len(sents)}  命中族 {n_hit}/{len(sents)}"
                  f"  撞上限 {n_cap}  模型轮数 {n_ai}  回执 {n_rc}  受阻 {bb}"
                  f"  停止 {stops}  用时 {time.time() - t0:.0f}s", flush=True)
        R["rounds"].append(row)
        Path(a.out).write_text(json.dumps(R, ensure_ascii=False, indent=1))

    print("\n=== SUMMARY ===")
    for arm in arms:
        print(arm, [(x["round"], x["arms"][arm]["matched"], x["arms"][arm]["capped"],
                     x["arms"][arm]["llm_rounds"], x["arms"][arm]["secs"])
                    for x in R["rounds"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
