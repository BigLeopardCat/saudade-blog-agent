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
   改判成"**回执里有同一条命令**"——本文件的四组用例就是那四处判据的回归锁，
   夹具一并换成结构化形（夹具不换，判据就是哑的：它照样会绿）。
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
    _claim_issue, gate_node, parse_plan, plan_encode, planner_node,
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


def main():
    for fn in (test_param_problem_corrected_in_round,
               test_gate_nav_arrival_without_nav_frame,
               test_cmd_prefix_fallback_truthful,
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
