# -*- coding: utf-8 -*-
"""`with_tool_call_pairs` 的离线单测（20260928）。

**为什么这个补形状的函数要有测试**：它坐在 narrator 的**唯一一次 LLM 调用**前面，
判据全是"形状"——而形状错了不出声：少补一条，qwen 照旧能答（它容忍非法序列），
只有换成严格服务商才会整轮 400；多补一条（对本来合法的序列再补一遍），则是在
给模型看一段它没做过的调用。两种错都**照样跑绿**，所以这里把三个形态钉死：

  ① 孤儿帧被认领（`tool_calls[].id` 与帧自己的 `tool_call_id` 逐字相同）；
  ② 连续多条合进**同一条** assistant（= 一轮并行调用，正是它们的来源）；
  ③ **幂等**：紧跟在已认领 assistant 后面的帧组原样透过、不再补。

实测背景（`eval/dial_matrix.py` 的 `native-nothink-deepseek` 档注里有详述）：
qwen 端点容忍 `role:"tool"` 前面没有 `tool_calls` 的非法序列，deepseek 一律 400。

秒级、纯数据，不联网、不跑 LLM。
用法：.venv/bin/python tests/test_tool_call_pairs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from agent.context import with_tool_call_pairs  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def frame(name: str, idx: int, text: str = "已执行") -> ToolMessage:
    """execute_node 造帧的同一形状：`tool_call_id=f"execute_{idx}"` + `name`。"""
    return ToolMessage(content=text, tool_call_id=f"execute_{idx}", name=name)


print("① 孤儿帧被认领")
msgs = [HumanMessage(content="帮我开樱花特效"), frame("toggle_effect", 0)]
out = with_tool_call_pairs(msgs)
check("多出一条 assistant，位置在帧之前",
      [type(m).__name__ for m in out] == ["HumanMessage", "AIMessage", "ToolMessage"],
      str([type(m).__name__ for m in out]))
ai = out[1]
check("tool_calls 的 id 就是帧自己那个（服务商靠它配对）",
      [tc["id"] for tc in ai.tool_calls] == ["execute_0"], str(ai.tool_calls))
check("函数名取自帧的 name", [tc["name"] for tc in ai.tool_calls] == ["toggle_effect"],
      str(ai.tool_calls))
check("assistant 正文为空——补的是形状，不是给模型加话",
      not ai.content, repr(ai.content))
check("帧对象本身没被改动（同一份材料照旧进提示词）",
      out[2] is msgs[1] and out[2].content == "已执行")

print("\n② 连续多条合进同一条 assistant")
msgs = [HumanMessage(content="带我去留言板"), frame("navigate_to", 0),
        frame("toggle_effect", 1)]
out = with_tool_call_pairs(msgs)
check("只多一条 assistant（= 一轮并行调用）",
      [type(m).__name__ for m in out] ==
      ["HumanMessage", "AIMessage", "ToolMessage", "ToolMessage"],
      str([type(m).__name__ for m in out]))
check("两个 id 都在同一条上",
      [tc["id"] for tc in out[1].tool_calls] == ["execute_0", "execute_1"],
      str(out[1].tool_calls))

print("\n③ 幂等：本来合法的序列原样透过")
legal = [HumanMessage(content="带我去留言板"),
         AIMessage(content="", tool_calls=[{"name": "navigate_to", "args": {},
                                            "id": "execute_0", "type": "tool_call"}]),
         frame("navigate_to", 0)]
again = with_tool_call_pairs(legal)
check("不再补第二条 assistant（补过头 = 给模型看它没做过的调用）",
      [type(m).__name__ for m in again] ==
      ["HumanMessage", "AIMessage", "ToolMessage"], str([type(m).__name__ for m in again]))
check("跑两遍与跑一遍结果同形", with_tool_call_pairs(again) == again or
      [type(m).__name__ for m in with_tool_call_pairs(again)] ==
      [type(m).__name__ for m in again])
part = [HumanMessage(content="x"),
        AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "execute_0",
                                          "type": "tool_call"}]),
        frame("a", 0), frame("b", 1)]
out = with_tool_call_pairs(part)
check("只认领了一半的帧组照样补（配对逐条对，不是「前一条是 assistant 就算」）",
      [type(m).__name__ for m in out] ==
      ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage", "ToolMessage"],
      str([type(m).__name__ for m in out]))
check("被补的那条只认领没被认领的帧（认领过的前缀原样透过，不重复挂）",
      [tc["id"] for tc in out[3].tool_calls] == ["execute_1"], str(out[3].tool_calls))

print("\n④ 零帧 / 纯历史：一个字节都不动")
plain = [HumanMessage(content="你好"), AIMessage(content="喵～")]
check("没有 ToolMessage 时原样返回（引用相等，连拷贝都不做）",
      with_tool_call_pairs(plain) == plain and len(with_tool_call_pairs(plain)) == 2)
check("空列表不炸", with_tool_call_pairs([]) == [])
check("末尾是帧时也收尾（循环后必须 flush 一次）",
      len(with_tool_call_pairs([HumanMessage(content="x"), frame("t", 0)])) == 3)

print("\n⑤ 跨轮唯一性：id 是**请求内**的身份，不是本轮位次（20261006）")
# 这一节治的是"形状函数的输入"：`task_id` 一旦在同一请求里重复，服务商就拒收整条
# 序列（`Duplicate value for 'tool_call_id' of execute_0`，原话见 graph.py::_frame_id）。
# 上面四节全部照样通过——它们每次只喂**一轮**的帧，而这正是它在生产里藏了 8 天的原因：
# 判据（以及 qwen 端点）都只看一轮。
from langchain_core.messages import ToolMessage as _TM  # noqa: E402

from agent.graph import _frame_id  # noqa: E402


def dup_ids(msgs: list) -> list[str]:
    """一条序列里重复的配对 id。

    **两侧各数各的**（不是把两处加起来）：一个 id 本来就该出现两次——一次在
    assistant 的 `tool_calls` 里，一次在那条帧自己的 `tool_call_id` 上。加起来数会把
    **每一对合法配对**都读成重复，这条判据就成了恒红。
    """
    declared: dict[str, int] = {}
    framed: dict[str, int] = {}
    for m in msgs:
        for tc in (getattr(m, "tool_calls", None) or []):
            k = str(tc.get("id"))
            declared[k] = declared.get(k, 0) + 1
        if isinstance(m, _TM):
            k = str(getattr(m, "tool_call_id", "") or "")
            framed[k] = framed.get(k, 0) + 1
    return sorted(k for k in set(declared) | set(framed)
                  if declared.get(k, 0) > 1 or framed.get(k, 0) > 1)


def round_frames(prev: list, tools: list[str], old: bool = False) -> list:
    """一轮 execute 产出的帧。`old=True` 复刻修前口径（逐轮从 0 数）。"""
    out = []
    for i, t in enumerate(tools):
        fid = f"execute_{i}" if old else _frame_id(prev, i)
        out.append(_TM(content=f"{t} 已完成", tool_call_id=fid, name=t))
    return out


# 修前现场：两轮各一条（第 2 轮是 planner 受阻后重决策，最常见）。
_PREV = [HumanMessage(content="帮我删掉标签「大笨狗」")]
_R1_OLD = round_frames(_PREV, ["list_tags"], old=True)
_R2_OLD = round_frames(_PREV + _R1_OLD, ["list_tags"], old=True)
check("★ 正控（修前口径）：两轮并进一条 assistant 后**确实**出现重复 id"
      "——这条不红，下面那条绿就是假的",
      dup_ids(with_tool_call_pairs(_PREV + _R1_OLD + _R2_OLD)) == ["execute_0"],
      str(dup_ids(with_tool_call_pairs(_PREV + _R1_OLD + _R2_OLD))))

_R1 = round_frames(_PREV, ["list_tags"])
_R2 = round_frames(_PREV + _R1, ["delete_tag"])
_WIRE = with_tool_call_pairs(_PREV + _R1 + _R2)
check("修后：同一条序列喂进去，一个重复都没有",
      dup_ids(_WIRE) == [], str(dup_ids(_WIRE)))
check("  两轮的帧仍并进同一条 assistant（补形状的语义没变，只换 id）",
      [type(m).__name__ for m in _WIRE] ==
      ["HumanMessage", "AIMessage", "ToolMessage", "ToolMessage"],
      str([type(m).__name__ for m in _WIRE]))
check("  配对仍然成立：每条帧的 id 都被它前面那条 assistant 声明过",
      [str(tc["id"]) for tc in _WIRE[1].tool_calls] ==
      [str(getattr(m, "tool_call_id", "")) for m in _WIRE[2:]],
      str([tc["id"] for tc in _WIRE[1].tool_calls]))
check("  id 单调可读（第 2 轮接着第 1 轮往下数，不回退）",
      [str(getattr(m, "tool_call_id", "")) for m in _R1 + _R2] ==
      ["execute_0", "execute_1"],
      str([getattr(m, "tool_call_id", "") for m in _R1 + _R2]))

# 同一轮里多条并行帧：base 对整轮是常量、靠 idx 分开（别把 base 也当成逐帧累加）。
_R3 = round_frames(_PREV + _R1 + _R2, ["a", "b", "c"])
check("同一轮 3 条并行帧互不相同，且接在已有帧之后",
      [str(getattr(m, "tool_call_id", "")) for m in _R3] ==
      ["execute_2", "execute_3", "execute_4"],
      str([getattr(m, "tool_call_id", "") for m in _R3]))
check("入参不被改动（纯函数；`state[\"messages\"]` 是别人的）",
      _frame_id(_PREV + _R1, 0) == "execute_1" and len(_PREV) == 1)
check("空/无帧序列不炸", _frame_id([], 0) == "execute_0"
      and _frame_id(None, 2) == "execute_2")

# 生产端的接线：`execute_node` 必须**调**这个函数，不能把 `execute_{idx}` 写回来。
_SRC = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
check("接线：帧的 id 由 `_frame_id` 出（写回 `execute_{idx}` 会被这条抓住）",
      "tool_call_id=_frame_id(state.get(\"messages\"), idx)" in _SRC)
check("  旧的逐轮位次写法在源码里已经不存在",
      'tool_call_id=f"execute_{idx}"' not in _SRC)

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项未通过：")
    for name in FAILED:
        print(f"   - {name}")
    sys.exit(1)
print("✅ 全部通过")
