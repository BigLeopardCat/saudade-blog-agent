# -*- coding: utf-8 -*-
"""工具级菜单（POC，20261020）：模型**直接选工具**时看到的那张表。

**与 `agent/planner_llm.py::build_tool_schema` 的关系**：那张是**技能级**的
（`visible_workflows(role) × workflow_param_specs`，`function name` = 技能名），本模块是
**工具级**的（`tools/base.py::get_all_tools()`，`function name` = 真工具名）。两者输出
**同形**（OpenAI `tools` 数组），因此可以喂同一个 `bind_tools` —— 这是"菜单形状"能被
单独拨动的前提（POC 要的就是这个自变量）。

三条纪律：

① **角色过滤不写第二份名单**：判据只有一处——`authz.required_scope(tool) ∈
   authz.scopes_for(role)`，与 `graph.execute_node` 调用前那一道 `authz.check` **同源**。
   于是"菜单里能选的"恒 ⊆ "执行期放行的"，**不扩权是结构性的**，不是靠提示词请求
   （同 `planner_llm` 头注那条"schema 与菜单同源"）。`role=None` 时 `scopes_for` 回空集
   ⇒ 一个工具都选不出来（fail-closed，与执行期同取向）。

② **描述不抄第二份**：planner 面的人工说明仍住在 `graph._TOOL_MENU_LINES`（唯一事实源），
   本模块只**读**它；缺省回退工具自己的 docstring。那一批说明里写着工具之间的分工
   （"两张报表不是一张纸"那类），正是"菜单少了它模型会点错"的地方。

③ **跨切面槽位照搬**（`intents` / `reply_kind` / `evidence` / 三个伪函数）：它们**不是
   模板资产**，是 harness 资产——消费方分别是 `tasks.normalize_intents`、
   `prompt_context.parse_reply_kind`、`provenance`、`tasks`。不搬就等于把"菜单形状"与
   "少了一条自我申报通道"混成一个变量，那正是 A/B 最忌的那种混淆。

**边界（别过度承诺）**：本模块只管"给模型看的那张表"。它不判权限（判在
`authz.check`）、不执行、不产回执——那些都在下游，与菜单形状无关。
"""

from __future__ import annotations

from typing import Any

from agent import authz
from agent.graph import _TOOL_MENU_LINES
from agent.prompt_context import REPLY_KIND_ARG, REPLY_KIND_FIELD_DESC, reply_kind_prop_schema
from agent.provenance import EVIDENCE_ARG, EVIDENCE_FIELD_DESC, evidence_prop_schema
from agent.tasks import INTENTS_ARG, INTENTS_FIELD_DESC, intents_prop_schema, pseudo_tool_schemas
from tools.base import get_all_tools


def all_tool_names() -> list[str]:
    """注册表里的全部工具名（`tools/base.py` 是业务唯一数据源）。"""
    return [t.name for t in get_all_tools()]


def visible_tools(role: str | None) -> list[str]:
    """这个身份能选的**工具名**，按名字升序（顺序确定 ⇒ 菜单逐字节可复现）。

    判据只有 `authz` 的 scope 表一处：未在 `TOOL_SCOPE` 里声明的工具（`required_scope`
    回 `None`）恒不可见——与 `authz.manifest_gaps` 那条"新增工具忘了声明"的完备性判据
    同一个取向：工程疏漏不该靠运行时宽容。
    """
    scopes = authz.scopes_for(role)
    return sorted(n for n in all_tool_names() if authz.required_scope(n) in scopes)


def _json_schema(tool: Any) -> dict:
    """`args_schema` → JSON Schema（`properties` / `required` 那一层）。

    langchain 的 `@tool` 从函数签名生成 `args_schema`：`Annotated[..., "描述"]` 变
    description、`Literal[...]` 变 enum、有默认值的进 `default`。**这就是工具级菜单
    不必再手写参数规格的原因**——技能级那套 `ParamSpec` 要解决的"必填/类型/默认"
    在这里由签名直接给出。
    """
    schema = tool.args_schema
    if isinstance(schema, dict):
        return dict(schema)
    return dict(schema.model_json_schema())


def build_tool_menu(role: str | None, *, evidence: bool = False) -> list[dict]:
    """这个身份能选的工具 → OpenAI `tools` 数组（形状与技能级那份逐格对齐）。

    `evidence`（出厂关，同 `planner_llm.build_tool_schema` 的 `evidence`）：只挂在
    `authz.is_write` 为真的工具上——射程仍由权限表派生，不另立名单。
    """
    by_name = {t.name: t for t in get_all_tools()}
    tools: list[dict] = []
    for name in visible_tools(role):
        tool = by_name[name]
        schema = _json_schema(tool)
        props: dict[str, dict] = dict(schema.get("properties") or {})
        required: list[str] = list(schema.get("required") or [])
        # 与技能级那两格同源同形：意图清单（第二个出口）与短应答自我申报。
        props[INTENTS_ARG] = intents_prop_schema(INTENTS_FIELD_DESC)
        props[REPLY_KIND_ARG] = reply_kind_prop_schema(REPLY_KIND_FIELD_DESC)
        if evidence and authz.is_write(name):
            props[EVIDENCE_ARG] = evidence_prop_schema(EVIDENCE_FIELD_DESC)
        desc = _TOOL_MENU_LINES.get(name) or (tool.description or "")
        fn: dict[str, Any] = {"name": name, "description": desc,
                              "parameters": {"type": "object", "properties": props,
                                             "additionalProperties": False}}
        # 无必填就**不写** `required` 键（与技能级那份同一条取舍，理由见
        # `planner_llm.build_tool_schema` 的注：少一个字段就少一处网关差异）。
        if required:
            fn["parameters"]["required"] = required
        tools.append({"type": "function", "function": fn})
    # 三个伪函数（登记 / 撤下 / 交意图清单）照搬：它们不是技能，是跨轮任务的申报口，
    # 与菜单形状无关——**不搬就是混变量**（见模块头注 ③）。
    tools.extend(pseudo_tool_schemas(role))
    return tools
