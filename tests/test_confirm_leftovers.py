# -*- coding: utf-8 -*-
"""弹窗之后剩下的动作（20260927 生产事故，两半合起来才看得懂）。

**现场**（同一句话的两轮 trace）：

  · `20260927T171545`：主人说「不错收藏啦，开启夜间模式和雪花」——三个动作。planner
    一轮只能选一个技能（`SKILL=` 单值），选中收藏 ⇒ 写操作弹确认卡 ⇒ 本轮 END。
  · `20260927T171550`：主人点「确定」⇒ 前端合成确认句 ⇒ 服务端凭令牌**零 LLM** 拼计划
    ⇒ 执行收藏 ⇒ 直去 narrator ⇒ 它写下「**夜间模式和雪花特效这边也一并处理好了**」，
    gate 判 PASS。主人读到的是一句系统没做过的事，而这一轮的全部动作只是一次收藏。

两半各是一个缺陷，**而且互相喂养**：后两个动作之所以丢，是因为"还有没做完的事"这个
判据在弹窗之后**恒为假**（确认轮的"当前消息"是前端合成的确认句，意图扫描读它当然扫
不出东西）；而幻觉之所以能过闸，是因为既有的四张网都不覆盖"实体 + 完成式"这个形状
（洞① 只在零帧轮、5c 要点名工具、5d/5f 是内容域、5g 的射程是"回执在场时同一句话说
两遍"）。⇒ 修法也是两半：接回规划回路（决策权仍在 planner，**不新造执行通道**），
外加一张**实体锚定**的声称网。

本套件按这个顺序分三段：① 意图清单看得见（消息来源 + 未完成判据）；② 回路接回去
（`route_after_execute` + planner 的 resumed 轮 + 令牌语义未变）；③ 幻觉网（纯函数 +
gate 直调 + 兜底文案）。全部秒级、零网络、零 LLM。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.graph import (  # noqa: E402
    _FALLBACK_DEED_NO_RECEIPT, _fallback_deed_no_receipt, _pending_intents, _prev_user_msg,
    _unsupported_deed_claims, gate_node, parse_plan, plan_encode, planner_node,
    route_after_execute,
)
from agent.skills import instantiate_plan  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ── 现场语料（逐字取自两条生产 trace，不许改写：判据就是照着它们定的）──────────
ASK = "不错收藏啦，开启夜间模式和雪花"
CONFIRM_ASK = "确认执行：要收藏文章 19吗？点「确定」我就去办。"
FAVORITE_SPEC = 'add_favorite({"article_id": 19})'
# 已验签的令牌体（真形状：`confirm.py` 签的就是这两个字段——技能名 + 逐条 spec）。
# 形状写错的代价是"照令牌拼计划"那一支恒空清单，而用例照样绿（判据哑掉）。
GRANT = {"v": 2, "uid": 5, "conv": 251, "skill": "favorite_add",
         "specs": [{"tool": "add_favorite", "args": {"article_id": 19}}]}
HALLUCINATION = ("好嘞主人～收藏好啦！**夜间模式和雪花特效这边也一并处理好了**，"
                 "晚上看博客眼睛会舒服很多，飘着雪花也很有氛围哦！:比耶:")
FAVORITE_RCPT = {"skill": "favorite_add", "tool": "add_favorite",
                 "args": {"article_id": "19"}, "result": "已收藏文章 19"}


def _effect_rcpt(effect="snow"):
    return {"skill": "effect", "tool": "toggle_effect",
            "args": {"effect": effect, "action": "on"},
            "result": f"特效 {effect} 已打开",
            "cmd": {"kind": "effect", "effect": effect, "action": "on"}}


def _dark_rcpt():
    return {"skill": "darkmode", "tool": "toggle_dark_mode", "args": {"mode": "on"},
            "result": "夜间模式已打开", "cmd": {"kind": "darkmode", "mode": "on"}}


# ══════════════════════════════════════════════════════════════════
print("\n① 意图清单在确认轮看得见（事故的第一半：后两个动作从此不再丢）")

_msgs = [HumanMessage(content=ASK),
         AIMessage(content="要收藏文章 19吗？点「确定」我就去办。"),
         HumanMessage(content=CONFIRM_ASK)]

check("确认轮的当前消息是**前端合成的确认句**（判据前提，取自 chat-stream.js 的拼法）",
      CONFIRM_ASK.startswith("确认执行：") and "收藏" in CONFIRM_ASK)
check("  拿它去扫意图 → 一条都扫不出来（这就是事故里『恒为空』的成因）",
      G._scan_action_intents(CONFIRM_ASK) == [])
check("确认轮换源到主人原话（`_prev_user_msg` 取倒数第二条 HumanMessage）",
      _prev_user_msg(_msgs) == ASK)
check("  原话里扫出两件未完成（雪花 + 夜间模式）",
      [i["key"] for i in G._scan_action_intents(ASK)] == ["effect:snow=on", "darkmode=on"])

_st = {"messages": _msgs, "confirm_grant": dict(GRANT), "executed": []}
check("`_intent_src` 读主人原话（有 confirm_grant 时）", G._intent_src(_st) == ASK)
check("  非确认轮照旧读当前消息（这一支一个字没动）",
      G._intent_src({"messages": [HumanMessage(content="把樱花关掉")]}) == "把樱花关掉")
check("  历史里没有第二条 HumanMessage → 退回当前消息，不抛异常",
      _prev_user_msg([HumanMessage(content="你好")]) == ""
      and G._intent_src({"messages": [HumanMessage(content="你好")],
                         "confirm_grant": {}}) == "你好")

check("`_pending_intents`：收藏已执行 ⇒ 剩下的是那两件（都已执行过的收藏不再出现）",
      [i["key"] for i in _pending_intents({**_st, "executed": [FAVORITE_SPEC]})]
      == ["effect:snow=on", "darkmode=on"])


# ══════════════════════════════════════════════════════════════════
print("\n② 回路接回去（执行完直去 narrator 之前先看清单）")


def _cfg():
    from agent.authz import Principal
    return {"configurable": {"principal": Principal(uid=5, role="user"),
                             "user_id": 5, "conversation_id": 251, "stop_event": None}}


def _grant_state(**over):
    """兑现成功那一刻的 state：真 trace 里执行完还会多一条 ToolMessage（has_frames）。"""
    msgs = list(_msgs) + [ToolMessage(content="已收藏文章 19", tool_call_id="execute_0",
                                      name="add_favorite")]
    st = {"messages": msgs, "confirm_grant": dict(GRANT),
          "plan_rounds": 1, "executed": [FAVORITE_SPEC], "pending_confirm": None,
          "blocked": [], "noop_note": None, "done": False}
    st.update(over)
    return st


check("兑现成功 + 还有未完成动作 → 交回 planner（事故里是直去 narrator）",
      route_after_execute(_grant_state()) == "planner")
check("  三件都做完了 → 直去 narrator（20260921 那条语义原样保留）",
      route_after_execute(_grant_state(executed=[FAVORITE_SPEC,
                                                'toggle_effect({"effect": "snow", "action": "on"})',
                                                'toggle_dark_mode({"mode": "on"})'])) == "model")
check("  受阻回环（blocked）→ 仍走 planner（那一支不吃本判据）",
      route_after_execute(_grant_state(blocked=[{"spec": FAVORITE_SPEC}])) == "planner")
check("  非确认轮一个字节没动（无 grant ⇒ 无受阻即回 planner）",
      route_after_execute({"messages": _msgs, "executed": [], "blocked": [],
                           "pending_confirm": None, "noop_note": None}) == "planner")
check("  弹卡轮仍 END（本轮不执行任何东西）",
      route_after_execute(_grant_state(pending_confirm={"q": "要收藏吗"},
                                       plan_rounds=0, executed=[])) == "end")


class _ScriptedLLM:
    """照 tests/test_skills.py 的 `_ScriptedLLM`：按顺序吐回复、留 prompt 供断言。"""

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return AIMessage(content=self.replies.pop(0))


_orig_llm = G.get_llm
try:
    # ── 形态 A：令牌首轮（rounds==0）照旧**零 LLM**，一个字节都不许改 ──────────
    llm0 = _ScriptedLLM([])
    G.get_llm = lambda **kw: llm0       # noqa: ARG005
    out0 = planner_node({"messages": _msgs, "plan_rounds": 0,
                         "confirm_grant": dict(GRANT)}, _cfg())
    check("令牌首轮零 LLM（没问过模型一次）", len(llm0.prompts) == 0)
    check("  照令牌拼出来的清单就是签名里那一条",
          parse_plan(out0["plan"])["tools"] == [FAVORITE_SPEC],
          str(parse_plan(out0["plan"])["tools"]))

    # ── 形态 B：兑现成功后被交回 ⇒ 走正常 LLM 决策轮，且告知"还剩这几件"──────
    llm1 = _ScriptedLLM(['SKILL=effect\nPARAMS={"effect": "snow", "action": "on"}\n'
                         "REPLY: 雪花给你打开啦"])
    G.get_llm = lambda **kw: llm1       # noqa: ARG005
    out1 = planner_node(_grant_state(), _cfg())
    check("被交回的确认轮走 LLM 决策（问了一次）", len(llm1.prompts) == 1)
    check("  {correction} 槽写明「上一件已办完、这两件没办」（含中文标签与 key）",
          "确认轮剩余意图" in llm1.prompts[0] or
          ("雪花特效开（effect:snow=on）" in llm1.prompts[0]
           and "夜间模式开（darkmode=on）" in llm1.prompts[0]),
          "")
    check("  且明说「不要重做刚刚兑现的那次操作」（防把写操作做第二遍）",
          "不要" in llm1.prompts[0] and "重做" in llm1.prompts[0])
    check("  {intent_hints} 读的也是主人原话（两件都标【未完成】）",
          llm1.prompts[0].count("**未完成**") >= 2)
    check("  本轮照模型决策执行（雪花），不是重发令牌那件事",
          parse_plan(out1["plan"])["tools"] ==
          ['toggle_effect({"effect": "snow", "action": "on"})'],
          str(parse_plan(out1["plan"])["tools"]))

    # ── 形态 C：令牌那件事**受阻**（blocked）⇒ 照旧确定性收尾，不问模型 ──────
    llm2 = _ScriptedLLM([])
    G.get_llm = lambda **kw: llm2       # noqa: ARG005
    out2 = planner_node(_grant_state(plan_rounds=1, blocked=[{"spec": FAVORITE_SPEC}]),
                        _cfg())
    check("受阻第二轮零 LLM + 确定性收尾（不重发清单，20260921 语义）",
          len(llm2.prompts) == 0 and parse_plan(out2["plan"])["status"] == "wrapped",
          str(parse_plan(out2["plan"])["status"]))
finally:
    G.get_llm = _orig_llm


# ══════════════════════════════════════════════════════════════════
print("\n③ 幻觉网：实体锚定的完成式声称（事故的第二半）")

_hits = _unsupported_deed_claims(HALLUCINATION, [FAVORITE_RCPT])
check("现场那句命中（实体 = 雪花特效、夜间模式）",
      sorted(lbl for lbl, _ in _hits) == ["夜间模式", "雪花特效"], str(_hits))
check("  子句是**判据看到的那一句**（trace 里要能对上）",
      _hits[0][1] == "**夜间模式和雪花特效这边也一并处理好了**", _hits[0][1])
check("两者都真做过 ⇒ 不命中（回执按**实体**比，不按族比）",
      _unsupported_deed_claims(HALLUCINATION, [_effect_rcpt(), _dark_rcpt()]) == [])
check("  只做过雪花 ⇒ 只留夜间模式那一件（不牵连已办成的那件）",
      [lbl for lbl, _ in _unsupported_deed_claims(HALLUCINATION, [_effect_rcpt()])]
      == ["夜间模式"])
check("  特效回执的 effect 对不上（开了雨、说了雪）⇒ 仍命中",
      [lbl for lbl, _ in _unsupported_deed_claims(HALLUCINATION, [_effect_rcpt("rain")])]
      == ["夜间模式", "雪花特效"])

check("幂等轮的正确答案不命中（golden `eff_state_consistent` 的措辞，20260920 误伤过一次）",
      _unsupported_deed_claims(
          "是的喵～樱花特效现在正开着呢！🌸 系统这边显示当前页面开启的特效就是 `sakura`", [])
      == [])
check("  没有完成式施事语、只是陈述 → 不命中",
      _unsupported_deed_claims("雪花特效是开着的呀，你抬头就能看到", []) == [])
check("  纯状态轮（没有实体）→ 不命中",
      _unsupported_deed_claims("好的主人，我看看～", []) == [])
check("  否定式（'我没有帮你打开雪花特效'）→ 不命中",
      _unsupported_deed_claims("我没有帮你打开雪花特效哦", []) == [])
_quoted = "系统记录里那句「夜间模式也一并处理好了」不是我说的"
check("  引号里是**转述** ⇒ 调用方先剥壳（gate 那一侧剥、纯函数这一侧不剥）",
      _unsupported_deed_claims(_quoted, []) != []
      and _unsupported_deed_claims(G._strip_quoted_spans(_quoted), []) == [])

print("  ── 别名重复（同一实体命中多个别名时只算一次）")
check("「雪」与「雪花」同时命中 ⇒ 只出一个标签（不印成「雪特效、雪花特效」）",
      [lbl for lbl, _ in _unsupported_deed_claims("雨和雪都一并处理好了", [])]
      == ["雨特效", "雪特效"])

print("  ── gate 直调")


def _gate_state(reply, receipts):
    """事故那一轮的形状：**有帧**（收藏执行过）+ 回执里只有那次写。

    帧在场是这个用例的前提——洞① 只管零帧轮，而这一条判据恰恰要在**有帧轮**上
    生效（这正是它当初漏掉的原因之一）。
    """
    return {"plan": plan_encode(instantiate_plan("chat", {})),
            "messages": [HumanMessage(content=ASK),
                         ToolMessage(content="已收藏文章 19", tool_call_id="execute_0",
                                     name="add_favorite"),
                         AIMessage(content=reply)],
            "receipts": receipts, "executed": [FAVORITE_SPEC], "done": False,
            "plan_rounds": 2}


_events: list = []
_orig_record = G.record
try:
    G.record = lambda *a, **k: _events.append((a, k))   # noqa: ARG005

    _events.clear()
    o1 = gate_node(_gate_state(HALLUCINATION, [FAVORITE_RCPT]))
    check("本轮只有一次收藏（无命令族回执）⇒ fallback",
          o1.get("done") is True and o1.get("fallback_text") ==
          _fallback_deed_no_receipt(["夜间模式", "雪花特效"]),
          str(o1.get("fallback_text"))[:40])
    check("  落 `gate.action_claim_no_receipt` 事件（soft=False，可按维计数）",
          any(a[1] == "action_claim_no_receipt" and k.get("soft") is False
              for a, k in _events), str(_events[:3]))

    _events.clear()
    o2 = gate_node(_gate_state(HALLUCINATION,
                               [FAVORITE_RCPT, _effect_rcpt(), _dark_rcpt()]))
    check("两句都真做过 ⇒ 网**根本不响**（回执按实体比，不按'有没有动作'）",
          not o2.get("fallback_text")
          and not any(a[1] == "action_claim_no_receipt" for a, k in _events),
          str(_events[:3]))

    # 这一条才是 carve-out 的现场：**这一轮有命令族回执、但被点名的那个实体没有**
    # （只开了夜间模式，却把雪花也说成办好了）。判死的代价是 `__RESET__` 把本轮已
    # 下发的 `__CMD__` 一起清掉——那条"夜间模式已打开"的命令从未到过前端，而气泡里
    # 印着系统事实块 ⇒ 系统说了它没做的事（20260927 D3 的实测教训）。
    _events.clear()
    o2b = gate_node(_gate_state(HALLUCINATION, [FAVORITE_RCPT, _dark_rcpt()]))
    check("只开过夜间模式却声称雪花也办了 ⇒ 网响，但只记不判（有命令族回执）",
          not o2b.get("fallback_text")
          and any(a[1] == "action_claim_no_receipt" and k.get("soft") is True
                  and k.get("entity") == ["雪花特效"] for a, k in _events),
          str(_events[:3]))
    check("  且**没有** fallback 事件（命令照常下发，主人读到的是系统事实块）",
          not any(a[1] == "fallback" for a, k in _events))

    _events.clear()
    o3 = gate_node(_gate_state("好嘞主人～文章 19 收藏好啦 :比耶:", [FAVORITE_RCPT]))
    check("如实叙述 → 放行（不误伤）",
          not o3.get("fallback_text")
          and not any(a[1] == "action_claim_no_receipt" for a, k in _events))
finally:
    G.record = _orig_record

print("  ── 兜底文案（只否认那一件，不否认整轮）")
check("点名被否认的那几件", "雪花特效、夜间模式" in _fallback_deed_no_receipt(
    ["雪花特效", "夜间模式"]))
check("  **不**说站里没有这东西（这一轮压根没查过特效有没有这个能力）",
      "没有这个" not in _FALLBACK_DEED_NO_RECEIPT
      and "不存在" not in _FALLBACK_DEED_NO_RECEIPT)
check("  也不说整轮什么都没做（本轮真做过的那件另有事实块印着）",
      "什么都没做" not in _FALLBACK_DEED_NO_RECEIPT
      and "一个工具都没有执行" not in _FALLBACK_DEED_NO_RECEIPT)
check("  指回系统记录（事实块由 producer 印在正文之前，RESET 后重印）",
      "系统记录" in _FALLBACK_DEED_NO_RECEIPT)
check("  空标签退一句整话（不印出半截句子）",
      _fallback_deed_no_receipt([]).startswith("喵呜") and "这件事" in
      _fallback_deed_no_receipt([]))


def main():
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项不通过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
