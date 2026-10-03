# -*- coding: utf-8 -*-
"""假 LLM 桩：把夹具里现成的 `SKILL=/PARAMS=` 文本**翻译成 native tool_calls**。

**为什么需要它**（20261004）：planner 的接口层只剩 native tool calls 一条（见
`agent/native_plan.py` 与 `config/settings.py` 的注），而离线套件里十来个假 LLM 桩
是照着文本契约写的——`planner_node` 现在会 `bind_native(get_llm(...))`，桩缺
`bind_tools` 就直接 AttributeError。

两条路可选：① 把每个夹具的 `SKILL=/PARAMS=` 字面量改写成 `{"name":..., "args":...}`；
② 让桩自己翻译。选 ②，因为夹具里那些文本**不是"待迁移的历史包袱"**：它们是"模型想
做这件事"的最可读写法，而 `plan_encode`/`parse_plan` 那套内部协议（`SKILL=`/`PARAMS=`/
`TOOLS:`）**还在用**（execute/gate 读它）。把夹具改成 tool_calls 字面量，等于让每个
用例都手写一份 API 响应结构——多一层与判据无关的噪声。

**这个桩不替代判据**：它只做"文本 → 一次 tool_calls 响应"。读那一格的仍然是
`agent/native_plan.py::tool_calls_to_plan`（真的那个消费方），所以用例断言的
skill/params 差异仍然走生产路径。

## 用法

```python
from _native_stub import native_reply, bind_tools_stub

class _ScriptedLLM:
    bind_tools = bind_tools_stub          # 一行接上 bind_native

    def invoke(self, prompt):
        ...
        return native_reply(self.plans.pop(0))      # 原来返回 AIMessage(content=…)
```

`native_reply(text)` 认两种写法（与 `parse_plan` 的读端同款）：`SKILL:` / `SKILL=` 与
紧跟在后面的 `PARAMS:` / `PARAMS=` JSON。**文本里没有 SKILL 行** ⇒ 原样返回一条零
`tool_calls` 的响应（正文照旧进 `content`），这正是 native 契约里"一个函数都没点"的
那一格——新用例（`tests/test_native_wiring.py`）就是拿这个形状去测纠偏的。

⚠️ 别在这里加"按角色过滤技能"之类的智能：`tool_calls_to_plan` 自己会拿
`visible_skills(role)` 校验函数名（不在本轮 schema 里 ⇒ 返回 None ⇒ 确定性收尾）。
桩要是也过滤一遍，就变成两份判据，且夹具里写的技能名一旦对不上角色，红的是桩而不是
被验的那条路。
"""

from __future__ import annotations

import json
import re

from langchain_core.messages import AIMessage

# `SKILL: name` / `SKILL=name`（大小写不敏感，与 `parse_plan` 的读端同口径）
_SKILL_RE = re.compile(r"SKILL\s*[:=]\s*([A-Za-z_]\w*)", re.IGNORECASE)
_PARAMS_RE = re.compile(r"PARAMS\s*[:=]", re.IGNORECASE)


def bind_tools_stub(self, *args, **kwargs):
    """`llm.bind_tools(...)` → 自己（假模型没有真的 bind，记一笔方便断言）。"""
    self.bound_tools = args[0] if args else kwargs.get("tools")
    return self


def split_plan(text: str) -> tuple[str | None, dict]:
    """`SKILL=/PARAMS=` 文本 → `(技能名 or None, params)`。

    技能名或 PARAMS 缺失/坏掉**不抛**：返回 `(None, {})` 或 `(名字, {})`，让
    `native_reply` 按 native 契约原样交给下游判——坏掉的是夹具，不是被验的那条路。
    """
    m = _SKILL_RE.search(text or "")
    if not m:
        return None, {}
    skill = m.group(1)
    p = _PARAMS_RE.search(text or "", m.end())
    if not p:
        return skill, {}
    tail = (text or "")[p.end():]
    start = tail.find("{")
    if start < 0:
        return skill, {}
    try:                                    # 从第一个 `{` 起按 JSON 自己的边界断开
        obj, _end = json.JSONDecoder().raw_decode(tail[start:])
    except ValueError:
        return skill, {}
    return skill, (obj if isinstance(obj, dict) else {})


def native_reply(text: str, *, finish: str = "stop", call_id: str = "call_1") -> AIMessage:
    """一行夹具文本 → 一条 `AIMessage`（planner 决策轮的形状）。

    `finish` 让用例能演"被额度截断"那一轨（`finish="length"`，见
    `agent/native_plan.py` 对零调用 + length 的处置）。
    """
    skill, params = split_plan(text)
    if skill is None:
        # 没有 SKILL 行 = 零 tool_calls。**正文照旧带上**——这是"零调用但说了话"
        # 那一格（`undecided`），不是"响应不可解析"（那是空正文/坏 arguments）。
        return AIMessage(content=text or "", response_metadata={"finish_reason": finish})
    return AIMessage(
        content="",
        tool_calls=[{"name": skill, "args": params, "id": call_id, "type": "tool_call"}],
        response_metadata={"finish_reason": finish},
    )
