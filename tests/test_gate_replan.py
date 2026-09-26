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
from agent.graph import build_graph, graph_input  # noqa: E402
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


# 两句措辞不同、但都属于"站内没有"结论的叙述——第二句刻意**不逐字重复**第一句，
# 否则先触发的会是复读闸（`repeat_prev_reply`），而它不在 `_REPLAN_ISSUES` 里，
# 用例就测不到"重规划只发生一次"这条（会变成"复读直接兜底"的另一回事）。
_LIE_1 = "抱歉喵，目前站内并没有关于或名为且关联西顿学院的详细文章记录。"
_LIE_2 = "站内暂时没有西顿学院相关的文章呢，要不要换个关键词试试？"
_TRUTH = "站内检索到 1 篇与西顿学院相关的文章：《西顿学院小记》。"

_PLAN_CHAT = "SKILL: chat\nPARAMS: {}"
_PLAN_SEARCH = ('SKILL: content_query\nPARAMS: {"calls": [{"tool": "search_notes", '
                '"args": {"keyword": "西顿学院"}}]}')


def _run(plans: list, narrations: list):
    """跑一遍真图，返回 (最终 state, 假 LLM, 假工具, trace 事件列表)。"""
    llm, tool, events = _ScriptedLLM(plans, narrations), _FakeTool(), []
    orig_llm, orig_record = g.get_llm, g.record
    orig_tool = g._TOOL_MAP.get("search_notes")
    g.get_llm = lambda **kw: llm
    g.record = lambda node, event, **data: events.append((node, event, data))
    g._TOOL_MAP["search_notes"] = tool
    try:
        cfg = {"configurable": {"thread_id": "t-gate-replan", "user_id": 5,
                                "principal": Principal(uid=5),
                                "conversation_id": 1, "stop_event": None}}
        out = build_graph().invoke(
            graph_input([HumanMessage(content="西顿学院")]), cfg)
    finally:
        g.get_llm, g.record = orig_llm, orig_record
        if orig_tool is None:
            g._TOOL_MAP.pop("search_notes", None)
        else:
            g._TOOL_MAP["search_notes"] = orig_tool
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


print()
if FAILED:
    print(f"❌ {len(FAILED)} 条未通过：")
    for _n in FAILED:
        print(f"   - {_n}")
    sys.exit(1)
print("✅ 全部通过")
