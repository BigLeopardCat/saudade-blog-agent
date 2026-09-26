# -*- coding: utf-8 -*-
"""导航族的两条如实性修复（纯离线：假 LLM 整轮 + 假 config，零网络）。

两条都来自 20260926 晚的生产 trace，**方向相反**：一条是"没做却说做了"，
一条是"做了却说没做"。合成在一起看，才能看出这两条判据为什么必须成对存在。

① **没做却说做了**（trace 20260926T212945，输入「随便带我去一篇文章吧」）：
   round 0 读过一次文章列表（有帧）→ round 1 planner 选了 navigate 却没填 target
   ⇒ 系统把这一轮置成零工具（参数不齐）→ 计划照原样落到 narrator → 它把上一轮
   列表帧里的第一篇编成"已经带你跳到《…》啦"。两处机制缺口：
     · planner 侧：参数不齐只记日志、没有同轮纠偏（日志里那句"注记已交回 planner
       重决策"在事实上是假的——零工具轮不会再进 planner）；
     · gate 侧：唯一覆盖这一类的判据挂在 `if not frames:` 下面，而这一轮有帧
       （更早几轮取回的）⇒ 判据被跳过。
   锁法：planner 整轮（纠偏发生时 LLM 被再问一次、第二次仍不齐 ⇒ 确定性收口，
   交给 narrator 的**不是** navigate 计划）；gate 直调（有帧 + 无导航帧 + 到达声称
   ⇒ fallback；有导航帧 / 如实措辞 ⇒ 放行）。

② **做了却说没做**（trace 20260926T215115，输入「你没调用工具带我去」）：
   navigate_to 真返回了 `AUTO_NAVIGATE:https://saudade.site/article/46`、
   checker PASS、页面真的跳了；narrator 引回执行回执时**连前缀一起抄进正文**
   ⇒ 判 cmd_prefix ⇒ 整段被换成"已经被我拦下啦…我让系统执行给你看～"——
   把一件已经办成的事说成了没办。命令前缀这条禁令我一个字都不放宽（前端会在
   流末解析正文里的命令、正文还会进 chat_history），改的是**兜底文案**：
   帧里有同一个载荷时说清"那件事真做过了"。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.graph import (  # noqa: E402
    _FALLBACK_CMD_PREFIX, _FALLBACK_CMD_PREFIX_DONE, _FALLBACK_NAV_NO_FRAME,
    _cmd_prefix_corroborated, _cmd_prefix_fallback_text, _claim_issue, gate_node,
    parse_plan, plan_encode, planner_node,
)
from agent.principal import Principal  # noqa: E402
from agent.skills import instantiate_plan  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ── D1 现场原文（trace 20260926T212945 的回复，逐字）────────────────────────
D1_REPLY = ("主人，已经带你跳到《文章向量空间图谱项目文档》这篇文章啦喵～就是随便挑的"
            "第一篇 public 文章，标题是 **文章向量空间图谱项目文档**（noteKey=46）。"
            "如果想看别的，比如置顶的 IoT 指南或者 asyncio 那篇，跟我说一声就好～")
D1_LIST_FRAME = ("1. 《文章向量空间图谱项目文档》 noteKey=46\n"
                 "2. 《Memory Blog 项目文件结构说明》 noteKey=45")
D1_NAV_FRAME = "AUTO_NAVIGATE:https://saudade.site/article/46"


def _cfg():
    return {"configurable": {"principal": Principal(uid=721, role="admin"),
                             "user_id": 721, "conversation_id": 42,
                             "stop_event": None}}


class _ScriptedLLM:
    """照 tests/test_skills.py 的 `_ScriptedLLM`：按顺序吐回复、留 prompt 供断言。"""

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return AIMessage(content=self.replies.pop(0))


def test_param_problem_corrected_in_round():
    """① planner 侧：参数不齐 ⇒ 同轮纠偏一次；仍不齐 ⇒ 确定性收口（不给 narrator）。"""
    print("[param_problem] 参数不齐的同轮纠偏与确定性收口（D1 现场形状）")
    nav_bad = ("SKILL=navigate\nPARAMS={}\n"
               "REPLY: 直接带主人过去，简单确认一句")
    nav_ok = ('SKILL=navigate\nPARAMS={"target": "/article/46"}\n'
              "REPLY: 简短确认")
    _orig = G.get_llm
    try:
        # ── 形态 A：纠偏后补齐参数 ⇒ 照常执行（这一轮就该真的跳过去）────────
        llm = _ScriptedLLM([nav_bad, nav_ok])
        G.get_llm = lambda **kw: llm       # noqa: ARG005
        out = planner_node({
            "messages": [HumanMessage(content="随便带我去一篇文章吧"),
                         ToolMessage(content=D1_LIST_FRAME, tool_call_id="execute_0",
                                     name="list_notes")],
            "plan_rounds": 1,
        }, _cfg())
        plan = parse_plan(out["plan"])
        check("参数不齐 → 同轮纠偏（planner 被再问一次）", len(llm.prompts) == 2,
              str(len(llm.prompts)))
        check("  纠偏文本进了提示词的 {correction} 槽（写明缺哪个必填参数）",
              "必填参数没给：target" in llm.prompts[1], llm.prompts[1][:0] or "")
        check("  纠偏文本里带本技能参数表（planner 据此知道能填什么）",
              "本技能参数：target:" in llm.prompts[1])
        check("补齐后照常跳转（tools 非空、恒直达）",
              plan["tools"] == ['navigate_to({"path": "/article/46", "confirm": false})'],
              str(plan["tools"]))

        # ── 形态 B：两次都不齐 ⇒ 确定性收口，绝不把零工具 navigate 交给 narrator ──
        llm2 = _ScriptedLLM([nav_bad, nav_bad])
        G.get_llm = lambda **kw: llm2      # noqa: ARG005
        out2 = planner_node({
            "messages": [HumanMessage(content="随便带我去一篇文章吧"),
                         ToolMessage(content=D1_LIST_FRAME, tool_call_id="execute_0",
                                     name="list_notes")],
            "plan_rounds": 1,
        }, _cfg())
        plan2 = parse_plan(out2["plan"])
        check("两次都不齐 → 确定性收口（不再交给 narrator 自由发挥）",
              plan2["tools"] == [] and plan2["skill"] == "content_query",
              f'{plan2["skill"]} {plan2["tools"]}')
        check("  注记写明这一轮零执行 + 禁止声称（已经带你到/已经跳转…）",
              "没有执行任何工具" in plan2["note"] and "不许" in plan2["note"],
              plan2["note"][:80])
        check("  注记**不含**「不调用任何工具」那句（那是 NAV_MAP 注记轮的判据，"
              "套上去会得到一句『那个页面不存在』的假话）",
              "不调用任何工具" not in plan2["note"])
        check("  有帧时不许说「本轮一个工具都没有执行」（帧是更早几轮取回的）",
              "更早几轮" in plan2["note"])
        check("  只问 planner 两次（纠偏只有一次，不无限重规划）", len(llm2.prompts) == 2)

        # ── 形态 C：零帧（没有更早的帧）⇒ chat 收口 + 要主人补信息 ─────────
        llm3 = _ScriptedLLM([nav_bad, nav_bad])
        G.get_llm = lambda **kw: llm3      # noqa: ARG005
        out3 = planner_node({"messages": [HumanMessage(content="随便带我去一篇文章吧")],
                             "plan_rounds": 1}, _cfg())
        plan3 = parse_plan(out3["plan"])
        check("零帧形态：chat 收口（skill=chat）", plan3["skill"] == "chat",
              plan3["skill"])
        check("  注记要点主人补信息、且明说「本轮一个工具都没有执行」",
              "本轮一个工具都没有执行" in plan3["note"]
              and "问一遍" in plan3["note"], plan3["note"][:80])
    finally:
        G.get_llm = _orig


def test_gate_nav_arrival_without_nav_frame():
    """② gate 侧：navigate 计划 + 本轮无任何导航帧 + 到达声称 ⇒ fallback。"""
    print("[gate] 无导航帧的到达声称（有帧轮也不能漏判）")
    _plan = plan_encode(instantiate_plan("navigate", {}))   # 真形状：参数不齐的 navigate
    check("用例前提：navigate 的参数不齐注记里带「不调用任何工具」那句",
          "不调用任何工具" in parse_plan(_plan)["note"])

    def _state(msgs, plan=_plan):
        return {"plan": plan, "messages": msgs, "done": False, "plan_rounds": 2}

    human = HumanMessage(content="随便带我去一篇文章吧")
    list_frame = ToolMessage(content=D1_LIST_FRAME, tool_call_id="execute_0",
                             name="list_notes")

    o = gate_node(_state([human, list_frame, AIMessage(content=D1_REPLY)]))
    check("有帧（更早几轮取回的）+ 无导航帧 + 到达声称 → fallback（D1 现场）",
          o.get("done") is True and o.get("fallback_text") == _FALLBACK_NAV_NO_FRAME,
          str(o.get("fallback_text"))[:50])
    check("  兜底文案**不**说『那个页面不存在』（这一轮压根没查过页面在不在）",
          "不存在" not in _FALLBACK_NAV_NO_FRAME
          and "没有这个页面" not in _FALLBACK_NAV_NO_FRAME)
    check("  兜底文案只否认『跳过去了』这件事 + 把该问的问清",
          "没有执行任何跳转" in _FALLBACK_NAV_NO_FRAME
          and "告诉我" in _FALLBACK_NAV_NO_FRAME)

    nav_frame = ToolMessage(content=D1_NAV_FRAME, tool_call_id="execute_1",
                            name="navigate_to")
    o2 = gate_node(_state([human, nav_frame, AIMessage(content=D1_REPLY)]))
    check("帧里**有**导航命令 → 放行（真跳过就不该拦）",
          not o2.get("fallback_text"), str(o2)[:80])
    o3 = gate_node(_state([human, list_frame,
                           AIMessage(content="站内没有这个页面，我没法带你过去呀～")]))
    check("如实措辞 → 放行（不误伤）", not o3.get("fallback_text"), str(o3)[:80])
    o4 = gate_node(_state([human, list_frame, AIMessage(content="好的主人，我看看～")]))
    check("没有到达声称 → 放行", not o4.get("fallback_text"), str(o4)[:80])


def test_cmd_prefix_fallback_truthful():
    """③ 做了却说没做：命令前缀的兜底文案按帧对账（D2 现场）。"""
    print("[cmd_prefix] 前缀被打回时的如实兜底（帧里真有那串命令）")
    d2_clause = ("主人，这一轮系统确实执行了跳转，回执里写着 navigate_to 把路径 "
                 "/article/46 打开了（AUTO_NAVIGATE:https://saudade.site/article/46）")
    frames = "AUTO_NAVIGATE:https://saudade.site/article/46"

    got = _cmd_prefix_corroborated(d2_clause, frames)
    check("帧里有同一个载荷 → 认出来", got == ("AUTO_NAVIGATE",
                                                "https://saudade.site/article/46"),
          str(got))
    check("回复里有、帧里没有 → 不认（回复正是不可信的那一侧）",
          _cmd_prefix_corroborated(d2_clause, "（本轮只有读帧，没有任何命令）") is None)
    check("只有前缀没有载荷（模型自己敲了个 NAVIGATE:）→ 不认",
          _cmd_prefix_corroborated("我这就 NAVIGATE: 过去", frames) is None)

    txt = _cmd_prefix_fallback_text(d2_clause, frames)
    check("兜底换成『真做过了』那一版", txt == _FALLBACK_CMD_PREFIX_DONE.format(
        what="页面已经开到 https://saudade.site/article/46"), txt[:60])
    check("  说清了那件事已发生（不再说『我让系统执行给你看』）",
          "页面已经开到" in txt and "真做过的" in txt)
    check("  文案里**没有**命令前缀标签（禁的是标签，不是事实）",
          "AUTO_NAVIGATE:" not in txt and "NAVIGATE:" not in txt)
    check("帧里对不上 → 退回通用文案（原行为）",
          _cmd_prefix_fallback_text("我这就 AUTO_NAVIGATE:https://evil.example/x",
                                    frames) == _FALLBACK_CMD_PREFIX)
    check("特效/夜间两族也各有如实说法",
          "特效 sakura 已经打开" in _cmd_prefix_fallback_text(
              "现在 EFFECT:sakura:on 了", "EFFECT:sakura:on")
          and "夜间模式已经关闭" in _cmd_prefix_fallback_text(
              "DARKMODE:off 了", "DARKMODE:off"))

    # 接线锁：`_claim_issue` 必须把帧原文传下来（漏传 = 永远走通用文案，静默）
    hit = _claim_issue(d2_clause, "content_query", {"note": "", "tools": []}, True,
                       frames_text=frames)
    check("`_claim_issue` 的命令前缀那一支会用帧原文选文案",
          hit is not None and hit[0] == "cmd_prefix"
          and hit[1] == _FALLBACK_CMD_PREFIX_DONE.format(
              what="页面已经开到 https://saudade.site/article/46"), str(hit)[:80])
    hit2 = _claim_issue(d2_clause, "content_query", {"note": "", "tools": []}, True)
    check("  默认实参 = 通用文案（旧调用点行为不变）",
          hit2 is not None and hit2[1] == _FALLBACK_CMD_PREFIX)


def test_gate_wiring_for_prefix_text():
    """接线锁：gate 真的把帧原文传给了 `_claim_issue`（漏传是静默的）。"""
    print("[cmd_prefix] gate → _claim_issue 的帧原文接线（结构锁）")
    import inspect
    src = inspect.getsource(gate_node)
    check("gate 里 `_claim_issue(...)` 调用带 frames_text=tool_text",
          "frames_text=tool_text" in src)
    check("`tool_text` 在 `_claim_issue` 调用**之前**就算好了（否则是 NameError）",
          src.index("tool_text = ") < src.index("_claim_issue("))


def main():
    for fn in (test_param_problem_corrected_in_round,
               test_gate_nav_arrival_without_nav_frame,
               test_cmd_prefix_fallback_truthful,
               test_gate_wiring_for_prefix_text):
        fn()
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
