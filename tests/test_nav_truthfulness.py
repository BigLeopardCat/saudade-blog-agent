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

   20260926 批 2 起（命令与事实分离）：连线命令从工具帧搬到了**回执行**的 `cmd`
   字段，工具帧只剩「页面已跳转：<url>」这样给人看的事实。所以"帧里有同一个载荷"
   改判成"**回执里有同一条命令**"——本文件前四组用例就是那四处判据的回归锁，
   夹具一并换成结构化形（夹具不换，判据就是哑的：它照样会绿）。

③ **做了、但还没做完就说做完了**（trace 20261001T230706，主人报「声称转跳完成是在
   转跳前完成…等了一会出现转跳完成文本才转跳」）：整页目标（`/device-console/`）由
   nginx 直服、不在 React 路由表里，SPA 桥不接管 ⇒ 只能整页装载；而整页装载掐断本轮
   SSE、Rust 在终止帧才落库 ⇒ **必须等回复说完才能跳**（提前跳 = 这一轮回复从对话里
   消失）。所以时序不改、改字：这类目标说"即将跳转"，站内页照旧说"已跳转"（那类是
   桥当场换路由、陈述与动作同一时刻）。第五、六组用例锁这两向，外加"回执里的 cmd
   一字不动"。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.graph import (  # noqa: E402
    _FALLBACK_CMD_PREFIX, _FALLBACK_CMD_PREFIX_DONE, _FALLBACK_NAV_NO_FRAME,
    _cmd_prefix_corroborated, _cmd_prefix_fallback_text, _cmd_wire, _cmd_wires,
    _claim_issue, _live_page_path, gate_node, parse_plan, plan_encode, planner_node,
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
D1_NAV_FRAME = "页面已跳转：https://saudade.site/article/46"
D1_NAV_CMD = {"kind": "navigate", "url": "https://saudade.site/article/46", "mode": "direct"}


def _nav_receipt(cmd=D1_NAV_CMD):
    """checker PASS 后落到 state["receipts"] 的那一行（命令在 cmd 字段里）。"""
    return {"skill": "navigate", "tool": "navigate_to", "cmd": cmd}


# ── D4 洞⑪ 现场（trace 20261002T020256，逐字）──────────────────────────────
# 主人「猫咪带我去你的设计文档」（站内**没有**这个页面）→ planner 选 chat/answer_only
# （零帧零回执）→ narrator 回下面这句 → gate 判 PASS。**真值就在同一轮**：page_ctx 的
# `page=` 是首页（`https://saudade.site/`）。三张现成的网都在测词形（见 graph.py 洞⑪
# 头注），没有一条去核它。
D4_REPLY = "主人，物联网平台页面已经打开啦～你现在应该能看到设备控制台了喵！"
D4_PAGE_CTX = ("user_id=1, page=https://saudade.site/, title=Saudade Blog; "
               "current_effects=none; current_darkmode=on")


def _sys_msg(ctx=D4_PAGE_CTX):
    """`_build_messages` 写进消息流的那条 `[System: …]`（`_page_ctx` 的取值处）。"""
    return HumanMessage(content=f"[System: {ctx}]")


def _chat_state(msgs, **kw):
    """零帧 chat 轮的 gate 输入（洞⑪ 的判据只跑在零帧族里）。`gate_replan=True`
    = 重规划那次已经用掉 ⇒ 打回走确定性兜底，断言能直接读到兜底文本。"""
    st = {"plan": plan_encode(instantiate_plan("chat", {})), "messages": msgs,
          "done": False, "plan_rounds": 2, "gate_replan": True}
    st.update(kw)
    return st


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
        check("  注记**不含**「不调用任何工具」那句（旧版那是 NAV_MAP 注记轮的判据，"
              "套上去会得到一句『那个页面不存在』的假话）",
              "不调用任何工具" not in plan2["note"])
        # 20260926 批 3：判据从"那句措辞"改成 plan["status"]——所以这里断言的是
        # 状态值（收尾轮 = wrapped），措辞只是随它一起去的东西。上一行留着是**反向**
        # 证据：措辞仍在，但它已经不再是判据了（"注意到措辞还在"不等于"判据还在"）。
        check("  收口计划的状态是 wrapped（判据读状态，不读那句措辞）",
              plan2["status"] == "wrapped", plan2["status"])
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
    # 用例前提（20260926 批 3 改）：旧版这里断言的是"注记里带那句措辞"——那是当时
    # 的判据。现在判据是 status，所以前提也该是状态值：参数不齐 ⇒ param_missing。
    # 这个用例本身判的是**导航到达声称**那条（与状态无关），前提只是"计划是真形状"。
    check("用例前提：navigate 参数不齐 ⇒ status=param_missing",
          parse_plan(_plan)["status"] == "param_missing",
          parse_plan(_plan)["status"])

    def _state(msgs, plan=_plan, receipts=None):
        st = {"plan": plan, "messages": msgs, "done": False, "plan_rounds": 2}
        if receipts is not None:
            st["receipts"] = receipts
        return st

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
    o2 = gate_node(_state([human, nav_frame, AIMessage(content=D1_REPLY)],
                          receipts=[_nav_receipt()]))
    check("回执里**有**导航命令 → 放行（真跳过就不该拦）",
          not o2.get("fallback_text"), str(o2)[:80])
    # 判据的凭据在回执、不在帧文本：只有无前缀的事实帧而没有回执行时**仍判无导航帧**
    # ——这一条正是"改了实现忘了改夹具会让测试继续绿"的哨兵
    o2b = gate_node(_state([human, nav_frame, AIMessage(content=D1_REPLY)]))
    check("  只有事实帧、回执里没有命令 → 仍判无导航帧（凭据在回执）",
          o2b.get("fallback_text") == _FALLBACK_NAV_NO_FRAME,
          str(o2b.get("fallback_text"))[:50])
    o3 = gate_node(_state([human, list_frame,
                           AIMessage(content="站内没有这个页面，我没法带你过去呀～")]))
    check("如实措辞 → 放行（不误伤）", not o3.get("fallback_text"), str(o3)[:80])
    o4 = gate_node(_state([human, list_frame, AIMessage(content="好的主人，我看看～")]))
    check("没有到达声称 → 放行", not o4.get("fallback_text"), str(o4)[:80])


def test_cmd_prefix_fallback_truthful():
    """③ 做了却说没做：命令前缀的兜底文案按**回执**对账（D2 现场）。"""
    print("[cmd_prefix] 前缀被打回时的如实兜底（回执里真有那串命令）")
    d2_clause = ("主人，这一轮系统确实执行了跳转，回执里写着 navigate_to 把路径 "
                 "/article/46 打开了（AUTO_NAVIGATE:https://saudade.site/article/46）")
    wires = _cmd_wires([_nav_receipt()])

    check("回执行 → 连线形（cmd 搬上回执后由 _cmd_wires 重建，跨语言契约的这一半）",
          wires == ["AUTO_NAVIGATE:https://saudade.site/article/46"], str(wires))
    check("  读帧失败/没有 cmd 的回执行 → 重建出空清单（不编命令）",
          _cmd_wires([{"tool": "list_notes", "result": "1. 标题"}, {}, "脏行"]) == [])
    check("  三种命令各自的重建形",
          [_cmd_wire({"kind": "effect", "effect": "sakura", "action": "on"}),
           _cmd_wire({"kind": "darkmode", "mode": "off"}),
           _cmd_wire({"kind": "navigate", "url": "https://saudade.site/talk",
                      "mode": "confirm"})]
          == ["EFFECT:sakura:on", "DARKMODE:off", "NAVIGATE:https://saudade.site/talk"])

    got = _cmd_prefix_corroborated(d2_clause, wires)
    check("回执里有同一个载荷 → 认出来", got == ("AUTO_NAVIGATE",
                                                  "https://saudade.site/article/46"),
          str(got))
    check("回复里有、回执里没有 → 不认（回复正是不可信的那一侧）",
          _cmd_prefix_corroborated(d2_clause, ["AUTO_NAVIGATE:https://saudade.site/guestbook"])
          is None)
    check("只有前缀没有载荷（模型自己敲了个 NAVIGATE:）→ 不认",
          _cmd_prefix_corroborated("我这就 NAVIGATE: 过去", wires) is None)

    txt = _cmd_prefix_fallback_text(d2_clause, wires)
    check("兜底换成『真做过了』那一版", txt == _FALLBACK_CMD_PREFIX_DONE.format(
        what="页面已经开到 https://saudade.site/article/46"), txt[:60])
    check("  说清了那件事已发生（不再说『我让系统执行给你看』）",
          "页面已经开到" in txt and "真做过的" in txt)
    check("  文案里**没有**命令前缀标签（禁的是标签，不是事实）",
          "AUTO_NAVIGATE:" not in txt and "NAVIGATE:" not in txt)
    check("回执里对不上 → 退回通用文案（原行为）",
          _cmd_prefix_fallback_text("我这就 AUTO_NAVIGATE:https://evil.example/x",
                                    wires) == _FALLBACK_CMD_PREFIX)
    check("特效/夜间两族也各有如实说法",
          "特效 sakura 已经打开" in _cmd_prefix_fallback_text(
              "现在 EFFECT:sakura:on 了", ["EFFECT:sakura:on"])
          and "夜间模式已经关闭" in _cmd_prefix_fallback_text(
              "DARKMODE:off 了", ["DARKMODE:off"]))

    # 接线锁：`_claim_issue` 必须把回执传下来（漏传 = 永远走通用文案，静默）
    hit = _claim_issue(d2_clause, "content_query", {"note": "", "tools": []}, True,
                       receipts=[_nav_receipt()])
    check("`_claim_issue` 的命令前缀那一支会用回执选文案",
          hit is not None and hit[0] == "cmd_prefix"
          and hit[1] == _FALLBACK_CMD_PREFIX_DONE.format(
              what="页面已经开到 https://saudade.site/article/46"), str(hit)[:80])
    hit2 = _claim_issue(d2_clause, "content_query", {"note": "", "tools": []}, True)
    check("  默认实参 = 通用文案（旧调用点行为不变）",
          hit2 is not None and hit2[1] == _FALLBACK_CMD_PREFIX)


def test_whole_page_target_not_claimed_as_done():
    """③ **还没跳就别说跳完了**（trace 20261001T230706，主人报「声称转跳完成是在转跳前完成」）。

    现场：t≈3.226 执行 `navigate_to("/device-console/")`，t≈3.236 系统印出
    「页面已跳转：…」，而页面在 t≈9.412（回复写完、Rust 落库）才真的动——中间 6 秒
    主人盯着一句**说它已经做了那件它还没做的事**的系统事实。

    时序不是缺陷、是硬约束：整页目标（不在 React 路由表里 ⇒ SPA 桥不接管）只能整页
    装载，而整页装载掐断本轮 SSE、Rust 在终止帧才落库、断连的残缺回复被 Drop guard
    清掉 ⇒ 提前跳 = 这一轮回复从对话里消失。所以改的是**字**：这类目标说"即将跳转"。

    三向锁：① 整页目标不许出现"已跳转"、必须带 URL（rule 6 取值锚点）；② 站内页
    （SPA 桥当场换路由，陈述与动作同一时刻）**照旧**说"已跳转"——放宽不许扩散；
    ③ 清单自洽：整页集合必须是合法导航路径的子集，且回执里的 cmd 一字不动
    （机器侧仍然只有一种形状，措辞只影响给人看的那一半）。
    """
    print("[whole-page] 整页目标不许说「已跳转」")
    from tools.base import _NAV_EXACT_PATHS, _NAV_WHOLE_PAGE_PATHS, navigate_to

    check("整页集合非空且 ⊆ 合法路径（写错一个字符就是永远命不中的死分支）",
          bool(_NAV_WHOLE_PAGE_PATHS) and _NAV_WHOLE_PAGE_PATHS <= set(_NAV_EXACT_PATHS),
          str(sorted(_NAV_WHOLE_PAGE_PATHS)))

    # 前面那轮真跑过的两个目标：一个整页、一个站内（t=3.226 的现场参数原样）
    for p in sorted(_NAV_WHOLE_PAGE_PATHS):
        r = navigate_to.invoke({"path": p, "confirm": False})
        check(f"整页目标 {p} 说的是「即将跳转」，不是「已跳转」",
              str(r).startswith("页面即将跳转：") and "已跳转" not in str(r),
              str(r))
        check("  留住人类可读 URL（rule 6 的取值锚点）",
              f"https://saudade.site{p}" in str(r))
        check("  回执里的 cmd 与站内页**同形**（机器读的那一半一个字不改）",
              r.meta.get("cmd") == {"kind": "navigate",
                                    "url": f"https://saudade.site{p}",
                                    "mode": "direct"},
              str(r.meta.get("cmd")))

    r_spa = navigate_to.invoke({"path": "/talk", "confirm": False})
    check("站内页（SPA 当场换路由）照旧说「页面已跳转」——放宽不许扩散",
          str(r_spa) == "页面已跳转：https://saudade.site/talk", str(r_spa))
    r_cfm = navigate_to.invoke({"path": "/talk", "confirm": True})
    check("确认式照旧说「等主人确认」——三态各有各的字，不共用",
          "等主人确认" in str(r_cfm) and "已跳转" not in str(r_cfm), str(r_cfm))


def test_nav_family_not_printed_into_fact_block():
    """接线锁：**命令族不进用户可见事实块**（20261002 主人拍板）——跳转的交代归泠月自己说。

    主人原话：「不要显示〔系统〕 页面即将跳转：https://saudade.site/device-console/
    （本条回复说完再跳）显示，**到了后让agent自己回答**」。那份"（本条回复说完再跳）"
    是**系统在解释自己的时序**，读起来像机房播报而不是回应。

    但**事实供给一个字不能少**，所以这条是反向断言（正断言"没印"很容易写成"没供给"）：
    ① 工具帧文本照旧（模型看到的证据不变，narrator 才答得出"带你过去了"）；
    ② 分族仍是命令族（`is_action_family` 为真——`eval/narrator_facts_share.py` 的量化
       口径按分族算，"这一轮有几个动作"与"气泡里印了几行"不是一个数）；
    ③ 只有 `is_block_family` 为假（过滤发生在出口，不在分类）。
    """
    print("[whole-page] 命令族不进事实块（交代归 narrator）")
    from agent.factblock import (
        action_facts, block_of, is_action_family, is_block_family,
    )
    from tools.base import navigate_to

    r0 = navigate_to.invoke({"path": "/device-console/", "confirm": False})
    # 回执的形状照 execute 的实产：工具帧文本 + 顶层 cmd（`family_of` 靠它认命令族）
    receipt = {"skill": "navigate", "tool": "navigate_to", "result": str(r0),
               "cmd": r0.meta.get("cmd")}
    check("工具帧文本仍是那句（模型看到的证据不变，别把过滤挪进工具）",
          str(receipt["result"]).startswith("页面即将跳转："), str(receipt["result"]))
    check("  分族**仍是命令族**（量化口径按分族算，改印量不许顺手改掉它的含义）",
          is_action_family(receipt), "is_action_family 应为真")
    check("  但**不进**用户可见事实块（气泡里不再有〔系统〕那一行）",
          not is_block_family(receipt) and action_facts([receipt]) == []
          and block_of([receipt]) == "",
          str(action_facts([receipt])))


def test_gate_wiring_for_prefix_text():
    """接线锁：gate 真的把回执传给了 `_claim_issue`（漏传是静默的）。"""
    print("[cmd_prefix] gate → _claim_issue 的回执接线（结构锁）")
    import inspect
    src = inspect.getsource(gate_node)
    check("gate 里 `_claim_issue(...)` 调用带 receipts=receipts",
          "receipts=receipts" in src)
    check("`receipts` 在 `_claim_issue` 调用**之前**就算好了（否则是 NameError）",
          src.index("receipts = [") < src.index("_claim_issue("))
    check("  帧文本不再当命令凭据传给 `_claim_issue`（旧接线残留会静默 fail-open）",
          "frames_text=" not in src)


def test_cmd_frame_wiring():
    """④ 批次 2 的**接线锁**：`__CMD__` 帧在四端各接了一次，漏一处全是静默坏路。

    这不是"能力测试"而是接线测试——本仓吃过亏：`graph.py` 顶部加一句
    `from __future__ import annotations` 让节点里的断连检查静默失效（能力有测试、
    接线没有测试），只留一句 UserWarning。跨端只接一半是同一类洞：不报错、不抛异常，
    只是某个方向上的功能凭空消失。所以这里读源码钉住四处：
      · producer（`_run_agent_stream_to_queue`）**必须**发这个帧——golden 直接 drain
        的就是它的队列，只在 event_stream 发的话 golden 的 `commands` 恒为空；
      · event_stream 必须带 `data: ` 前缀转发 + 置 `nav_line`（决定 `__NAV_END__`）；
      · `run_golden` 必须把结构化 cmd 重建成连线形进 `commands`，且位置在那条
        `item.startswith("__")` 兜底**之前**（否则帧被无声吞进 control_frames）；
      · 非流式（`_run_agent_sync`）用同一个 `_cmd_wire` 重建，不自己再写一份。
    """
    print("[__CMD__] 四端接线（源码锁：漏一处都不报错）")
    root = Path(__file__).resolve().parent.parent
    srv = (root / "server.py").read_text(encoding="utf-8")
    gold = (root / "eval" / "run_golden.py").read_text(encoding="utf-8")

    i_put = srv.find('queue.put("__CMD__:"')
    i_stream = srv.find("async def event_stream")
    check("producer 发 `__CMD__`（golden 直接 drain 的就是它的队列）", i_put > 0)
    check("  且它在 event_stream **之前**（不是只有消费端接了）",
          i_put > 0 and i_stream > i_put)

    i_cons = srv.find('chunk.startswith("__CMD__:")')
    seg = srv[i_cons:i_cons + 1200] if i_cons > 0 else ""
    check("消费端认这个帧", i_cons > 0)
    check("  带 `data: ` 前缀转发（Rust 的 SSE 解析是 strip_prefix(b\"data: \")）",
          'yield f"data: {chunk}\\n\\n"' in seg)
    check("  置 nav_line（否则收尾发 __END__ 而不是 __NAV_END__）",
          "nav_line = chunk" in seg)
    check("  置 pending_nl（命令帧独占一行，否则行级过滤把整行剥空）",
          "pending_nl" in seg)
    check("非流式用同一个 `_cmd_wire` 重建（不是各写一份）",
          '_cmd_wire(r.get("cmd"))' in srv and "from agent.graph import" in srv
          and "_cmd_wire" in srv.split("from agent.graph import")[1].split("\n")[0])

    i_cmd = gold.find('item.startswith("__CMD__:")')
    i_catch = gold.find('elif isinstance(item, str) and item.startswith("__")')
    check("golden 把 __CMD__ 重建成连线形进 commands（既有 114 条断言零编辑的前提）",
          i_cmd > 0 and "_cmd_wire(_cmd)" in gold[i_cmd:i_cmd + 1800])
    check("  且分支在 `item.startswith(\"__\")` 兜底**之前**（否则被无声吞掉）",
          i_cmd > 0 and i_catch > i_cmd)


def test_gate_nav_present_claim_verified_against_page_ctx():
    """④ 洞⑪（20261002 02:02）：零帧轮的「主人现在在 X 页」→ 与 page= **真值**核对。

    这一族与前三条的关系：①②③ 判的都是**词形**（"已经带你跳到"长什么样），本族判的是
    **真值**（前端实时上报的位置）。所以它的回归锁必须**双向**：
      · 反例面：真值说主人在首页，回复却说"你现在能看到设备控制台了" ⇒ 判假；
      · 正例面：真值**就是**那一页（主人自己点过去的）⇒ 同一句话必须放行。
    只锁反例面的话，"把词形放得足够宽"也能让测试变绿——那正是这一族要离开的路。
    """
    print("[洞⑪] '主人现在在 X 页' 与 page= 真值核对（20261002 02:02 现场）")
    human = HumanMessage(content="猫咪带我去你的设计文档")

    # ── 反例（现场）：真值 = 首页，回复说"能看到设备控制台了" ⇒ 判假 ────────
    st = _chat_state([_sys_msg(), human, AIMessage(content=D4_REPLY)])
    o = gate_node(st)
    check("现场句（page=首页 / 回复称已在设备控制台）→ 兜底",
          o.get("done") is True and o.get("fallback_text") == _FALLBACK_NAV_NO_FRAME,
          str(o.get("fallback_text"))[:40])
    iss = _claim_issue(D4_REPLY, "chat", parse_plan(st["plan"]), False,
                       page_ctx=D4_PAGE_CTX)
    check("  issue = nav_present_claim_without_nav（分族记，能按族统计）",
          bool(iss) and iss[0] == "nav_present_claim_without_nav",
          str(iss and iss[0]))
    check("  被否掉的子句 = 那一句（进 trace；不是整段也不是空）",
          bool(iss) and "设备控制台" in iss[2] and "首页" not in iss[2],
          str(iss and iss[2])[:60])

    # ── 重规划通道：第一次打回交回 planner（不是直接兜底），且提示带两条出路 ──
    o2 = gate_node(_chat_state([_sys_msg(), human, AIMessage(content=D4_REPLY)],
                               gate_replan=False))
    check("首次打回 → 交回 planner 重规划一次（gate_replan=True、done=False）",
          o2.get("gate_replan") is True and o2.get("done") is False, str(o2.get("done")))
    _note = "".join(str(getattr(m, "content", "")) for m in (o2.get("messages") or []))
    check("  提示里点明'主人现在在哪一页'是系统事实（不是模型能安排的）",
          "系统事实" in _note and "page=" in _note, _note[:60])
    check("  提示里的禁止句在（不许说'页面已经打开了'）",
          "页面已经打开了" in _note)

    # ── 正例面：真值一致 ⇒ 同一句话放行（幂等轮的正确答案）──────────────
    o3 = gate_node(_chat_state([
        _sys_msg("user_id=1, page=https://saudade.site/guestbook; current_effects=none"),
        human, AIMessage(content="主人，您现在就在留言板呀～")]))
    check("真值 = 留言板 + 回复'您现在就在留言板' → 放行（幂等轮不误伤）",
          o3.get("done") is True and not o3.get("fallback_text"), str(o3)[:60])

    # ── 没有真值 ⇒ 整条判据不跑（宁漏勿误伤）────────────────────────────
    o4 = gate_node(_chat_state([human, AIMessage(content=D4_REPLY)]))
    check("页面上下文缺席（无 [System:] 那条）→ 不判，放行",
          o4.get("done") is True and not o4.get("fallback_text"), str(o4)[:60])
    o5 = gate_node(_chat_state([
        _sys_msg("user_id=1, page=about:blank; current_darkmode=off"),
        human, AIMessage(content=D4_REPLY)]))
    check("`page=` 解不出站内路径（about:blank）→ 不判，放行（无从核对 ≠ 判假）",
          o5.get("done") is True and not o5.get("fallback_text"), str(o5)[:60])

    # ── 三处刻意不收（实测出来的误伤面，改词形时别把它们放进来）──────────
    _page_home = "user_id=1, page=/, current_effects=none"
    o6 = gate_node(_chat_state([
        _sys_msg(_page_home), human,
        AIMessage(content="物联网平台在 /device-console/，点顶部菜单就过去了")]))
    check("**指路句**（未来式、在教路怎么走）→ 放行",
          o6.get("done") is True and not o6.get("fallback_text"), str(o6)[:60])
    o7 = gate_node(_chat_state([
        _sys_msg(_page_home), human,
        AIMessage(content="你现在能在物联网平台控制 OLED 屏幕")]))
    check("**介词短语**（'在物联网平台控制 X' 不是位置谓语）→ 放行",
          o7.get("done") is True and not o7.get("fallback_text"), str(o7)[:60])
    o8 = gate_node(_chat_state(
        [_sys_msg(_page_home), human, AIMessage(content="刚才已经带你到物联网平台了")],
        ledger={"executions": [{"detail": "跳转「/device-console/」"}]}))
    check("**追述**（回执在场 + 追述时间词 = 引台账）→ 放行",
          o8.get("done") is True and not o8.get("fallback_text"), str(o8)[:60])

    # ── 再三处（都是标定电池里实测出来的翻车点，改这张表/这个词形先看这里）──
    o9 = gate_node(_chat_state([
        _sys_msg(_page_home), human,
        AIMessage(content="主人刚才在留言板留的那条话我看到了")]))
    check("**过去式**（说的是主人**过去**在哪）→ 放行：本族只管'现在在哪'，"
          "判时态靠窗口里的纯过去词（`_NAV_PAST_ONLY_RE`），不靠回执在场那条追述豁免",
          o9.get("done") is True and not o9.get("fallback_text"), str(o9)[:60])
    o10 = gate_node(_chat_state([
        _sys_msg(_page_home), human,
        AIMessage(content="主人，你不在留言板呀，你在首页")]))
    check("**否定位置**（'你不在留言板'是句真话）→ 放行（`不` 划出了位置谓语的间隙，"
          "否则'你**不**在X'会被读成'你在X'）",
          o10.get("done") is True and not o10.get("fallback_text"), str(o10)[:60])
    o11 = gate_node(_chat_state([
        _sys_msg("user_id=1, page=https://saudade.site/guestbook; current_darkmode=off"),
        human, AIMessage(content="主人，你现在在首页刷文章呢")]))
    check("**陈述句里的语气词「呢」**（宿主真值=留言板）→ 兜底：'呢'不在豁免表里"
          "（它当疑问词时本来就不需要豁免——那种句子里没有页面名）",
          o11.get("done") is True and o11.get("fallback_text") == _FALLBACK_NAV_NO_FRAME,
          str(o11.get("fallback_text"))[:40])

    # ── ⑫ 条件句（20261002 04:02 实测误伤：那句话的前提是"一旦"）─────────────
    # 现场（golden trace `20261002_040233/admin_announcement_question_no_popup`，
    # `replan` 事件的 `clause` 字段**逐字**）：主人（管理员）问「把公告删掉的话，访客
    # 那边还看得到吗？」，回复里有一句「**一旦**你在 **站点设置-公告管理**
    # （/dashboard/announcement）里执行了删除操作」→ 判 `nav_present_claim_without_nav`
    # 打回 → 交回 planner 重规划一次（14.68s / 705 输出 token）→ 最终答复只剩 7 个字。
    # 病根：豁免表的**条件类**只写了「如果/若是/要是/若」，而"一旦/倘若/假如/除非"同属
    # 条件——**假设句里出现页面名，说的不是主人此刻在哪页**。锁法照本族纪律：放行面
    # （条件句）＋**反向对照**（同一句抽掉条件词 ⇒ 当场判假，证明这放行是那颗词给的）。
    _cond = ("一旦你在 **站点设置-公告管理**（/dashboard/announcement）"
             "里执行了删除操作，公告就会从列表里消失")
    o12 = gate_node(_chat_state([_sys_msg(_page_home), human, AIMessage(content=_cond)]))
    check("**条件句**（'一旦你…里执行了操作'是假设，不是主人现在在哪）→ 放行",
          o12.get("done") is True and not o12.get("fallback_text"), str(o12)[:60])
    o13 = gate_node(_chat_state([
        _sys_msg(_page_home), human,
        AIMessage(content=_cond.replace("一旦", ""))]))
    check("  反向对照：抽掉条件词「一旦」⇒ 当场判假（放行是那颗词给的，"
          "不是这句话本来就判不了）",
          o13.get("fallback_text") == _FALLBACK_NAV_NO_FRAME,
          str(o13.get("fallback_text"))[:40])
    _missing_cond = [w for w in ("一旦", "倘若", "假如", "除非")
                     if not G._NAV_PRESENT_EXEMPT_RE.search(w)]
    check("  条件词一族齐全（一旦/倘若/假如/除非，一个都不能少——"
          "同一类词缺一个，这一类就等于没写）", not _missing_cond, str(_missing_cond))

    # ── ⑬ 裸「已」的名词化（20261003 族 3 复扫：5 例打回里 4 例是这一个字造的）────
    # 打回原句（`replan` 事件的 clause 字段逐字，golden `capability_list_user_no_admin_leak`
    # 三个 run 三次同一句 + `admin_capability_absent_honest` 一次）：能力清单里的
    # 「标记为**已读**（需要你先**登录**哦）」与「他所有**已登录**的会话会立刻失效」——
    # 窗口判据把 **已读/已登录** 这个复合词里的"已"当成了完成态标记，又逮到后面那个
    # 10 字内的"登录"，于是"清单里列了一条能力"被读成"主人现在在登录页"。与
    # `_STATE_ACTION_EXEMPT_RE` 里裸「未」的收窄同一条道理（未读/未知/未审/未阅/未免）。
    # 锁法照本族纪律：放行面（四句现场）＋反向对照（真完成态必须照旧判假）。
    _cap_lines = (
        "或者帮你标记站内通知/私信为已读（需要你先登录哦）",
        "他所有已登录的会话会立刻失效",
        "* **通知管理**：把你收到的站内信或通知标记为已读（需要你登录账号才行喵）",
        "* **消息管理**：把你收到的站内通知或私信标记为已读（同样需登录）",
    )
    for _t in _cap_lines:
        o = gate_node(_chat_state(
            [_sys_msg(_page_home), human, AIMessage(content=_t)]))
        check(f"**能力清单**（已读/已登录 是名词不是完成态）→ 放行：{_t[:22]}",
              o.get("done") is True and not o.get("fallback_text"),
              str(o.get("fallback_text"))[:40])
    o14 = gate_node(_chat_state([
        _sys_msg(_page_home), human,
        AIMessage(content="主人，留言板页面已经打开啦～你现在应该能看到那些留言了喵")]))
    check("  反向对照：真完成态（已经 + 位置动词）照旧判假（收窄只放名词化的那六个）",
          o14.get("fallback_text") == _FALLBACK_NAV_NO_FRAME,
          str(o14.get("fallback_text"))[:40])
    # 逐词判别锁：每个名词化的「已+X」后面**紧跟一个位置动词**时都不许命中
    # （不加 `X看到` 的话这条会假绿——裸词本来就没有位置动词可匹配，
    #   这正是"判'没有'要构造出能命中的形态"那条纪律）。反向：真完成态必须命中。
    _still_wide = [w for w in ("已读", "已登录", "已登陆", "已知", "已审", "已阅", "已免")
                   if G._NAV_PRESENT_WINDOW_RE.search(w + "看到")]
    check("  收窄面齐全（已读/已登录/已登陆/已知/已审/已阅/已免 —— 各加一个位置动词后"
          "一个都不许命中）", not _still_wide, str(_still_wide))
    check("  反向：真完成态「已经登录」「已经打开」照旧命中（收窄只吃名词化的那六个）",
          bool(G._NAV_PRESENT_WINDOW_RE.search("已经登录"))
          and bool(G._NAV_PRESENT_WINDOW_RE.search("已经打开")))

    # ── `page=` 的解析：绝对 URL / query / fragment / 尾斜杠都要归一────────
    check("绝对 URL → 站内路径（去 query/fragment/尾斜杠）",
          _live_page_path("page=https://saudade.site/device-console/#a?b=1")
          == "/device-console")
    check("相对路径与根路径都认（/ 仍是 /）",
          _live_page_path("page=/guestbook") == "/guestbook"
          and _live_page_path("page=/") == "/")
    check("畸形/缺席 ⇒ None（调用方据此不判）",
          _live_page_path("（无）") is None and _live_page_path("page=") is None)


def main():
    for fn in (test_param_problem_corrected_in_round,
               test_gate_nav_arrival_without_nav_frame,
               test_cmd_prefix_fallback_truthful,
               test_whole_page_target_not_claimed_as_done,
               test_nav_family_not_printed_into_fact_block,
               test_gate_nav_present_claim_verified_against_page_ctx,
               test_gate_wiring_for_prefix_text,
               test_cmd_frame_wiring):
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
