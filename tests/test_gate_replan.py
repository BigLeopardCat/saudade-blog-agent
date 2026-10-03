# -*- coding: utf-8 -*-
"""gate 打回 → 交回 planner 重规划一次（20260926，用户点名的现场）。

现场（trace `20260926T091548`，主人只说了「西顿学院」）：planner 落 `chat` 零工具，
narrator 写下「站内并没有关于或名为且关联西顿学院的详细文章记录」，gate 抓住
（洞④：站内"没有"的结论没有任何帧依据）——旧行为是**直接降级**：把那段叙述换成
一句道歉收尾，本轮就此结束。用户原话：「明明可以直接反馈给 planner，让 planner
重新规划，用户无感，而不是直接降级让用户看到道歉」。

本套件把那条真实路径在**真图**里跑完（假 LLM + 假工具，零网络零真写），锁四件事：
  ① 打回后真的回到 planner（不是终局兜底），重规划那一轮真的执行了检索工具；
  ② 被否定的叙述**从 state 里摘掉**（`RemoveMessage`）——它既不能留在最终回复里，
     也不能留成下一轮 narrator 的范文（20260920 那四代克隆链就是这么来的）；
  ③ 重规划**只发生一次**：第二次仍打回时回到确定性兜底，不无限循环；
  ④ `gate_replan` 在终局路径复位（否则 `route_after_gate` 会把收尾轮又送回 planner）。

20260930 补两节（同一条通道的另两个入口）：
  ④ **洞⑨ 那族**（"这一轮系统真的办成了：…已标记为已读"而无帧）也走重规划——治的是
     "拦住了谎，事还是没办成"。现场与判据见 `agent/graph.py::_WRITE_DONE_CLAIM_RE`。
  ⑤ **同步锁**：零帧轮声称表（`_zero_frame_families`）的每一族都得在 `_REPLAN_ISSUES`
     里。这两张表各写各的，20260930 之前洞⑨ 与第三人称取数那两族**上线时都漏挂了**
     ——拦住之后直接道歉收尾，而主人那一句本来是要它动手的。

用法：.venv/bin/python tests/test_gate_replan.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

# ── 仓根（同 tests/ 下其余套件：搬进 tests/ 后要靠这两行才 import 得到 agent/）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import base as _base  # noqa: E402  工具的返回值契约（ToolResult：str 子类 + kind）

import agent.graph as g  # noqa: E402
from agent.graph import build_graph, graph_input, parse_plan  # noqa: E402
from agent.principal import Principal  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# ── 脚本化的假 LLM：planner 与 model 共用 get_llm ────────────────────────────
# 按**入参形态**分流而不是按节点名：planner_node 传的是格式化好的提示词字符串，
# model_node 传的是 `[system] + state["messages"]` 列表（见 graph.py 两处调用点）。
# 这条分流判据一变（比如将来 planner 也传消息列表），下面的用例会立刻红——这正是
# 想要的：假 LLM 分不清节点时，"planner 被调了几次"的断言就会静默失去意义。
class _ScriptedLLM:
    def __init__(self, plans: list, narrations: list):
        self.plans, self.narrations = list(plans), list(narrations)
        self.planner_prompts: list[str] = []
        # 脚本用尽时**不抛异常**：planner 的 `except Exception` 会把它吞成"LLM 异常兜底
        # 收尾计划"，于是用例照样绿、却是在另一条路径上绿的（20260926 本套件首跑就踩了：
        # 第 3 轮规划根本没执行，是异常兜底替它产出的收尾计划）。改为记一笔 + 回一条
        # 无害的 chat 计划，每个用例末尾断言 `exhausted == []`——脚本够不够**是可验的**。
        self.exhausted: list[str] = []

    def invoke(self, prompt):
        if isinstance(prompt, list):
            if not self.narrations:
                self.exhausted.append("model")
                return AIMessage(content="（脚本用尽）")
            return AIMessage(content=self.narrations.pop(0))
        self.planner_prompts.append(prompt)
        if not self.plans:
            self.exhausted.append("planner")
            return AIMessage(content=_PLAN_CHAT)
        return AIMessage(content=self.plans.pop(0))


class _FakeTool:
    """假 search_notes：只记账、零网络。"""

    def __init__(self):
        self.name = "search_notes"
        self.calls: list = []

    def invoke(self, args):
        self.calls.append(args)
        return _base.ok("找到 1 篇：《西顿学院小记》（正文节选）")


class _FakeNoticeTool:
    """假 read_notifications：只记账、零网络（④ 的写族用）。

    返回的是**真改过**那一支（4 条被标掉，`meta` 里**没有** `noop` 证书）——这条
    分别正是 ⑥/⑥b/⑦/⑦b 四个用例唯一换的那一档。
    """

    def __init__(self):
        self.name = "read_notifications"
        self.calls: list = []

    def invoke(self, args):
        self.calls.append(args)
        return _base.ok("已把 4 条通知标记为已读（现在未读：通知 0 条 / 私信 0）。")


class _FakeOwnReadTool:
    """假 get_unread_summary：只记账、零网络（⑥ 的取数族用）。

    真工具以发起人身份读他自己的那一份（`scope=read.own`，未登录时工具层如实说
    "读不到"）——这里换掉它就是**为了零网络**：判据本身与工具无关，它只认"这一轮
    到底有没有一张帧"。

    它**不接受参数**（无参只读），所以记下来的是空参数表——⑥ 里"别给取数工具编参数"
    那条建议正是照它写的。
    """

    def __init__(self):
        self.name = "get_unread_summary"
        self.calls: list = []

    def invoke(self, args):
        self.calls.append(args)
        return _base.ok("你的未读：通知 2 条 / 私信 0 条。")


class _FakeNoopNoticeTool(_FakeNoticeTool):
    """假 read_notifications 的**零改动**档：`meta["noop"]` 证书在场。

    文案与 `tools/base.py::read_notifications` 那条真返回逐字同源（"本来就没有未读的"
    那一支）——`noop_specs` 这个事实通道认的就是 `meta["noop"]`，不是这句话本身。
    """

    def invoke(self, args):
        self.calls.append(args)
        return _base.ok("你的通知本来就没有未读的，无需改动（没有发出写请求）。",
                        meta={"op": "notice_read", "change": "本来就没有未读的",
                              "noop": True})


# 两句措辞不同、但都属于"站内没有"结论的叙述——第二句刻意**不逐字重复**第一句，
# 否则先触发的会是复读闸（`repeat_prev_reply`），而它不在 `_REPLAN_ISSUES` 里，
# 用例就测不到"重规划只发生一次"这条（会变成"复读直接兜底"的另一回事）。
_LIE_1 = "抱歉喵，目前站内并没有关于或名为且关联西顿学院的详细文章记录。"
_LIE_2 = "站内暂时没有西顿学院相关的文章呢，要不要换个关键词试试？"
_TRUTH = "站内检索到 1 篇与西顿学院相关的文章：《西顿学院小记》。"

_PLAN_CHAT = "SKILL: chat\nPARAMS: {}"
_PLAN_SEARCH = ('SKILL: content_query\nPARAMS: {"calls": [{"tool": "search_notes", '
                '"args": {"keyword": "西顿学院"}}]}')


def _run(plans: list, narrations: list, user_msg: str = "西顿学院",
         tool_name: str = "search_notes", fake=None, uid: int = 5):
    """跑一遍真图，返回 (最终 state, 假 LLM, 假工具, trace 事件列表)。

    `tool_name` / `fake` 让别的族复用同一台真图（④ 的写族打的是 `read_notifications`，
    不是检索工具）——**换的只是工具**：图、gate、路由全是生产那一份。
    `uid` 同理（⑥ 的守卫用例要 `uid=0` 的未登录档：那是 golden 里那两条
    `own_*_not_logged_in` 的身份，判据在该档下必须不开火）。
    """
    llm, tool, events = _ScriptedLLM(plans, narrations), (fake or _FakeTool()), []
    orig_llm, orig_record = g.get_llm, g.record
    orig_tool = g._TOOL_MAP.get(tool_name)
    g.get_llm = lambda **kw: llm
    g.record = lambda node, event, **data: events.append((node, event, data))
    g._TOOL_MAP[tool_name] = tool
    try:
        cfg = {"configurable": {"thread_id": "t-gate-replan", "user_id": uid,
                                "principal": Principal(uid=uid),
                                "conversation_id": 1, "stop_event": None}}
        out = build_graph().invoke(
            graph_input([HumanMessage(content=user_msg)]), cfg)
    finally:
        g.get_llm, g.record = orig_llm, orig_record
        if orig_tool is None:
            g._TOOL_MAP.pop(tool_name, None)
        else:
            g._TOOL_MAP[tool_name] = orig_tool
    return out, llm, tool, events


def _ai_text(state: dict) -> str:
    """最终 state 里所有 **AI 叙述**的正文（SystemMessage 不算：兜底文本会把被否定的
    那句话**引述**进去，那是如实告知的一部分，不是叙述本身）。"""
    return "\n".join(str(getattr(m, "content", ""))
                     for m in state["messages"] if isinstance(m, AIMessage))


def _rounds(llm: "_ScriptedLLM", n: int) -> list[str]:
    """第 n 轮规划时喂给 planner 的提示词（`round_info` 里写着"第 n/4 轮"）。"""
    return [p for p in llm.planner_prompts if f"第 {n}/{g.MAX_PLAN_ROUNDS} 轮" in p]


print("\n① 打回 → 回 planner 重规划，重查之后如实作答")
_out, _llm, _tool, _ev = _run([_PLAN_CHAT, _PLAN_SEARCH, _PLAN_CHAT], [_LIE_1, _TRUTH])
check("脚本足够跑完这一轮（没有一轮是靠「脚本用尽」混过去的）",
      _llm.exhausted == [], str(_llm.exhausted))
check("planner **真的重新决策了一次**（第 2 轮规划存在 = 不是兜底收尾）",
      len(_rounds(_llm, 2)) == 1, f"各轮次数={[len(_rounds(_llm, n)) for n in (1, 2, 3)]}")
check("  且重规划那一轮**看得见**打回原因（确定性提示进了提示词，不是只躺在消息流里）",
      bool(_rounds(_llm, 2)) and "已被系统否定" in _rounds(_llm, 2)[0],
      "第 2 轮提示词里找不到打回说明")
check("  第 1 轮（正常决策）**没有**这条提示——只有被打回的那一轮才有",
      bool(_rounds(_llm, 1)) and "已被系统否定" not in _rounds(_llm, 1)[0])
check("重规划那一轮真的执行了检索工具（不是又空跑一轮）",
      len(_tool.calls) == 1 and _tool.calls[0].get("keyword") == "西顿学院",
      str(_tool.calls))
check("最终回复是重查之后那条有依据的叙述",
      (_out["messages"][-1].content or "").strip() == _TRUTH,
      repr((_out["messages"][-1].content or "")[:40]))
check("被否定的那段叙述**不在**最终 state 里（不留成下一轮的范文）",
      _LIE_1 not in _ai_text(_out) and _LIE_1 not in "\n".join(
          str(getattr(m, "content", "")) for m in _out["messages"]))
check("没有走兜底（fallback_text 为空、done 为真）",
      not _out.get("fallback_text") and _out.get("done") is True)
check("gate_replan 已在终局路径复位", _out.get("gate_replan") is False)
check("trace 里有 gate/replan 事件（判据可回溯）",
      ("gate", "replan") in [(n, e) for n, e, _ in _ev],
      str([(n, e) for n, e, _ in _ev]))


print("\n② 重规划只发生一次：第二次仍打回 → 确定性兜底，不无限循环")
_out2, _llm2, _tool2, _ev2 = _run([_PLAN_CHAT, _PLAN_CHAT], [_LIE_1, _LIE_2])
check("脚本足够跑完这一轮", _llm2.exhausted == [], str(_llm2.exhausted))
check("没有第 3 轮规划（重规划不是每轮都发生）",
      len(_rounds(_llm2, 3)) == 0, f"各轮次数={[len(_rounds(_llm2, n)) for n in (1, 2, 3)]}")
check("第二次打回走兜底：final_reply 换成如实文本、done 收尾",
      bool(_out2.get("fallback_text")) and _out2.get("done") is True,
      repr(str(_out2.get("fallback_text") or "")[:40]))
# 注：兜底路径**刻意不摘掉**那条叙述消息（与 `_replan_result` 不同）——`_fallback_result`
# 是终局：这一轮到此结束、state 随请求丢弃，而服务端据末尾那条 `[Fallback 决定]` 把要
# 展示、要入库的回复**整段替换**成兜底文本（见 server.py 的 gate 分支）。重规划那条路
# 则**必须**摘掉，因为那一轮还要继续跑——留着它就成了下一轮 planner/narrator 的范文。
# 所以这里锁的是"末端那条决定帧在场"（服务端唯一认的替换凭据），不是"假话不在 state 里"。
check("  末尾那条 `[Fallback 决定]` 在场（服务端据此替换回复的唯一凭据）",
      str(_out2["messages"][-1].content).startswith("[Fallback 决定]"),
      repr(str(_out2["messages"][-1].content)[:30]))
check("  这次走的是兜底分支而不是又一次重规划（trace 里 fallback 事件在场）",
      any(e == "fallback" for _n, e, _d in _ev2),
      str([(n, e) for n, e, _ in _ev2]))
check("  gate_replan 复位（否则 route_after_gate 会把收尾轮又送回 planner）",
      _out2.get("gate_replan") is False)
check("  两次打回只有一条 replan 事件（后一条是 fallback）",
      sorted(e for _n, e, _d in _ev2 if e in ("replan", "fallback")) == ["fallback", "replan"],
      str([(n, e) for n, e, _ in _ev2]))


print("\n③ 正常通过的一轮不受影响（零回归：没有 replan 事件、没有打回提示）")
_out3, _llm3, _tool3, _ev3 = _run([_PLAN_SEARCH, _PLAN_CHAT], [_TRUTH])
check("脚本足够跑完这一轮", _llm3.exhausted == [], str(_llm3.exhausted))
check("检索工具执行了一次", len(_tool3.calls) == 1)
check("没有 replan 事件（这条通道只在打回时开）",
      "replan" not in [e for _n, e, _d in _ev3])
check("提示词里不带任何打回说明", all(
    "已被系统否定" not in p for p in _llm3.planner_prompts))
check("最终回复就是那条叙述", (_out3["messages"][-1].content or "").strip() == _TRUTH)


print("\n④ 洞⑨ 那族也走重规划：拦住谎之后**把事办成**，而不是就此道歉收尾")
# 现场（trace `20260930T123938`，uid=1 真主人）：主人说「我的未读信息全部就标记为
# 已读」（一条明确的祈使写请求），planner 判 chat 零工具，narrator 回「这一轮系统真的
# 办成了：你（id=1）的未读站内信已全部标记为已读」——那句还是**抄的**紧邻上一条真办成
# 那轮的句式。洞⑨ 拦住了这句谎；这一段锁的是拦住之后那一步：不打回重规划的话，主人
# 看到的是"我刚才那句是编的……要不要我真的去办一遍"——谎没了，事还是没办。
_LIE_WRITE = ("主人，这一轮系统真的办成了：你的未读站内信已全部标记为已读，"
              "现在未读是 **0 封**，列表清干净了喵～")
_TRUTH_WRITE = "主人，4 条未读通知已经真的标记为已读了喵。"
_PLAN_NOTICE = 'SKILL: notice_read\nPARAMS: {"all": true}'
_out4, _llm4, _tool4, _ev4 = _run([_PLAN_CHAT, _PLAN_NOTICE, _PLAN_CHAT],
                                  [_LIE_WRITE, _TRUTH_WRITE],
                                  user_msg="猫咪我的未读信息全部就标记为已读",
                                  tool_name="read_notifications",
                                  fake=_FakeNoticeTool())
check("脚本足够跑完这一轮", _llm4.exhausted == [], str(_llm4.exhausted))
check("planner **真的重新决策了一次**（第 2 轮规划存在 = 不是道歉收尾）",
      len(_rounds(_llm4, 2)) == 1, f"各轮次数={[len(_rounds(_llm4, n)) for n in (1, 2, 3)]}")
check("重规划那一轮真的把写工具执行了（不是又空跑一轮）——事办成了",
      _tool4.calls == [{"all": True}], str(_tool4.calls))
check("  打回提示给的是**写族**的出路（去动手），不是检索族那套（去查）",
      bool(_rounds(_llm4, 2)) and "该动手就去动手" in _rounds(_llm4, 2)[0]
      and "选检索类技能" not in _rounds(_llm4, 2)[0],
      (": ".join(_rounds(_llm4, 2)[0].splitlines()[-4:])[:160]
       if _rounds(_llm4, 2) else "第 2 轮提示词不存在"))
check("最终回复是重办之后那条有依据的叙述",
      (_out4["messages"][-1].content or "").strip() == _TRUTH_WRITE,
      repr((_out4["messages"][-1].content or "")[:40]))
check("被否定的那句**不在**最终 state 里（不留成下一轮的范文）",
      _LIE_WRITE not in _ai_text(_out4))
check("没有走兜底（fallback_text 为空、done 为真）",
      not _out4.get("fallback_text") and _out4.get("done") is True)
check("trace 里有 gate/replan 事件（判据可回溯）",
      ("gate", "replan") in [(n, e) for n, e, _ in _ev4],
      str([(n, e) for n, e, _ in _ev4]))

print("\n⑤ 同步锁：零帧轮声称表的每一族都得挂号（新加一张网忘了加 _REPLAN_ISSUES = 这里红）")
# 两张表各写各的：`_zero_frame_families` 是"有哪些网"，`_REPLAN_ISSUES` 是"哪些网
# 打回后值得交回 planner"。20260930 实测两族**上线时都漏挂了**（洞⑨ 与第三人称取数
# 声称）——拦住之后一步兜底、主人看到的是一句道歉，而那句话正是要它动手的。
# 锁成"每一族都在"而不是"两边相等"：有帧轮的族（phantom_* 等）本来就不在这张零帧表里。
_ZF = {f.issue for f in g._zero_frame_families({}, "chat")}
check("零帧轮声称表非空（表被改名/搬走时这条先红，别让下面那条空转）", len(_ZF) >= 5,
      str(sorted(_ZF)))
check("零帧轮声称表的每一族都在 _REPLAN_ISSUES 里",
      _ZF <= set(g._REPLAN_ISSUES),
      "漏挂：" + "、".join(sorted(_ZF - set(g._REPLAN_ISSUES))))
check("_REPLAN_ADVICE 的键都是 _REPLAN_ISSUES 的成员（挂在非成员上 = 读不到的死代码）",
      set(g._REPLAN_ADVICE) <= set(g._REPLAN_ISSUES),
      "、".join(sorted(set(g._REPLAN_ADVICE) - set(g._REPLAN_ISSUES))))
check("建议措辞真按族发：写族拿「去动手」、检索族拿「去查」（接错表 = planner 被指错路）",
      "该动手就去动手" in g._replan_note("sys_write_claim_without_tool", "x")
      and "选检索类技能" not in g._replan_note("sys_write_claim_without_tool", "x")
      and "选检索类技能" in g._replan_note("site_absence_claim_without_tool", "x"))
check("  没写建议的族退回缺省那一份（新增 issue 不会拿到半截提示）",
      "选检索类技能" in g._replan_note("这个 issue 不存在", "x"))
check("  洞⑩ 的否定说明**不跟着缺省那句走**（它说的是「这一轮真执行过」，"
      "而缺省那句写的是「一个工具都没有执行」——当着 planner 的面说反话）",
      "真的执行过" in g._replan_note("write_change_denial", "x")
      and "一个工具都没有执行" not in g._replan_note("write_change_denial", "x"))

print("\n⑥ 洞⑩ 走重规划：真改了东西却说「这一轮什么都没改」 ⇒ 打回重说（用户上线后报告）")
# 现场（trace `20260930T192729_1`，uid=1 真主人）：主人点「确定」那一轮，
# `read_notifications` **真执行**、回执写着「已把 1 条通知标记为已读（现在未读：通知 0 条）」，
# narrator 却说「通知这边其实本来就没有未读的，所以这一轮没有可标记的、什么都没改」——
# 主人刚亲手点的确定，被告知站内什么都没发生。根因是**技能回复契约**里那句可抄的否认句
# （已同步改措辞），判据是兜底：措辞只降概率，兑现轮说反话必须拦得住。
_LIE_NOCHANGE = "通知这边其实本来就没有未读的，所以这一轮没有可标记的、什么都没改喵。"
_out6, _llm6, _tool6, _ev6 = _run([_PLAN_NOTICE, _PLAN_CHAT, _PLAN_CHAT],
                                  [_LIE_NOCHANGE, _TRUTH_WRITE],
                                  user_msg="猫咪我的未读信息全部就标记为已读",
                                  tool_name="read_notifications",
                                  fake=_FakeNoticeTool())
check("脚本足够跑完这一轮", _llm6.exhausted == [], str(_llm6.exhausted))
check("写**真的执行了**（这是本用例的前提：判定要的是「真有改动」)",
      _tool6.calls == [{"all": True}], str(_tool6.calls))
check("planner **真的重新决策了一次**（第 3 轮规划存在 = 不是兜底道歉收尾）",
      len(_rounds(_llm6, 3)) == 1, f"各轮次数={[len(_rounds(_llm6, n)) for n in (1, 2, 3)]}")
check("  打回提示给的是洞⑩ 那一族（「照回执如实说、不用再执行一次」），不是检索族",
      bool(_rounds(_llm6, 3)) and "不需要再执行一次" in _rounds(_llm6, 3)[0]
      and "选检索类技能" not in _rounds(_llm6, 3)[0])
check("最终回复是重说之后那条有依据的叙述",
      (_out6["messages"][-1].content or "").strip() == _TRUTH_WRITE,
      repr((_out6["messages"][-1].content or "")[:40]))
check("被否定的那句**不在**最终 state 里（不留成下一轮的范文）",
      "没有可标记的" not in _ai_text(_out6))
check("没有走兜底（fallback_text 为空、done 为真）",
      not _out6.get("fallback_text") and _out6.get("done") is True)
check("trace 里有 gate/replan 事件（判据可回溯）",
      ("gate", "replan") in [(n, e) for n, e, _ in _ev6],
      str([(n, e) for n, e, _ in _ev6]))

print("\n⑥b 负锁：零改动的那一轮说「本来就没有」**不打回**（门要认得工具自报的 noop 证书）")
# 与 ⑥ 只差一件事：工具返回的是**零改动**（`meta["noop"]`）。此时 narrator 说的
# 「本来就没有未读的、没有可标的」是**真话**——门绝不能把如实说明当成谎打回，
# 否则诚实叙述会被反复重写（这正是加 `noop_specs` 这个事实通道的全部理由）。
_NOOP_TEXT = "主人，你的通知本来就没有未读的，所以这一轮没有可标的、什么都没改喵～"
_out6b, _llm6b, _tool6b, _ev6b = _run([_PLAN_NOTICE, _PLAN_CHAT], [_NOOP_TEXT],
                                      user_msg="猫咪我的未读信息全部就标记为已读",
                                      tool_name="read_notifications",
                                      fake=_FakeNoopNoticeTool())
check("脚本足够跑完这一轮", _llm6b.exhausted == [], str(_llm6b.exhausted))
check("工具真跑了（零改动也是执行过）", len(_tool6b.calls) == 1, str(_tool6b.calls))
check("**没有**打回（零改动轮的如实说明不算改动否认）",
      ("gate", "replan") not in [(n, e) for n, e, _ in _ev6b]
      and ("gate", "fallback") not in [(n, e) for n, e, _ in _ev6b],
      str([(n, e) for n, e, _ in _ev6b]))
check("  最终回复就是那句如实说明（没被兜底换掉）",
      (_out6b["messages"][-1].content or "").strip() == _NOOP_TEXT)

print("\n⑦ 零改动重复不再重跑（用户原话：「第二轮连调三次工具」）")
# 现场（trace `20260930T192824`）：`add_favorite(23)` 零改动返回后，planner **拿不到
# "目标已达成"这个事实**，连规划 4 轮同一件、每轮各执行一次（每次都是零改动），
# 12.5 秒 / 5 次 LLM 换来一个"什么都没发生"。判据 = 工具自报的 noop 证书（execute 落进
# `noop_specs`）⇒ 同一件零改动的 spec 第二次点名不再执行。
_out7, _llm7, _tool7, _ev7 = _run([_PLAN_NOTICE, _PLAN_NOTICE, _PLAN_CHAT],
                                  [_NOOP_TEXT],
                                  user_msg="猫咪我的未读信息全部就标记为已读",
                                  tool_name="read_notifications",
                                  fake=_FakeNoopNoticeTool())
check("脚本足够跑完这一轮", _llm7.exhausted == [], str(_llm7.exhausted))
check("同一件零改动的写**只执行了一次**（第 2 轮规划被确定性地剔掉了）",
      len(_tool7.calls) == 1, f"执行了 {len(_tool7.calls)} 次：{_tool7.calls}")
check("  拦截事件落了 trace（reason=noop_repeat，判据可回溯）",
      ("planner", "intercept") in [(n, e) for n, e, _ in _ev7]
      and any(e == "intercept" and d.get("reason") == "noop_repeat"
              for n, e, d in _ev7),
      str([(n, e, d.get("reason")) for n, e, d in _ev7 if e in ("intercept",)]))
check("  收尾注记把「零改动」说清楚（narrator 才不会两头都敢说）",
      "状态本来就是目标值" in (parse_plan(_out7.get("plan", ""))["note"] or ""),
      repr((parse_plan(_out7.get("plan", ""))["note"] or "")[:60]))
check("最终回复是那句如实说明",
      (_out7["messages"][-1].content or "").strip() == _NOOP_TEXT)

print("\n⑦b 负锁：**真改过**的同一件 spec 不受这条裁剪管辖（换的是事实，不是工具名）")
# 与 ⑦ 只差工具返回：这次是真的标了 4 条。第二轮的同一件 spec **照旧放行**——
# 裁剪键的是工具自己声明的"零改动"，不是"这个签名出现过"（后者会把"再来一次"
# 这种合法的新请求一并吞掉）。
_out7b, _llm7b, _tool7b, _ev7b = _run([_PLAN_NOTICE, _PLAN_NOTICE, _PLAN_CHAT],
                                      [_TRUTH_WRITE],
                                      user_msg="猫咪我的未读信息全部就标记为已读",
                                      tool_name="read_notifications",
                                      fake=_FakeNoticeTool())
check("脚本足够跑完这一轮", _llm7b.exhausted == [], str(_llm7b.exhausted))
check("真改过的那件第二轮**照旧执行**（只有零改动的才被剔）",
      len(_tool7b.calls) == 2, f"执行了 {len(_tool7b.calls)} 次")

print("\n⑧ 判据的两个零件各自可验（纯函数，不经过图）")
# `_has_real_change` 是**前提**：有写回执、且那条回执不在 noop 里。
_SIG = ["add_favorite", '{"article_id": "23"}']
check("  写回执 + 不在 noop 里 ⇒ 真有改动",
      g._has_real_change([{"tool": "add_favorite", "args": {"article_id": 23}}], []))
check("  同一条回执但工具自报零改动 ⇒ **不算**真有改动",
      not g._has_real_change([{"tool": "add_favorite", "args": {"article_id": 23}}], [_SIG]))
check("  只有只读回执 ⇒ 不算真有改动（读不改变任何东西）",
      not g._has_real_change([{"tool": "search_notes", "args": {"keyword": "x"}}], []))
check("  `noop_specs` 缺失（老 state / 未声明）⇒ 写回执全按「真改动」从严判",
      g._has_real_change([{"tool": "add_favorite", "args": {"article_id": 23}}], None))
check("  谓词的**前提**真的挡在前面：没有真改动时，同一句否认不成立",
      g._change_denial_claim("这一轮什么都没改喵", True)
      and not g._change_denial_claim("这一轮什么都没改喵", False))
check("  多件轮里如实说**其中一件**没改不在此列（无本轮作用域标记）",
      not g._change_denial_claim("额度那条没有改动，留言那条已经通过了", True))
check("  逐字段的如实报告不在此列（「颜色：这次没改」——作用域是那一格、不是整轮）",
      not g._change_denial_claim("**颜色**：这次没改，名字和父级都已经换好了", True)
      and not g._change_denial_claim("- **颜色**：这次没改", True)
      and not g._change_denial_claim("这次跳转没有带动画", True))
check("  但带量词的整轮否认照样命中（裸「改」只是被量词锚定，不是被删掉）",
      g._change_denial_claim("这次一个字节都没改喵", True)
      and g._change_denial_claim("这一轮什么都没改", True))
check("  引号里转述不算 narrator 自己的声称（调用方剥引号后判）",
      not g._change_denial_claim(g._strip_quoted_spans("留言里写着「这一轮什么都没改」"),
                                 True))
# `_trim_noop_specs`：签名按 `_spec_signature` 归一（与回执侧同源）。
_P70 = {"skill": "notice_read", "tools": ["read_notifications({'all': True})"],
        "params": {"all": True}, "note": ""}
check("  签名在 noop 里 ⇒ 剔除",
      g._trim_noop_specs(_P70, [list(g._spec_signature(
          "read_notifications", {"all": True}))]) is not None)
check("  签名不在 noop 里 ⇒ 一件都不剔（返回 None，计划原样走）",
      g._trim_noop_specs(_P70, [list(g._spec_signature(
          "read_notifications", {"all": False}))]) is None)
check("  剔空之后 PARAMS 同步剔（两行不一致 = narrator 只能猜到底做了没有）",
      (g._trim_noop_specs(
          {"skill": "notice_read", "tools": ["read_notifications({'all': True})"],
           "params": {"all": True, "tools": ["read_notifications",
                                             "list_notifications"]}, "note": ""},
          [list(g._spec_signature("read_notifications", {"all": True}))])[0]["params"]
       .get("tools")) == ["list_notifications"])


print("\n⑥ 零帧纯作答轮 + 主人在问**自己那份数据** → 打回重规划（20261003 补的那半）")
# 现场（uid=1 会话 320，trace `20261003T194144`；下面两段取自那份 trace 原文的**开头**）：
# 主人问「我有哪些未读通知呀」，planner 落 chat 零工具，narrator 回了一段**关于上一轮话题
# （翻服务日志 / worker respawn）的真话**。上面每一族问的都是"这句话真不真"——那段话字字
# 属实，于是原先直接 gate PASS（zero_frame=True）。这一节锁的就是补上的那半：**这轮答的是
# 不是主人刚问的那件事**（判据与射程数据见 `agent/authz.py::is_own_read_question` 的头注）。
_LIE_OWN = ("主人，这一轮我得跟你说实话喵——我手上没有“翻原始日志文件”的工具，"
            "journalctl / rust.log / agent.log 的原文我这轮取不到，所以没法把这次 "
            "respawn 的具体堆栈贴给你看。")
_TRUTH_OWN = "主人，你现在有 2 条未读通知喵～（私信 0 条）"
_PLAN_OWN = ('SKILL: content_query\nPARAMS: {"calls": [{"tool": "get_unread_summary", '
             '"args": {}}]}')
_out6, _llm6, _tool6, _ev6 = _run([_PLAN_CHAT, _PLAN_OWN, _PLAN_CHAT],
                                  [_LIE_OWN, _TRUTH_OWN],
                                  user_msg="小猫咪！我有哪些未读通知呀",
                                  tool_name="get_unread_summary",
                                  fake=_FakeOwnReadTool())
check("脚本足够跑完这一轮", _llm6.exhausted == [], str(_llm6.exhausted))
check("planner **真的重新决策了一次**（不是直接兜底道歉）",
      len(_rounds(_llm6, 2)) == 1, f"各轮次数={[len(_rounds(_llm6, n)) for n in (1, 2, 3)]}")
check("重规划那一轮真的把取数工具执行了（不是又空跑一轮）",
      _tool6.calls == [{}], str(_tool6.calls))
check("打回提示给的是**取数族**的出路（这几个工具不需要参数），不是检索族那套",
      bool(_rounds(_llm6, 2)) and "不需要参数" in _rounds(_llm6, 2)[0]
      and "选检索类技能" not in _rounds(_llm6, 2)[0],
      (": ".join(_rounds(_llm6, 2)[0].splitlines()[-4:])[:160]
       if _rounds(_llm6, 2) else "第 2 轮提示词不存在"))
check("最终回复是重查之后那条有依据的叙述",
      (_out6["messages"][-1].content or "").strip() == _TRUTH_OWN,
      repr((_out6["messages"][-1].content or "")[:40]))
check("被否定的那段**不在**最终 state 里（不留成下一轮的范文）",
      _LIE_OWN not in _ai_text(_out6))
check("没有走兜底（fallback_text 为空、done 为真）",
      not _out6.get("fallback_text") and _out6.get("done") is True)
check("trace 里 replan 的 issue 就是本族（判据可回溯）",
      any(d.get("issue") == "own_read_question_without_tool"
          for _n, e, d in _ev6 if e == "replan"),
      str([(e, d.get("issue")) for _n, e, d in _ev6 if e in ("replan", "fallback")]))

print("\n⑥b 守卫：这些轮**不许**开火（开火了就是把本来对的回答换成兜底）")
_NEUTRAL = "嗯嗯，这个我知道喵～"


def _no_fire(user_msg: str, uid: int = 5):
    """跑一轮 chat/answer_only（触发形状与⑥完全一致，只换消息或 uid），返回 (事件名, state)。"""
    _o, _l, _t, _e = _run([_PLAN_CHAT], [_NEUTRAL], user_msg=user_msg, uid=uid)
    return [e for _n, e, _d in _e], _o


_e, _o = _no_fire("我有哪些未读通知？", uid=0)
check("uid=0（未登录）：「读不到你自己的数据」本身就是如实的答案 ⇒ 不开火",
      "replan" not in _e and "fallback" not in _e, str(_e))
check("  且那一轮照常 PASS 收尾（golden 的 `own_*_not_logged_in` 两条正是这一档）",
      not _o.get("fallback_text") and (_o["messages"][-1].content or "") == _NEUTRAL)
for _msg, _why in [("你可以单独把一个消息标记已读吗", "能力问句：如实答「不能」就是出路"),
                   ("猫咪我的未读信息全部就标记为已读", "写命令：归洞⑨（写侧那族）"),
                   ("站内最近有什么公告吗？", "公开面（golden `data_announcements` 同形）"),
                   ("读取站内通知读不到吗", "「通知」是通用词、句里没有自指"),
                   ("小猫咪你会做蛋糕吗", "闲聊")]:
    _e, _o = _no_fire(_msg)
    check(f"不开火：{_msg}（{_why}）",
          "replan" not in _e and "fallback" not in _e, str(_e))

print("\n⑥c 源码锁：开火点在零帧块里，三张表都挂了号，兜底文案不许带读数")
_GSRC_OWN = Path(g.__file__).read_text(encoding="utf-8")
_OWN_CALL = "authz.is_own_read_question(_last_user_msg(msgs))"
check("判据接在 gate 里（源码里认得出这个调用点）", _OWN_CALL in _GSRC_OWN)
check("  且排在 `if not frames:` **之后**（有帧 = 这一轮真取过数了，不该被这条拦）",
      _GSRC_OWN.index("if not frames:") < _GSRC_OWN.index(_OWN_CALL))
check("家族在 `_REPLAN_ISSUES` / `_REPLAN_ADVICE` / `_REPLAN_WHY` 三张表里都挂了号",
      "own_read_question_without_tool" in g._REPLAN_ISSUES
      and "own_read_question_without_tool" in g._REPLAN_ADVICE
      and "own_read_question_without_tool" in g._REPLAN_WHY)
check("  否定说明**不跟缺省那句走**（缺省写的是「那条结论没有任何依据」，"
      "而本族的前提是「句句属实但答错了题」）",
      "没有去取" in g._replan_note("own_read_question_without_tool", "x")
      and "没有任何依据" not in g._replan_note("own_read_question_without_tool", "x"))
check("兜底文案如实说「没去取」，且**不含任何读数**（走到兜底时一个字节都没取到）",
      "没有去取你自己的数据" in g._FALLBACK_OWN_READ
      and not any(k in g._FALLBACK_OWN_READ for k in ("0 条", "0 封", "没有未读")))

print()
if FAILED:
    print(f"❌ {len(FAILED)} 条未通过：")
    for _n in FAILED:
        print(f"   - {_n}")
    sys.exit(1)
print("✅ 全部通过")
