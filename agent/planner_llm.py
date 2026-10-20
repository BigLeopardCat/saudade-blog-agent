"""planner 与 LLM 之间的接线层：技能表 ↔ API 的 `tools` / `tool_calls`（20260927 起）。

**为什么存在**：planner 此前靠"系统渲染文本菜单 → 模型写五行文本 → 正则抠"决策
（`WORKFLOW=` / `PARAMS=` / `TOOLS:` 契约见 `agent/plan_codec.py` 的 `plan_encode`）。那条路把
**格式正确性**押在模型的文本纪律上：实测约 15% 的轮次改用 JSON 对象或带引号键回答，
旧内联正则匹配不到 ⇒ 静默落成 chat（主人收到一句假的"我做不到"）。现在这一步换成
API 的 `tools` 字段 + `tool_calls` 返回——格式由服务端约定、参数由 schema 约束。

> 本模块 20261017 前叫 `native_plan.py`。改名是因为 `native` 那个限定词的**对照物
> 已经不在了**：20261004 文本契约那一档连同它的解析器整族删除后，"native" 不再
> 与任何东西相对。**代码与注释里残留的 `native` 分三类，别顺手改**：① 归档键
> （trace 事件 `native_decision` / `native_fallback` / `native_multi_call`、报告里的
> `engine="native"`）——改了历史基线就失去可比性；② `.env` 的键（`planner_native_*`，
> 见 `config/settings.py` 那一段的注）；③ 历史文档里的叙述（CHANGELOG / 问题记录 / ADR）。

**为什么模型选的是「技能」而不是「工具」**（本模块最关键的一条，是被反证逼出来的）：
初看更自然是"模型直接调工具、我们再反查技能"，核完代码后不成立——
① `planner_node` 的 7 个参数校正器（`_board_quote_fix` / `_announcement_text_fix` /
   `_name_target_fix` / `_name_arg_fix` / `_target_grounding_refusal` /
   `_write_target_refusal` / `_ledger_target_refusal`）全部读**技能级**
   `plan_obj["params"]`；
② 技能参数与工具参数**不是同一层**：`navigate` 技能的参数叫 `target`，而它模板里的
   `navigate_to` 工具参数叫 `path`，且由技能自己从 `NAV_MAP` 算出；
③ 工具→技能反查**天生歧义**：`get_moderation_status` 同时出现在两处技能模板里。
⇒ 模型的 function name = **技能名**，`parameters` = 该技能自己的参数。技能模板层因此
仍然是动作工具的唯一入口，`instantiate_plan` 一行不用改。

**为什么这不缩小本模块的价值**：技能集正是渲染 planner 菜单用的那张表
（`visible_workflows(role)` × `workflow_param_specs`）⇒ schema 与菜单**同源**，"模型能选的"
恒等于"今天这个身份本来就能选的"，没有第二份名单可漂移——**不扩权是结构性的**，
`tests/test_planner_llm.py` 另有一条集合相等断言做防御。工具参数上的 `Literal` 闭集
也第一次变成服务端强制的 `enum`（此前只是提示词里的一句话）。

**边界（别过度承诺）**：这一层治格式正确性、参数正确性、单轮多调用。多步目标跨轮丢失
由**另一条通道**治（20260927 批 D）：schema 里多出来的 `task_hold` 让模型把"还没做完的
步骤"说出来，跨轮的载体是 `agent_task` 表（见 `agent/tasks.py` 的头注）。它不改任何
行为纪律（短应答还原 / 全选式短应答 / 授权式 / 写身份防线）——那是**行为**不是格式。
所有防线（授权 scope、同意闸、确认卡、checker、gate）都在这一层的**下游**，因此天然
继承、无需改写。

⚠️ **本模块不许 import `agent.graph`**：graph 是消费方，反向 import 会成环。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from agent.prompt_context import (
    REPLY_KIND_ARG,
    REPLY_KIND_FIELD_DESC,
    parse_reply_kind,
    reply_kind_prop_schema,
)
from agent.provenance import (
    EVIDENCE_ARG,
    EVIDENCE_FIELD_DESC,
    evidence_prop_schema,
    parse_evidence,
    write_scope_tools,
)
from agent.tasks import (
    INTENTS_ARG,
    INTENTS_FIELD_DESC,
    TASK_DROP,
    TASK_HOLD,
    TASK_INTENTS,
    intents_prop_schema,
    normalize_declaration,
    normalize_drop,
    normalize_intents,
    pseudo_tool_schemas,
)
from agent.tool_index import describe_skill_schema, index_line, skill_description
from agent.workflow_defs import WORKFLOW_MAP, visible_workflows
from agent.workflow_model import ParamSpec
from agent.workflow_params import tool_arg_schemas, workflow_param_specs
from agent.workflow_tools import callable_query_tools, explicit_tools
from models.llm import get_llm

logger = logging.getLogger(__name__)

# 技能参数的短类型名（`ParamSpec.type`，见 `workflow_params.py::arg_type_short`）→ JSON Schema。
# `any` / `?` 是"推不出映射"，不在表里——由 `_param_schema` 兜（见那里的注）。
_TYPE_MAP = {"str": "string", "int": "integer", "num": "number",
             "bool": "boolean", "list": "array", "dict": "object"}


@dataclass(frozen=True)
class PlannerDecision:
    """一次工具调用决策的结果。**只是数据**——不写 state、不碰库、不发帧。

    `workflow` / `params` 交给 `instantiate_plan` 展开，与文本档产出**同构**；其余字段
    只进 trace，供事后按 engine 切语料对账（见本模块头注的"边界"）。

    `declare`（20260927 批 D）是唯一的例外：它不是技能参数，而是"这件事还没做完"的
    结构化登记（`agent/tasks.py`）。它与 `workflow`/`params` **可以同时有**——"这一轮把
    第一步做掉 + 把剩下的登记下来"正是多步目标该有的形态。消费方（planner）见它就走
    任务登记那一支，不把它塞进 `instantiate_plan`。
    """
    workflow: str
    params: dict
    notes: tuple[str, ...] = ()      # 机器可读的异常记账（如 native_multi_call）
    finish_reason: str = ""          # `length` = 被额度截断（"预算够不够"的判据）
    raw_tool_calls: tuple = field(default_factory=tuple)
    declare: dict | None = None      # 归一化后的任务登记（无 → None）
    # 模型对**短应答**那一类的自我申报（20261015 批 3，`agent/prompt_context.py::REPLY_KIND_ARG`）：
    # `"pos"|"neg"|"auth"`、别处塞进来的写法（原样留 16 字）或 `""`（没申报）。
    # **不是判据**——准入是纯结构的（`prompt_context._is_short_reply`），这一格只进 trace
    # （`planner.reply_kind`）供事后核对"模型把哪一类读错了"。
    reply_kind: str = ""
    # 归一化后的**意图清单**（20261008 批 ②，`agent/tasks.py::TASK_INTENTS`）：主人这一句
    # 话里要办的每一件事 `[{"goal", "workflow"}]`（含这一轮正要办的那件）。与 `declare`
    # 并列而**不是**它的替代：`declare` 是"剩下的步骤与工具"（模型写的计划），
    # `intents` 只是枚举——系统拿它减去本轮办了的，把剩下的自动登记（步骤由技能模板推）。
    # 空元组 = 模型没给（这是常态，只有多件事的一句话才该给）。
    intents: tuple[dict, ...] = ()
    # 一个函数都没点、正文却非空（20261004）：**这不是一个决策**，只是"没做出决策"。
    # 它的 `workflow` 仍是 `chat`（下游要用一个合法技能名把这一轮走完），但调用方必须先
    # 拿这个标记去走一次纠偏——见 `planner_node` 的"零调用"那一格与
    # `_PLANNER_OUTPUT_CONTRACT_NATIVE` 第 7 条。
    undecided: bool = False
    # 出处引文（20261014 第一批，`agent/provenance.py`）：挂在动作调用上的那一格
    # `evidence` 摘下来之后的**归一产物** `((arg, quote), ...)`。空元组 = 模型没给
    # （出厂关这一档的常态，也是开档但目标明确时的常态）。**它只进 trace 与影子核验**：
    # 第一批**不拦任何事**，也**永不进 `spec.args`**（见模块头注的出处通道一节）。
    evidence: tuple[tuple[str, str], ...] = ()


def _items_schema(sp: ParamSpec) -> dict:
    """数组参数的 `items`：从 `ParamSpec.from_tool` 回查工具原始片段。

    **为什么必须回查**：`ParamSpec` 只留了短类型名，`list` 丢掉了元素类型，而元素
    类型是有真有假的——`notice_read.ids` 是 integer、`article_tags.add` 是 string。
    一律声明 string 会让模型把 id 写成 `"3"`，而 `check_workflow_params` 的归一**不看
    数组元素**，于是这个错误会一路走到工具层才炸（白烧一轮）。`from_tool` 是
    `ParamSpec` 自己的字段、指向同一条派生链，所以这不算引入第二个来源。
    查不到就退化成 string（**宁可少说，不说错**——同 `render_workflow_params` 的取向）。
    """
    tool_name, _, arg = sp.from_tool.partition(".")
    frag = ((tool_arg_schemas().get(tool_name) or {}).get("properties") or {}).get(arg)
    if not isinstance(frag, dict):
        return {"type": "string"}
    # 可空数组的片段长这样：`{"anyOf": [{"type":"array","items":{...}}, {"type":"null"}]}`
    # （pydantic 2.13 实测，见 `agent/workflow_params.py::arg_enum` 的同族注），所以要把
    # anyOf 各支与顶层一起看——只看顶层会得出"它没有 items"。
    variants = list(frag.get("anyOf") or ()) + [frag]
    for one in variants:
        if isinstance(one, dict) and one.get("type") == "array":
            items = one.get("items")
            if isinstance(items, dict) and items.get("type"):
                return dict(items)
    return {"type": "string"}


def _param_schema(sp: ParamSpec) -> dict:
    """一个 `ParamSpec` → JSON Schema 属性片段。"""
    js: dict[str, Any] = {}
    if sp.type == "list":
        js["type"] = "array"
        js["items"] = _items_schema(sp)
    elif sp.type in _TYPE_MAP:
        js["type"] = _TYPE_MAP[sp.type]
    else:
        # `any` / `?`：参数没配到工具（如 navigate.target 由 NAV_MAP 映射）。
        # 声明 `string` 而不是留空：留空在 JSON Schema 里合法，但多数网关会当成
        # 缺省类型而拒绝整份 tools；而 string 是安全的——这些参数在文本档里本来就是
        # 被当字符串读的（`(params.get("target") or "").strip()`）。
        js["type"] = "string"
    if sp.desc:
        js["description"] = sp.desc
    if sp.choices:
        js["enum"] = list(sp.choices)
    return js


# ── 手写覆盖：只有这几格，理由是它们**恰恰是 `ParamSpec` 推不出形状的那几格** ──
# `content_query.plan` 是空列表（调用清单由 planner 经 PARAMS.tools/calls 注入，
# 见技能定义），所以 `_template_param_map` 配不到工具、两个参数一律落在 `any`。
# 而它们正是 native 收益最大的地方：白名单此前只是提示词里的一句话，现在可以变成
# 服务端强制的 `enum`。`review_inbox.calls` 同理（它的 `plan` 模板参数也是空的——
# 逐条调用的**工具名由模型写**，模板给不出映射）。
# **别顺手往这张表里加别的键**——每加一格就少一格同源保证，
# `tests/test_planner_llm.py` 把键集合钉成了字面量，加键必须同改测试（= 有人复核）。
_CALLS_ITEMS = {
    "type": "object",
    "properties": {
        "tool": {"type": "string"},
        "args": {"type": "object"},
    },
    "required": ["tool", "args"],
}

_SCHEMA_OVERRIDES: dict[tuple[str, str], dict] = {
    ("content_query", "tools"): {
        "type": "array",
        "items": {"type": "string"},
        "description": "无参只读数据工具的点名列表（闭集由本轮角色决定）",
    },
    ("content_query", "calls"): {
        "type": "array",
        "items": _CALLS_ITEMS,
        "description": "带参调用清单：[{tool, args}]，只给当前步",
    },
    ("review_inbox", "calls"): {
        "type": "array",
        "items": _CALLS_ITEMS,
        "description": "要一次办的那几件，逐条 {tool, args}",
    },
    # `tag_create.titles`（20261008 批）：一次新建多个标签的名字清单。同属"推不出
    # 形状"那一族——技能模板是**一个名字一条 spec**（`{"title": "$title"}`），数组
    # 这一格在 `_template_param_map` 里配不到任何工具参数（`create_tag` 收的是单数
    # `title`）。不收进来它就落进 `_param_schema` 的兜底、被声明成 `string`：模型
    # 填一个字符串**不算错**（展开层两种都收，见 `workflows._write_list_arg`），但
    # "这一格装的是一批名字"这件事，只有 array 声明说得出口——而它正是"主人一次
    # 点了好几个新标签"能被一次办掉的入口。
    ("tag_create", "titles"): {
        "type": "array",
        "items": {"type": "string"},
        "description": "一次新建多个标签时的名字清单（只建一个就别用它，填 title）",
    },
}


def _calls_arg_hint(tool_names) -> str:
    """`[{tool,args}]` 里每个工具的**参数名**（从工具自己的 schema 现取）。

    **为什么必须现取而不是在这写死一句**：`args` 声明的是 `{"type": "object"}`——
    形状对，但模型还得知道每件要填哪些键，否则只能靠猜。把键名写在描述里就等于在这
    手抄一份工具签名（本仓反复打掉的"第二份名单"），而 `tool_arg_schemas()` 是那条
    派生链的正主（`ParamSpec.from_tool` 回查的是同一份，见 `_items_schema`）。
    取不到的工具**整条跳过**（宁可少说，不说错）。
    """
    schemas = tool_arg_schemas()
    bits = []
    for t in tool_names:
        got = schemas.get(t) or {}
        req = [n for n in (got.get("properties") or {}) if n in (got.get("required") or ())]
        if req:
            bits.append(f"{t} 填 {'、'.join(sorted(req))}")
    return "；".join(bits)


def _override_for(role: str | None, workflow_name: str, param: str) -> dict | None:
    """取手写覆盖（含按角色展开的闭集）。没覆盖 → None。"""
    base = _SCHEMA_OVERRIDES.get((workflow_name, param))
    if base is None:
        return None
    out = dict(base)
    if workflow_name == "content_query" and param == "tools":
        out["items"] = {"type": "string", "enum": explicit_tools(role)}
    elif workflow_name == "content_query" and param == "calls":
        # 闭集按角色展开 —— 白名单只有 `callable_query_tools` 这一处来源
        # （见 agent/workflow_tools.py 的注：手写名单是漏项来源）。
        out["items"] = {
            **base["items"],
            "properties": {
                **base["items"]["properties"],
                "tool": {"type": "string", "enum": callable_query_tools(role)},
            },
        }
    elif workflow_name == "review_inbox" and param == "calls":
        # 闭集 = 该技能 `plan` 声明的工具全集（与 `_expand_change_set` / 执行轮
        # `_confirm_grant_plan` 读的是**同一个字段**，见技能定义：它同时是跨族安全性
        # 的来源）。枚举之外的写工具名因此连 schema 这一关都过不了。
        allowed = [t for t, _ in (WORKFLOW_MAP[workflow_name].plan or ())]
        items = {**base["items"],
                 "properties": {**base["items"]["properties"],
                                "tool": {"type": "string", "enum": allowed}}}
        hint = _calls_arg_hint(allowed)
        out = {**base, "items": items}
        if hint:
            out["description"] = f"{base['description']}（{hint}）"
    return out


def _has_write_tool(workflow) -> bool:
    """这个技能**可能**派出去的工具里，有没有落在出处闸射程内的写工具（20261014）。

    取两者之和（`plan` 模板 + `extra_plan_tools` 条件工具），即"这份令牌里容得下
    哪几件"——与 `_confirm_grant_plan` 的形状检查**同一处事实源**（见 `Workflow.extra_plan_tools`
    的注：只看 `plan` 会漏掉 `article_tags` 的 `create_tag`，而那正是要出处的那一件）。
    """
    scope = write_scope_tools()
    return any(t in scope for t in
               [t for t, _ in workflow.plan] + list(workflow.extra_plan_tools))


def build_tool_schema(role: str | None, *, evidence: bool = False,
                      deny: frozenset[str] | set[str] | None = None,
                      deny_pseudo: frozenset[str] | set[str] | None = None,
                      index: bool = False, slots: bool = True) -> list[dict]:
    """本轮这个身份能选的技能 → OpenAI `tools` 数组。

    **来源只有一处**：`visible_workflows(role)` × `workflow_param_specs(workflow)`——与渲染
    planner 菜单（`build_planner_context`）用的是同一张表。因此"模型能选的"恒等于
    "今天这个身份本来就能选的"：管理员技能在 `role != "admin"` 时结构上选不出来，
    不需要在这里再判一次角色。

    **`deny` 是"这一轮不许再选"的技能名集合**（20261007，1d）——来源是上一轮的受阻项里
    "改参数重试无效"那一族（`block_reasons.denied_workflows`）。它**不扩权**（只会更小），
    也不是第二份可见性名单：它只在"上一轮同一个技能刚失败过"时才非空，无阻碍轮**传空集
    ⇒ schema 逐字节不变**。语义上是"模型没有可再点的东西"，不是"提示它别点"——同一条
    禁令写进提示词的那两版都被 A/B 否掉了（见 `docs/问题记录.md` §1.55 的 1b）。
    `chat` 永不进这个集合（见 `block_reasons._NEVER_DENY`），调用方那一侧保证。

    `complete_when` 拼进 description：它在文本档里本来就只进提示词、**没有强制点**
    （没有任何代码读它），搬进 description 是等价的，且比原来离决策更近。

    **三个伪函数恒在**（`agent/tasks.py`：`task_hold` 登记"还没做完的事"、`task_drop`
    撤下一件已登记的事——为什么是两条而不是"空步骤"，见 `tasks.TASK_DROP` 的注；
    `task_intents` 交意图清单）。**20261015 撤档前它们挂在一个 `task_state` 开关下**
    （出厂关、产线开）：现在没有开关了——任务状态是默认必备的能力，不该有开关把它变成
    摆设（主人裁决，理由与判据见 `docs/问题记录.md` §1.63）。它们不进 `visible_workflows`
    那条集合断言的口径里——断言写的是"技能名集合 + 申报过的伪函数"（见
    `tests/test_planner_llm.py` ①）。**这不是扩权**：它们不是技能、不执行任何工具、
    也没有第二个消费方，只是让模型能把"剩下的步骤"说出来、把"不做了"说清楚；
    能不能真做，仍然由技能通道与下游全部防线决定。

    **每个技能函数还多一格可选参数 `intents`**（意图清单的第二个出口，见
    `agent/tasks.py::intents_prop_schema`）：`parallel_tool_calls=False` 让"单独交清单"
    与"点技能"在**同一轮**里互斥，挂在动作调用上的一格让两者一次说完。它**不进
    `required`**、也不改 `params`（`tool_calls_to_plan` 摘走它再归一化），所以
    "模型不填"这条路径与本参数不存在时**逐字节相同**。

    `evidence=True`（20261014 第一批，**出厂关**）时，**会产出写工具的技能**再多一格
    可选参数 `evidence`（出处引文，见 `agent/provenance.py` 头注）。三个"同 `intents`"：
    不进 `required`、不进 `params`（`tool_calls_to_plan` 摘走）、不填时逐字节相同。
    两处不同：① 只挂在**射程内**的技能上（`_has_write_tool`，射程是派生的）；
    ② 这一批**只是影子**——摘出来的引文没人拿它拦任何事，只落 trace。

    **`deny_pseudo` 是同一个 `deny` 的伪函数版**（20261008 批 ②，见 `graph.py` 里
    "只交意图清单"那一格）：语义、代价、空集不变性**逐条同 `deny`**，只有一处不同——
    它的来源不是"上一轮失败过"，而是"**这一轮回的正是它**"。实测依据（两条 golden
    真链路）：生产模型在 `parallel_tool_calls=False` 下**一条轮次只发得出一条调用**，
    所以"交清单"与"点技能"在同一轮里互斥——模型交了清单那一轮就零动作，而 `deepseek`
    那一次（`20261008_213818` 的 `mix2_two_writes_one_breath_card_only`）：纠偏重决策
    后的输出与上一版**逐字节相同**（`planner.llm_done` 的 `input` 29048→29130、
    `output` 75→75，`native_decision.calls` 两次都是孤零零的 `task_intents`）。
    纯话术纠不动一个它本来就想交的答案，所以这一格也走 1d 那条
    验证过的路：**把选项从 schema 里摘掉**，而不是再劝一次（同 §1.55 的 1b 结论）。
    `task_hold` / `task_drop` 同理可摘，只是今天没有触发它们的现场。

    **`index=True`（20261020，ADR-0015 阶段 0，出厂关）**：每个技能的 `description` 从
    整段散文压成**一行索引**，并追加一个 `describe_skill(name)` 让模型按需取回原文。
    **结构与散文的边界是**：函数名 / type / enum / required / 嵌套 schema 一个字符不动
    （`enum` 是带参清单的拦截点），只换 `description` 那一格的内容。
    缺省 `False` ⇒ **本参数不存在时这条路径逐字节相同**（`tests/test_prompt_prefix.py`
    钉住），理由、实测依据与"为什么是外置不是删除"见 `agent/tool_index.py` 头注。

    **`slots=False`（20261021，出厂开）**：不挂 `intents` / `reply_kind` 那两个**跨切面
    槽位**。缺省 `True` ⇒ **本参数不存在时这条路径逐字节相同**（`tests/test_prompt_prefix.py`
    与 `tests/test_planner_llm.py` 的集合断言钉着）。**唯一消费方是能力索引臂**——它折算
    计划时走 `tool_menu.strip_slots`，那两格**填了就扔**；而它们单份 799 字符 × 49 条
    能力 = 索引菜单的 **59%**。三个伪函数**不受影响**（`task_intents` 自带 `intents`，
    它才是被 `tasks.normalize_intents` 真读的那个出口），理由见 `tool_menu.build_tool_menu`。
    """
    tools: list[dict] = []
    for workflow in visible_workflows(role):
        if deny and workflow.name in deny:
            continue
        specs = workflow_param_specs(workflow)
        props: dict[str, dict] = {}
        required: list[str] = []
        for name, sp in specs.items():
            props[name] = _override_for(role, workflow.name, name) or _param_schema(sp)
            if sp.required:
                required.append(name)
        # 两个**跨切面槽位**（`slots`，见 docstring）：它们是"给模型一个地方填"的申报口，
        # 不进 `required`、不进 `params`（`tool_calls_to_plan` 摘走），所以"模型不填"
        # 这条路径与本参数不存在时逐字节相同。
        if slots:
            # 意图清单的**第二个出口**（20261008，见 `tasks.intents_prop_schema`）：每个
            # 技能函数多带一格**可选**的 `intents`。**不进 `required`**——它是"一句话里
            # 有两件以上时才填"，填不填都不影响这一轮的动作决策（模型不填 = 今天的行为）。
            # 挂在每一个技能上而不是只挂写技能：要办的两件可以都不是写（"查一下 X 再顺手
            # 把它置顶"），而"哪几件"这件事与技能是不是写无关。
            props[INTENTS_ARG] = intents_prop_schema(INTENTS_FIELD_DESC)
            # 短应答的自我申报（20261015 批 3，`prompt_context.REPLY_KIND_FIELD_DESC`）：
            # **挂在每个技能上、没有任何档位**。拷 `tasks.py` 那份体例的两处不同见
            # `prompt_context.REPLY_KIND_ARG` 的注；这里只补一条为什么**不**像 `evidence`
            # 那样按技能射程收窄：短应答的三种出口各自落到哪个技能是不定的（同意多半落
            # 在读、授权多半落在写），而"模型读成了哪一类"这件事与技能无关——按射程收窄
            # 会让大部分轮次没有申报口，读数就断了。
            props[REPLY_KIND_ARG] = reply_kind_prop_schema(REPLY_KIND_FIELD_DESC)
        # 出处引文（20261014 第一批，`evidence` 那一格）：**只挂在会产出写工具的
        # 技能上**。射程由 `agent/provenance.write_scope_tools` 派生（= authz 里声明
        # 为写 ∧ 不在"一律弹窗"族），不是第二份手抄名单——挂多了是给模型白送一格
        # 没人读的自我说明，挂少了影子数据会缺一整族。
        # 与 `intents` 一样：**不进 `required`、不进 `params`**（`tool_calls_to_plan`
        # 摘走），所以"模型不填"与本参数不存在时逐字节相同。
        if evidence and _has_write_tool(workflow):
            props[EVIDENCE_ARG] = evidence_prop_schema(EVIDENCE_FIELD_DESC)
        # 描述里的工具枚举标记**必须在这里展开**（20260927 修）：技能描述里写着
        # `__无参只读工具清单__` 这类占位（见 workflow_model.py 的 `_EXPLICIT_TOOLS_MARK`），
        # 角色相关的展开此前只发生在文本菜单那一路（`workflow_menu.py` 的
        # `render_tool_marks`）⇒ 接口层把**未展开的标记原样**发给了模型（实测 admin/None
        # 各 1 处）。两处渲染同一份
        # 描述文本，展开器只能有一个（同"手抄第二份名单"的教训）。
        # 20261020：展开那两行搬进 `tool_index.skill_description`——索引档要取回**同一份**
        # 文本（`describe_skill` 的返回），写两处就会悄悄分叉，而分叉没有判据看得见。
        desc = skill_description(workflow, role)
        if index:
            desc = index_line(desc)
        fn: dict[str, Any] = {"name": workflow.name, "description": desc,
                              "parameters": {"type": "object", "properties": props,
                                             "additionalProperties": False}}
        # 无必填就**不写** `required` 键：写空数组与不写语义相同，但少一个字段就少
        # 一处网关差异（有些网关对空 required 的处理不一致）。**`additionalProperties:
        # False` 不受这条取舍影响**：它是"这份函数的参数就这几格"的**声明**，与空
        # `required` 那种"写不写都一样"不同——它把"这一格是关闭的"这件事写成了事实，
        # 而不是留给"没写=随便"去解释。**只加在这一层**（函数参数的顶层）：`calls[].args`
        # 那类内层对象是**自由形状**（键就是被调工具的形参名），在那儿加会把它关死。
        if required:
            fn["parameters"]["required"] = required
        tools.append({"type": "function", "function": fn})
    # 名字从 schema 自己身上读（`s["function"]["name"]`），不另立一份伪函数名表：
    # 摘的是同一批对象，"名单"与"形状"必须是同一处事实源（同 `pseudo_tool_schemas`
    # 头注那条）。
    tools.extend(s for s in pseudo_tool_schemas(role)
                 if not (deny_pseudo and s["function"]["name"] in deny_pseudo))
    # 索引档才有的取回口（ADR-0015 阶段 0）。**不并进 `pseudo_tool_schemas`**：那三个伪函数
    # 是"任务状态"这一族（有开关史、有集合断言），而 `describe_skill` 是**读自己菜单**的
    # 纯只读口——它无副作用、不进 `_split_pseudo_calls`、缺省档一次都不出现。
    # 放在**最后**：它是对上面那份菜单的注释，不是菜单里的一项。
    if index:
        tools.append(describe_skill_schema())
    return tools


def finish_reason(resp: object) -> str:
    """响应里的 `finish_reason`（取不到 → 空串）。

    **单独一个函数是因为它是"截断"的唯一机器判据**：`length` 意味着输出被额度切断，
    此时 `arguments` 极可能断在半截 JSON 上。调用方（graph 的 native 分支）要把它
    记进 trace——`length` 的占比正是"预算够不够"这个问题的答案。
    """
    meta = getattr(resp, "response_metadata", None)
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("finish_reason") or "")


def _content_of(resp: object) -> str:
    """AIMessage.content → str（langchain 1.x 允许内容块列表，这里只取文本块）。"""
    raw = getattr(resp, "content", "")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "".join(b.get("text", "") for b in raw if isinstance(b, dict))
    return ""


def _split_pseudo_calls(calls: list, role: str | None):
    """把**三个伪函数**（登记 / 撤下 / 意图清单）从调用里摘出来。

    返回 `(剩下的调用, declare, intents, notes)`。它们不参与技能选择，
    单独归一化成 `declare` / `intents`（机制与取向见 `tool_calls_to_plan` 头注那一节）。
    判不了一律**记账不猜**：
      · 两个伪函数同轮出现 ⇒ 取登记、把撤下记成 `task_drop_ignored`（代价不对称：丢掉
        登记 = 这件事又没人管了，丢掉撤下 = 系统多跟踪一会儿，下一轮照样能撤）；
      · 归一化失败（没给 goal / 一条有效步骤都没有 / 撤下的 goal 是空的）**不等于判不了**
        ——当作"这次登记无效"记一笔，继续按剩下的调用决定这一轮：为此丢掉一个合法决策
        是更大的损失。这一格也是"空 steps 撤下"那条老路的终点：它现在什么都不做
        （见 `agent/tasks.py` 的 TASK_DROP 注）；
      · 意图清单与登记**互不替代**，两条可以同时出现（"这一轮做一件 + 把这句话里剩下那
        几件记下来"正是它该有的形态）；整份判无效（没给数组 / 一条都归一不出来）同样只记一笔。
    """
    rest, holds, drops, ints = [], [], [], []
    for c in calls:
        nm = str((c or {}).get("name") or "") if isinstance(c, dict) else ""
        if nm == TASK_HOLD:
            holds.append(c)
        elif nm == TASK_DROP:
            drops.append(c)
        elif nm == TASK_INTENTS:
            ints.append(c)
        else:
            rest.append(c)
    notes: list[str] = []
    declare: dict | None = None
    intents: tuple[dict, ...] = ()
    if holds and drops:
        notes.append("task_drop_ignored")
    if holds:
        declare = normalize_declaration((holds[0] or {}).get("args"))
        if declare is None:
            notes.append("task_hold_invalid")
    elif drops:
        declare = normalize_drop((drops[0] or {}).get("args"))
        if declare is None:
            notes.append("task_drop_invalid")
    if ints:
        intents = tuple(normalize_intents((ints[0] or {}).get("args"), role))
        if not intents:
            notes.append("task_intents_invalid")
    return rest, declare, intents, notes


def tool_calls_to_plan(resp: object, role: str | None) -> PlannerDecision | None:
    """一次 `tool_calls` 返回 → `PlannerDecision`；**判不了就返回 None**（调用方兜底）。

    None 的五个来源（全是"没给出可用决定"，调用方一律走确定性收尾，**不猜**）：
      · `invalid_tool_calls` 非空 —— 参数被截断或不是合法 JSON（思考链吃光
        max_tokens 时 `finish_reason=length`，arguments 会断在半截）；
      · 模型点了不在本轮 schema 里的函数名；
      · `args` 不是对象（我们的 schema 全是 object，出现别的形状说明响应不合约定）；
      · 零 `tool_calls` 且 `finish_reason == "length"`（被额度截断）；
      · 零 `tool_calls` 且正文也空（既没决策也没话说）。

    一条都没发、正文却有 ⇒ 返回 `chat`，但**打上 `undecided`**（20261004）：零调用
    不是闲聊的代名词，只是"这一轮没做出决策"。调用方（`planner_node`）见它就先用
    纠偏通道把契约第 7 条（"闲聊也要显式点 `chat`"）讲一次重决策——第二次仍这样才
    认成 `chat`。理由：实测 384 份 trace 里 42 次零帧零调用轮，一半是封号/驳回/
    标记已读/导航这类真动作请求（"一个函数都不点"从来没被追过责）。
    `params` 恒给**空**。

    ⚠️ 只有登记/撤回、或伪函数参数无效时（`declare` 非空或有 `notes`）**不打**
    `undecided`：那一轮模型是明确表达过意图的（"剩下的记下来"），只是没有动作要做。

    ⚠️ **别把模型那句话塞进 `params.reply`**（20260927 一稿就是这么写的，跟着数据核
    完改掉了）：`chat` 技能在注册表里 `inputs={}`、`workflow_param_specs` 也是空的，
    而 `planner_node` 会把 `PARAMS=` 原样写进计划文本（`plan_encode`）交给 narrator
    ——所以① 那句话**不会**被任何代码当字段读走；② 会白白记一条 `param_unknown`
    告警（那个信号是**注册表与提示词漂移的探针**，每轮闲聊都报一次等于把它淹掉）。
    文本档在这一格给的就是空 `PARAMS`，native 照做才是"同构"。
    回复正文由 narrator 依据 `reply_contract` 生成——**两条路都是这样**，不是本模块的取舍。

    content 也空 ⇒ 返回 None，由调用方走既有收尾（`_wrap_up_plan`），不在这里编一句话。

    先摘出**三个伪函数**的调用（登记/撤下：20260927 批 D；意图清单：20261008 批 ②）：
    **它们不参与技能选择**，而是单独归一化成 `declare` / `intents`，剩下的调用照旧走本
    函数原有的"取第一条"逻辑。组合都成立且都要支持——只登记（`workflow="chat"`、
    `declare` 非空）、登记 + 一个动作调用（"这一轮做掉第一步，同时把剩下的记下来"，
    这正是多步目标该有的形态）、一句话多件事时"动作 + 意图清单"。
    （20261015 撤档前这一步挂在一个 `task_state` 开关下；开关撤掉后摘伪调用是**无条件**
    的，见 `docs/问题记录.md` §1.63。）

    意图清单还有**第二个出口**：动作调用自己的参数里那一格 `intents`（`build_tool_schema`
    挂在每个技能上，见 `agent/tasks.py::intents_prop_schema`）。它**在取到第一条调用
    之后**摘、摘完再归一化——因为 `params` 是这个技能的真实参数，多一个键会一路漏到
    `PARAMS=` 与 `execute`。两条出口合并（顺序：独立调用在前、字段在后），下游看到的是
    同一份 `intents`。

    **两个伪函数同轮出现时取登记、把撤下记账丢掉**（`task_drop_ignored`）：两条意图
    互相矛盾，而误判代价不对称——丢掉登记 = 这件事又没人管了（本批要治的那个病）；
    丢掉撤下 = 系统多跟踪一会儿（下一轮照样能撤）。同一条取向见 `agent/tasks.py`
    的 `normalize_declaration`：判不了就记账、不猜。
    """
    if list(getattr(resp, "invalid_tool_calls", None) or ()):
        return None
    calls = list(getattr(resp, "tool_calls", None) or ())
    base = {"finish_reason": finish_reason(resp), "raw_tool_calls": tuple(calls)}
    declare: dict | None = None
    intents: tuple[dict, ...] = ()
    notes: list[str] = []
    calls, declare, intents, notes = _split_pseudo_calls(calls, role)
    if not calls:
        undecided = False
        if declare is None and not notes and not intents:
            # 截断 ≠ 闲聊（20261001）。`finish_reason == "length"` 意味着这一轮的话
            # 被额度**切断**，而"还没说到那个 tool_call 就被切"与"这一轮本来就没有
            # 动作"在响应里**形状完全相同**（都是零 `tool_calls`）——旧行为把后者
            # 当成前者：静默落成 `chat`、零工具、narrator 手里零帧（首轮尤其致命，
            # 只能凭记忆或道歉收场）。截断**不做决定**：返回 None，交给调用方走
            # 确定性收尾（`planner_node` 的 `native_fallback` 截断轨）。
            #
            # **刻意不重试**（别照抄 narrator 那次空内容重试）：那是**采样**失败
            # （同一条消息再问一次就正常），这是**预算**失败——同一条消息再问一次
            # 多半截在同一个位置，多花的只是钱。两类病两副药。
            if finish_reason(resp) == "length":
                return None
            if not _content_of(resp).strip():
                return None
            # 正文非空却一个函数都没点（20261004）：**零调用不是一个决策**。
            # 契约第 7 条明说"闲聊也要显式点 `chat`"，这中间的缝是"什么都不点
            # 就交卷"——实测 384 份 trace 里 42 次零帧零调用轮，其中一半是
            # 封号/驳回/标记已读/导航这类真动作请求，全被这里静默落成 chat。
            # 仍然给 `chat`（下游要用一个合法技能名走完这一轮），但打上
            # `undecided` ⇒ 调用方**先纠偏一次**，模型改口点了函数就走那条；
            # 第二次仍这样才认（见 planner_node"零调用"那一格）。
            undecided = True
        # 只有登记/撤回/意图清单、或伪函数参数无效：技能位给 chat（本轮没有动作要执行）
        # ——`declare`/`intents` 交给 planner 那一支，`notes` 让那一格在 trace 里看得见。
        return PlannerDecision(workflow="chat", params={}, notes=tuple(notes),
                              declare=declare, intents=intents,
                              undecided=undecided, **base)
    head = calls[0] if isinstance(calls[0], dict) else {}
    name = str(head.get("name") or "")
    if name not in {s.name for s in visible_workflows(role)}:
        return None
    args = head.get("args")
    if not isinstance(args, dict):
        return None
    # 出处引文（20261014）：**无条件摘走**，与 `evidence` 那一档开不开无关。理由同下面
    # `intents` 那条长注（摘的是"非技能参数"），且多一条：摘掉才是"这一格不存在"那条
    # 路径**逐字节相同**（缺键 `pop` 是 no-op）——关了出厂档不许有任何行为差，就是这条。
    evidence = parse_evidence(args.pop(EVIDENCE_ARG, None))
    # 短应答申报（20261015 批 3）：同样**无条件摘走**，理由与上面 `evidence` 逐字相同
    # ——摘的是"非技能参数"，留着会跟着 `params` 走到 `PARAMS=` 与 `execute` 的参数校验。
    reply_kind = parse_reply_kind(args.pop(REPLY_KIND_ARG, None))
    # 单独一条 `task_intents` 调用**和**一个动作调用同轮到达（网关忽略
    # `parallel_tool_calls=False` 时的形态）——下面两条记账要分开，别把"字段填了"
    # 读成"多来了一条调用"（一个是设计内的常态，一个是约定被打破的证据）。
    _ints_from_call = bool(intents)
    if INTENTS_ARG in args:
        # 意图清单的**第二个出口**（见 `intents_prop_schema`）：挂在动作调用上的那一格。
        # `pop` 而不是 `get`——它**不是**这个技能的参数，留着会跟着 `params` 一路走到
        # `PARAMS=` 计划文本、走到 `execute` 的参数校验（多一个键要么触发"参数未知"
        # 告警、要么被当成技能参数原样填进模板）。摘走之后，`params` 与本参数不存在时
        # **逐字节相同**（包括模型压根没填的那条路径）。
        inline = normalize_intents({"intents": args.pop(INTENTS_ARG)}, role)
        # 与单独一条 `task_intents` 调用**不冲突**（网关若忽略了
        # `parallel_tool_calls=False`，两种可以同时到）：合并、按原顺序。
        intents = tuple(intents) + tuple(inline)
        # 归一后为空也要记账：`task_intents_invalid` 说明模型填了清单而一件都收不下
        # （技能名写错/写了个够不着的本领），那是"漏登记"的一条静默路径。
        notes.append(f"{TASK_INTENTS}_field:{name}" if inline
                     else "task_intents_field_invalid")
    if len(calls) > 1:
        # 并发调用本应由 `parallel_tool_calls=False` 挡在服务端；网关若忽略该参数，
        # 这里只取第一条、其余记账。**绝不**把多个技能拼进同一份计划——那会绕过
        # "一张确认卡只装同一个技能的动作"（见 mainline §6.5）。
        rest = ",".join(str((c or {}).get("name") or "?") for c in calls[1:])
        notes.append(f"native_multi_call:{name}|{rest}")
    if declare is not None:
        # 同一轮既调用动作又登记：**允许**（多步目标"做一步、记下剩下的"就是这个形状），
        # 但它越过了 `parallel_tool_calls=False` 的约定 ⇒ 记账，供事后看这个约定是否被
        # 网关遵守（若常态出现，那说明该参数的约束力要重新评估）。
        notes.append(f"{TASK_HOLD if declare.get('state') != 'cancelled' else TASK_DROP}"
                     f"_inline:{name}")
    if _ints_from_call:
        # 同上：意图清单与动作同轮出现**正是它该有的形态**（模型一次把"这一轮做哪件 +
        # 这句话里还有哪几件"说完），记账的理由与上面那条一样——它是网关是否遵守
        # `parallel_tool_calls=False` 的第二个观测点。**判据是"清单来自另一条调用"
        # 而不是"intents 非空"**：字段那一路（设计内的常态）另有 `task_intents_field`
        # 一条，混用会让"约定被打破"这个信号恒真。
        notes.append(f"{TASK_INTENTS}_inline:{name}")
    return PlannerDecision(workflow=name, params=args, notes=tuple(notes), evidence=evidence,
                          reply_kind=reply_kind,
                          declare=declare, intents=intents, **base)


def bind_planner_tools(llm: object, role: str | None, *, evidence: bool = False,
                deny: frozenset[str] | set[str] | None = None,
                deny_pseudo: frozenset[str] | set[str] | None = None) -> object:
    """把 LLM 绑上本轮的 schema。**`tool_choice` 固定 `auto`、`parallel_tool_calls=False`**。

    · `auto` 而不是 `required`：强制会把闲聊轮也逼成一次假技能调用（模型明明该答
      "你好呀"却得点一个技能）。带参清单的白名单因此靠 schema 的 `enum` 约束，
      而不是靠"必须调工具"。
    · `parallel_tool_calls=False`：下游"一张确认卡只装同一个技能的动作"是按一轮一条
      设计的（见 `docs/native-toolcalls-mainline.md` §6.5）。网关若忽略这个参数，
      `tool_calls_to_plan` 只取第一条并记账——**两道都在**，不互替。
    · 任务状态那三个伪函数（`task_hold` / `task_drop` / `task_intents`）**恒在**
      （20261015 撤档，见 `build_tool_schema` 的注）——本函数不再有 `task_state` 参数，
      "忘了传"这种静默失效因此在结构上不存在。
    · `evidence=True` 时会产出写工具的技能多一格 `evidence`：**出厂关**，
      "关了就等于这一格不存在"（`build_tool_schema` 不往 `props` 里放它，
      模型结构上发不出来）。
    · schema 为空时**不 bind**（20261002）：OpenAI 兼容网关对 `tools: []` 没有一致语义
      （有的 400，有的直接拒 `tool_choice="auto"`）。今天这一支不可达——`chat` 技能
      对任何角色可见，schema 恒非空；留着是防"将来有人把 chat 也收掉"（比如给杂鱼
      加了更窄的可见性规则）而没人发现。不 bind = planner 拿不到 tool_calls，
      按既有的"零调用"路径走，不新增分支。`deny` 交上来时这一支同样兜着（`chat`
      不进 deny，故仍不可达）。
    """
    schema = build_tool_schema(role, evidence=evidence,
                               deny=deny, deny_pseudo=deny_pseudo)
    if not schema:
        return llm
    return llm.bind_tools(schema, tool_choice="auto", parallel_tool_calls=False)


def tool_call_names(decision: PlannerDecision) -> str:
    """trace 用：这次决策**实际点到了**哪些函数名（含被丢弃的并发调用，便于事后核算）。

    ⚠️ 零调用回**空串**、不回 `decision.workflow`（20260927 二稿改）：两者必须可分辨——
    "模型显式点了 `chat`"与"模型一个函数都没点"是**不同的行为证据**（后者是 native
    契约没被遵守的信号，前者是遵守了）。回技能名会把这两格在 trace 里抹成同一个值，
    按 engine 切语料对账时那正是要看的那一格。
    """
    if not decision.raw_tool_calls:
        return ""
    return ",".join(str((c or {}).get("name") or "?") for c in decision.raw_tool_calls)


# ══════════════════════════════════════════════════════════════════════════════
# 复盘器（`graph.reflector_node`）的输出：结构化优先、文本回落（20261019 批 G7）
# ══════════════════════════════════════════════════════════════════════════════
# **为什么住在这一层**：本模块是"模型输出的形状与解析"的接线层——planner 那一路是
# `tools`/`tool_calls`（`tool_calls_to_plan`），复盘器这一路是 `response_format` + JSON。
# 不放进 `agent/graph.py` 或 `agent/prompts.py` 的理由很硬：那两个文件都在行数台账里
# （**只许变小**），塞不进新表；而 schema 与它的解析必须**同处一地**，否则又是两份会漂。
# schema 只带**结构**（类型 / 枚举 / 必填 / 不许加键）：字段**语义**写在
# `_REFLECTOR_PROMPT` 里，不在这儿再写一份 `description`——`json_object` 那一档 schema
# 本来就没人读，契约必须在提示词；两边各写一份只会漂（且漂了没人看得出来）。
REFLECT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["replan", "wrap_up"]},
        "issue": {"type": "string"},
    },
    "required": ["decision", "issue"],
    "additionalProperties": False,
}

# 文本回落支的两条正则（**老口径**，20261019 之前是唯一通道）。留着不是怀旧：
# `json_object` 那一档下 schema 只是装饰，而任何一家都可能在端点侧忽略 response_format
# ——那时模型仍按提示词回 JSON（下面先按 JSON 解），只有它**没**按提示词走时才轮到这两条。
# `[*_`]*` 那两格是 G7 补的：模型极爱写 `**DECIDE:** replan`，而老正则要求关键词后
# 紧跟 `:`/`=` ⇒ 加粗这种最常见的排版**恰恰匹配不到**（静默落成 wrap_up 的那一次）。
# 只许装饰出现在**关键词与冒号之间**：值里的 `_`（`article_id`）一个字都不许动。
_DECOR = r"\s*[*_`]*\s*"
_DECIDE_RE = re.compile(r"DECIDE" + _DECOR + r"[:=]" + _DECOR + r"(\w+)", re.IGNORECASE)
_ISSUE_RE = re.compile(r"ISSUE" + _DECOR + r"[:=]" + _DECOR + r"(.+)", re.IGNORECASE | re.DOTALL)
_DECISIONS = ("replan", "wrap_up")


def _one_line(v: Any) -> str:
    """契约是单行 ⇒ 多行只留第一行（两条路同一口径）。"""
    return str(v or "").strip().split("\n")[0].strip()[:300]


def parse_reflector_output(raw: str) -> tuple[str, str]:
    """复盘器的一次输出 → `(decision, issue)`；**判不了就是 `wrap_up` + 空 issue**。

    20261019 批 G7：此前**只有**文本正则（`DECIDE\\s*[:=]\\s*(\\w+)`），而模型写的
    `**DECIDE:** replan`（加粗）匹配不到 ⇒ 静默落成 `wrap_up`：模型想重规划、系统当成
    收尾（`tests/test_reflector_output.py` 把这三个形状钉住了）。现在 JSON 优先——
    它同时兜住"端点不吃 response_format、但模型照提示词回了 JSON"这条路。

    取值口径（两条路合起来，顺序就是下面四步）：①JSON 对象里的 `issue`（单行化）；
    ②`decision` 取 JSON 里的，**必须是两值之一**（别的写法一律不认，落到 ③）；
    ③没有 JSON（或 JSON 里那个值不合法）时看文本行的 `DECIDE:`；④都没有 ⇒ `wrap_up`。

    ⚠️ 两条路都读不出时**出声**（`logger.warning`）：这一族的教训是"降级路径静默"比
    "降级"坏得多——`wrap_up` 与"判据坏了"在 trace 里长得一样。
    """
    text = (raw or "").strip()
    obj = _first_json_object(text)
    issue = _one_line(obj.get("issue")) if isinstance(obj, dict) else ""
    if isinstance(obj, dict):
        decision = str(obj.get("decision") or "").strip().lower()
        if decision in _DECISIONS:
            return decision, issue
    dm = _DECIDE_RE.search(text)
    if dm:
        im = _ISSUE_RE.search(text)
        return dm.group(1).lower(), issue or (_one_line(im.group(1)) if im else "")
    if text:
        logger.warning("[reflector] 输出既不是约定的 JSON、也没有 DECIDE 行 ⇒ 按 wrap_up "
                       "收尾（这一条**不是**模型的判断，是解析失败）：%.120s", text)
    return "wrap_up", issue


def _first_json_object(text: str) -> Any:
    """从一段可能带围栏/前后缀的文字里取出**第一个** JSON 对象；取不到给 None。

    容的是模型最常见的两种包装：` ```json … ``` ` 与"先说一句话再给对象"。**不做**
    宽松解析（不用 `ast.literal_eval` 兜单引号）：这不是给人手写的输入，宽容只会把
    "模型没按契约答"洗成"答对了"。
    """
    i = text.find("{")
    while i >= 0:
        try:
            obj, _end = json.JSONDecoder().raw_decode(text[i:])
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            return obj
        i = text.find("{", i + 1)
    return None


def invoke_reflector(prompt: str) -> Any:
    """复盘器的一次 LLM 调用：**按服务商绑结构化输出方言**，绑不上就降级纯文本。

    降级**出声**（一条 WARNING）：静默降级的后果不是"判得松一点"，是输出契约没人管
    （`json_object` 那一档的字段契约只长在提示词里，见 `_REFLECTOR_PROMPT`）。

    参数与 20261019 之前逐字相同（`temperature=0.0, max_tokens=300, timeout=30,
    enable_thinking=False`）——复盘是确定性诊断（判两值），不需要任何随机性。
    """
    from config import settings
    from models.llm import response_format

    llm = get_llm(temperature=0.0, max_tokens=300, timeout=30, enable_thinking=False)
    try:
        return llm.bind(response_format=response_format(
            settings._provider_prefix, REFLECT_SCHEMA, name="reflect")).invoke(prompt)
    except Exception as e:  # noqa: BLE001 —— 端点不吃结构化输出 ⇒ 降级，但要让人知道
        logger.warning("[reflector] 结构化输出不可用，降级为纯 prompt 约束：%s: %s",
                       type(e).__name__, e)
        return llm.invoke(prompt)
