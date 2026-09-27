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

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项未通过：")
    for name in FAILED:
        print(f"   - {name}")
    sys.exit(1)
print("✅ 全部通过")
