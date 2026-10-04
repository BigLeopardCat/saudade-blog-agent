#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零调用残余的定点探针：**只看决策层**，读计数不读通过率。

## 它量什么

上一批（`6f5a1dc`）把「一个函数都不点」从合法决策降级成「纠偏一次」，并把契约第 7 条
改成「闲聊也要**显式**点 `chat`」。这带来两个必须分开数的东西：

  · **零调用**（`finish=stop`，一个函数都没点）——被纠偏罩住了；
  · **显式 chat**（点了 `chat` 但本轮零工具）——**纠偏看不见它**（`undecided` 只为
    前者置位）。契约改动的净效果可能是"把洞从'不点'挪到'点 chat'"：对
    「小猫咪现在生产环境状态怎么样」这类**系统查得到**的问题，模型点 `chat` 与
    什么都不点在结果上完全一样（零帧、零结果），而闸门里"chat 轮的第一人称读取
    声称"是刻意豁免的 ⇒ 那条 20261001 的实况（「我刚查了一遍…三个核心服务全部
    active」、零帧、`gate.pass`）**今天仍然拦不住**。

所以本探针按「这句话**该不该**有工具动作」把句子分两组，分别数**零工具决策**的
比例——数据型句子上出现零工具决策，就是残余。

## 它不量什么

不跑 narrator、不跑 gate（那是另外两层）；`planner_node` 只决策不执行，**零真实写**。
一轮 = 一两次 planner LLM 调用。

判据是**多遍计数**（同一批句子跑 N 轮取分布），单遍一律当噪声。

跑法（必须在仓根、`PYTHONPATH` 指到本分支——venv 的 editable `.pth` 把主仓钉在
`sys.path` 上，不指就静默跑主仓那份代码）：

    cd <本分支 worktree>
    PYTHONPATH=$PWD .venv/bin/python eval/zero_call_residual_probe.py --rounds 3

判读见 `docs/zero-call-residual.md`。同族放在 `eval/` 平铺的先例：`native_tools_probe.py`、
`task_state_probe.py`、`d4_structured_output_poc.py`（**要花真 LLM 调用，不进 CI**）。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.principal import Principal  # noqa: E402
from tools import get_all_tools  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

TOOL_NAMES = {t.name for t in get_all_tools()}

CFG = {"configurable": {"principal": Principal(uid=7, role="admin"),
                        "user_id": 7, "conversation_id": 4242, "stop_event": None}}

# ── 句子表 ─────────────────────────────────────────────────────────────────────
# 族非空 ⇒ **数据型**（正确答案必须落到某个工具上，零工具 = 残余）；
# 族为空 ⇒ **闲聊型对照组**（正确形态就是零工具）。
#
# 前 16 句与 `react_line_ab.py` 同源（那是 20261004 三臂 A/B 的同一批，可比）；
# 后 6 句是从生产 trace 里捞出来的**零帧零调用轮原话**（20260927–20261004，
# 见 `/tmp/byday.py` 的输出），它们是残余的真实样本而不是造出来的。
ACCT = {"freeze_account", "unfreeze_account", "set_account_role", "account_mute"}
MODW = {"audit_board_comment", "delete_board_comment"}
MODR = {"get_moderation_status", "list_admin_board", "list_guestbook"}
MSG = {"read_notifications", "read_messages"}
OWN = {"list_my_favorites", "get_unread_summary", "list_notifications", "list_dashboard_todos"}
OPS = {"get_server_status", "get_service_health"}
NAV = {"navigate_to"}

SENTS: list[tuple[str, set[str]]] = [
    ("牛牛冻结账号给日程4标记为未完成", ACCT),
    ("把它号封吧", ACCT),
    ("给他驳回请求", MODW),
    ("两个都通过吧", MODW),
    ("猫咪我的未读信息全部就标记为已读", MSG),
    ("猫咪带我去你的设计文档", NAV),
    ("小猫咪我都有哪些收藏", OWN),
    ("还有什么待办吗", OWN),
    ("小猫咪！我有哪些未读通知呀", OWN),
    ("今天天气怎么样", {"get_weather"}),
    ("小猫咪现在生产环境状态怎么样", OPS),     # ← 20261001 那条拦不住的漏网
    ("留言板那边还等着我点头的几件，你怎么看？", MODR),
    ("帮我看看后台有没有等着审核的留言", MODR),
    ("把那条写着「泠月喵好笨啊」的留言删掉吧", MODW),
    ("给小猫咪我读一下这篇文章", {"get_article_detail"}),
    ("看看最新的几篇笔记的标题", {"list_notes", "get_top_notes", "search_notes"}),
    # ↓ trace 里捞的原话
    ("竟然敢说我们猫猫笨，把他号封吧要不", ACCT),
    ("猫咪，好刺眼", {"toggle_effect", "set_theme"}),
    ("猫咪这段我看不太懂", {"get_article_detail", "rag_search"}),
    ("嗯，看看吧", {"list_notes", "list_admin_board"}),
    # ↓ 闲聊对照组（零工具是**对的**）
    ("你随便说一点吧", set()),
    ("猫咪你都可以做什么", set()),
    ("按你想法啦", set()),
    ("你好呀，今天心情不错呢", set()),
]


# ── 换臂：三个臂，各自只还原**一笔改动的行为面** ───────────────────────────────
# 一律不换文件（`git show` 换 `graph.py` 会把同期那批路径改名一起退回去），只在运行期
# 把那一笔的行为面还原——精确，且不会碰到无关代码。
#
#   · `after`（默认，什么都不做的那个臂）：本批（判据前移）生效；
#   · `before`：把 `authz` 那两条窄判据短路成恒 False —— 这就是本批改动的**唯一行为面**
#     （`planner_node` 里新加的那支只在两条判据为真时才有动作），所以它是"改动前"；
#   · `legacy`：再往前退一笔（`6f5a1dc` 之前）——① 契约第 7 条回到「可以不调用任何
#     函数、直接给正文」；② `undecided` 恒 False（没有纠偏通道）。
#
# `planner_node` 调 `_render_planner_prompt` 时**不传** `contract`（吃默认值），
# 所以 legacy 只能在这一层替。
LEGACY_CONTRACT = """\
7. 你的决定**通过工具调用表达**：调用本轮 tools 里与所选**技能同名**的那个函数，
   把该技能的参数填进 arguments。技能名与参数名一律以 tools 里的定义为准——不要
   自己造名字，也不要把**工具**名（技能模板内部用的那些）当成技能名。
   - 正文不是决策通道：**不要**再写 SKILL=/PARAMS= 这类契约行，决策只以工具调用为准
     （正文只在你自己想留一句说明时写，主人看不到规划轮的正文）。
   - 只想闲聊、或如实说明查不到时，可以不调用任何函数、直接给正文。
   - 多步链的中间轮仍可在正文里另起一行写 `TODO: <步骤1> → <步骤2>`（只描述本轮
     之后的后续依赖步骤，单步/收尾轮不写）。"""


class _Arm:
    """上下文管理器：按臂还原行为面（`after` 什么都不做）。"""

    def __init__(self, arm: str) -> None:
        self.arm = arm
        self._render = None
        self._parse = None
        self._authz = None

    def __enter__(self):
        if self.arm == "before":
            # 只短路两条判据（其余 authz 属性原样透传：planner_node 还会读别的）。
            import types

            self._authz = G.authz
            shim = types.SimpleNamespace(**{k: v for k, v in vars(G.authz).items()
                                            if not k.startswith("__")})
            shim.is_own_read_question = lambda _m: False
            shim.is_site_corpus_question = lambda _m: False
            G.authz = shim
            return self
        if self.arm != "legacy":
            return self
        import dataclasses

        self._render, self._parse = G._render_planner_prompt, G.tool_calls_to_plan
        orig_render, orig_parse = self._render, self._parse

        def _render_legacy(**kw):
            kw["contract"] = LEGACY_CONTRACT
            return orig_render(**kw)

        def _parse_legacy(*a, **kw):
            d = orig_parse(*a, **kw)
            return None if d is None else dataclasses.replace(d, undecided=False)

        G._render_planner_prompt = _render_legacy
        G.tool_calls_to_plan = _parse_legacy
        return self

    def __exit__(self, *exc):
        if self.arm == "before":
            G.authz = self._authz
        if self.arm == "legacy":
            G._render_planner_prompt, G.tool_calls_to_plan = self._render, self._parse
        return False


def run_one(msg: str, tid: str) -> dict:
    """跑一次 `planner_node`（只决策）。返回这一轮全部决策与台账事件。"""
    d = tempfile.mkdtemp(prefix="zero_call_probe_")
    trace_mod.start_trace(tid, 7, "admin", {}, dir=d, by_day=False)
    out: dict = {"error": "", "decisions": [], "nudge": 0, "accepted": 0,
                 "unparseable": 0, "truncated": 0, "tools": set(), "plan": "",
                 "dq": 0, "dq_still": 0}
    try:
        upd = G.planner_node({"messages": [HumanMessage(content=msg)], "plan_rounds": 0,
                              "executed": [], "tool_data": []}, CFG) or {}
    except Exception as e:  # noqa: BLE001
        upd = {}
        out["error"] = f"{type(e).__name__}: {e}"
    out["plan"] = "".join(str(upd.get(k) or "") for k in ("plan",))
    for e in trace_mod.events_of(tid):
        if e.get("node") != "planner":
            continue
        name = e.get("event")
        if name == "native_decision":
            out["decisions"].append({"round": e.get("round"),
                                     "skill": str(e.get("skill") or ""),
                                     "calls": str(e.get("calls") or ""),
                                     "finish": str(e.get("finish") or "")})
        elif name == "no_call_nudge":
            out["nudge"] += 1
        elif name == "no_call_accepted":
            out["accepted"] += 1
        elif name == "data_question_no_tool":
            out["dq"] += 1
        elif name == "data_question_still_no_tool":
            out["dq_still"] += 1
        elif name == "native_fallback":
            out["unparseable"] += 1
            if e.get("disposition") == "truncated_wrapup":
                out["truncated"] += 1
    # ⚠️ 工具名只能从**计划文本的 TOOLS 行**取：`native_decision.calls` 里只有技能名，
    # 读族的工具全在 PARAMS 里 —— 读那一格会把「我有哪些未读通知」误记成"零调用"。
    out["tools"] = {n for n in re.findall(r"([a-z_]{4,})\s*\(", out["plan"])
                    if n in TOOL_NAMES}
    return out


def kind_of(dec: dict) -> str:
    """一条决策的形状：`zero`（一个函数都没点）/ `chat`（点了 chat）/ `tool`。"""
    if not dec["calls"]:
        return "zero"
    if dec["skill"] == "chat":
        return "chat"
    return "tool"


def _blank() -> dict:
    return {"dec": 0, "zero": 0, "chat": 0, "tool": 0, "hit": 0, "nudge": 0,
            "accepted": 0, "unparseable": 0, "secs": [], "dq": 0, "dq_still": 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3, help="每句跑几轮（≥3 才看分布）")
    ap.add_argument("--only", default="", help="只跑含这段字的句子（逗号分隔）")
    ap.add_argument("--json", default="", help="把逐轮明细写到这个文件")
    ap.add_argument("--arms", default="after",
                    help="逗号分隔（after=本批改动生效 / before=判据短路 / legacy=退回"
                         "6f5a1dc 之前）；给两个就是两臂交替（本仓 A/B 纪律：不交替会把"
                         "「那个时段模型抖了」读成「改动生效了」）")
    a = ap.parse_args()

    sents = SENTS
    if a.only:
        keys = [k for k in a.only.split(",") if k]
        sents = [s for s in SENTS if any(k in s[0] for k in keys)]
    arms = [x for x in a.arms.split(",") if x]
    for arm in arms:
        if arm not in ("after", "before", "legacy"):
            raise SystemExit(f"未知臂：{arm}")

    detail: list[dict] = []
    agg = {(arm, ds): _blank() for arm in arms for ds in (True, False)}

    for rnd in range(1, a.rounds + 1):
        # 每轮把臂序**反过来**：同一个时段内的漂移对两臂机会均等。
        order = arms if rnd % 2 else list(reversed(arms))
        for i, (sent, fam) in enumerate(sents):
            for arm in order:
                t0 = time.monotonic()
                with _Arm(arm):
                    r = run_one(sent, f"probe_{arm}_r{rnd}_{i}")
                secs = time.monotonic() - t0
                data_shape = bool(fam)
                g = agg[(arm, data_shape)]
                dec = r["decisions"][-1] if r["decisions"] else {"skill": "", "calls": "",
                                                                 "finish": ""}
                k = kind_of(dec) if r["decisions"] else "zero"
                # 对照组的"判对"没有意义（点不点工具都可能对），只对数据型打分。
                hit = bool(r["tools"] & fam) if data_shape else True
                g["dec"] += 1
                g[k] = g.get(k, 0) + 1
                g["hit"] += int(hit)
                g["nudge"] += r["nudge"]
                g["accepted"] += r["accepted"]
                g["unparseable"] += r["unparseable"]
                g["dq"] += r["dq"]
                g["dq_still"] += r["dq_still"]
                g["secs"].append(secs)
                detail.append({"round": rnd, "arm": arm, "sent": sent,
                               "data_shape": data_shape,
                               "kinds": [kind_of(d) for d in r["decisions"]],
                               "skills": [d["skill"] for d in r["decisions"]],
                               "finish": [d["finish"] for d in r["decisions"]],
                               "tools": sorted(r["tools"]), "expect": sorted(fam),
                               "nudge": r["nudge"], "accepted": r["accepted"],
                               "dq": r["dq"], "dq_still": r["dq_still"],
                               "unparseable": r["unparseable"], "hit": hit,
                               "error": r["error"], "secs": round(secs, 1)})
                if len(arms) == 1:
                    flag = "" if (hit or not data_shape) else "  ✗"
                    print(f"[r{rnd} {i + 1:2d}/{len(sents)}] "
                          f"{'数据' if data_shape else '闲聊'} "
                          f"{'/'.join(kind_of(d) for d in r['decisions']) or '—':<16} "
                          f"最终={k:<4} 工具={sorted(r['tools']) or []} "
                          f"纠偏={r['nudge']} 仍零={r['accepted']} {r['error'][:40]}{flag}"
                          f"   {sent[:34]}")

    for arm in arms:
        print(f"\n== 分组的计数（臂={arm}，{len(sents)} 句 × {a.rounds} 轮）==")
        for data_shape, label in ((True, "数据型（该有工具）"), (False, "闲聊型（对照组）")):
            g = agg[(arm, data_shape)]
            n = g["dec"] or 1
            zt = g.get("zero", 0) + g.get("chat", 0)
            print(f"\n{label}  n={g['dec']}")
            print(f"  零调用(一个都没点) {g.get('zero', 0):3d}  "
                  f"显式 chat(零工具) {g.get('chat', 0):3d}  "
                  f"真工具决策 {g.get('tool', 0):3d}")
            print(f"  **零工具合计 {zt}/{n}（{zt / n:.1%}）**")
            print(f"  纠偏触发 {g['nudge']}   纠偏后仍零调用 {g['accepted']}   "
                  f"不可解析收尾 {g['unparseable']}   判对 {g['hit']}/{n}")
            print(f"  该取数却点 chat：纠偏 {g['dq']}   纠偏后仍点 chat {g['dq_still']}")
            if g["secs"]:
                print(f"  每句耗时 中位 {statistics.median(g['secs']):.1f}s")
    if len(arms) > 1:
        # 逐句看两臂差在哪（只列数据型）。`T` = 落到真工具、`C` = 显式 chat、`Z` = 零调用。
        print("\n== 逐句对照（只列数据型；T=真工具 C=显式chat Z=零调用）==")
        _code = {"tool": "T", "chat": "C", "zero": "Z"}
        per: dict[tuple[str, str], list[str]] = {}
        for r in detail:
            if r["data_shape"]:
                k = r["kinds"][-1] if r["kinds"] else "zero"
                per.setdefault((r["sent"], r["arm"]), []).append(_code[k])
        for sent, _fam in sents:
            got = {arm: "".join(per.get((sent, arm), [])) for arm in arms}
            if not any(got.values()):
                continue
            line = "  ".join(f"{arm}={got[arm] or '—'}" for arm in arms)
            print(f"  {line:<30} {sent[:34]}")
    if a.json:
        Path(a.json).write_text(json.dumps(detail, ensure_ascii=False, indent=1))
        print(f"\n明细写到 {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
