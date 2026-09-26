"""native tool calls 接线层（20260927 新主线第一批）。

**为什么存在**：planner 此前靠"系统渲染文本菜单 → 模型写五行文本 → 正则抠"决策
（`SKILL=` / `PARAMS=` / `TOOLS:` 契约见 `agent/graph.py` 的 `plan_encode`）。那条路把
**格式正确性**押在模型的文本纪律上：实测约 15% 的轮次改用 JSON 对象或带引号键回答，
旧内联正则匹配不到 ⇒ 静默落成 chat（主人收到一句假的"我做不到"）。native tool calls
把这一步换成 API 的 `tools` 字段 + `tool_calls` 返回——格式由服务端约定、参数由 schema 约束。

**为什么模型选的是「技能」而不是「工具」**（本模块最关键的一条，是被反证逼出来的）：
初看更自然是"模型直接调工具、我们再反查技能"，核完代码后不成立——
① `planner_node` 的 7 个参数校正器（`_board_quote_fix` / `_announcement_text_fix` /
   `_name_target_fix` / `_name_arg_fix` / `_target_grounding_refusal` /
   `_write_target_refusal` / `_forced_review_fix`）全部读**技能级** `plan_obj["params"]`；
② 技能参数与工具参数**不是同一层**：`navigate` 技能的参数叫 `target`，而它模板里的
   `navigate_to` 工具参数叫 `path`，且由技能自己从 `NAV_MAP` 算出；
③ 工具→技能反查**天生歧义**：`get_moderation_status` 同时出现在两处技能模板里。
⇒ 模型的 function name = **技能名**，`parameters` = 该技能自己的参数。技能模板层因此
仍然是动作工具的唯一入口，`instantiate_plan` 一行不用改。

**为什么这不缩小本模块的价值**：技能集正是渲染 planner 菜单用的那张表
（`visible_skills(role)` × `skill_param_specs`）⇒ schema 与菜单**同源**，"模型能选的"
恒等于"今天这个身份本来就能选的"，没有第二份名单可漂移——**不扩权是结构性的**，
`tests/test_native_plan.py` 另有一条集合相等断言做防御。工具参数上的 `Literal` 闭集
也第一次变成服务端强制的 `enum`（此前只是提示词里的一句话）。

**边界（别过度承诺）**：这一层治格式正确性、参数正确性、单轮多调用。它**不治**
"多步目标跨轮丢失"（那要会话级任务状态），也不改任何行为纪律（短应答还原 / 全选式
短应答 / 授权式 / 写身份防线）——那是**行为**不是格式。所有防线（授权 scope、同意闸、
确认卡、checker、gate）都在这一层的**下游**，因此天然继承、无需改写。

⚠️ **本模块不许 import `agent.graph`**：graph 是消费方，反向 import 会成环。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.skills import (
    ParamSpec,
    callable_query_tools,
    explicit_tools,
    skill_param_specs,
    tool_arg_schemas,
    visible_skills,
)

# 技能参数的短类型名（`ParamSpec.type`，见 `agent/skills.py::arg_type_short`）→ JSON Schema。
# `any` / `?` 是"推不出映射"，不在表里——由 `_param_schema` 兜（见那里的注）。
_TYPE_MAP = {"str": "string", "int": "integer", "num": "number",
             "bool": "boolean", "list": "array", "dict": "object"}


@dataclass(frozen=True)
class NativeDecision:
    """native 档的一次决策结果。**只是数据**——不写 state、不碰库、不发帧。

    `skill` / `params` 交给 `instantiate_plan` 展开，与文本档产出**同构**；其余字段
    只进 trace，供事后按 engine 切语料对账（见本模块头注的"边界"）。
    """
    skill: str
    params: dict
    notes: tuple[str, ...] = ()      # 机器可读的异常记账（如 native_multi_call）
    finish_reason: str = ""          # `length` = 被额度截断（"预算够不够"的判据）
    raw_tool_calls: tuple = field(default_factory=tuple)


def _items_schema(sp: ParamSpec) -> dict:
    """数组参数的 `items`：从 `ParamSpec.from_tool` 回查工具原始片段。

    **为什么必须回查**：`ParamSpec` 只留了短类型名，`list` 丢掉了元素类型，而元素
    类型是有真有假的——`notice_read.ids` 是 integer、`article_tags.add` 是 string。
    一律声明 string 会让模型把 id 写成 `"3"`，而 `check_skill_params` 的归一**不看
    数组元素**，于是这个错误会一路走到工具层才炸（白烧一轮）。`from_tool` 是
    `ParamSpec` 自己的字段、指向同一条派生链，所以这不算引入第二个来源。
    查不到就退化成 string（**宁可少说，不说错**——同 `render_skill_params` 的取向）。
    """
    tool_name, _, arg = sp.from_tool.partition(".")
    frag = ((tool_arg_schemas().get(tool_name) or {}).get("properties") or {}).get(arg)
    if not isinstance(frag, dict):
        return {"type": "string"}
    # 可空数组的片段长这样：`{"anyOf": [{"type":"array","items":{...}}, {"type":"null"}]}`
    # （pydantic 2.13 实测，见 `agent/skills.py::arg_enum` 的同族注），所以要把
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


# ── 手写覆盖：只有这两格，理由是它们**恰恰是 `ParamSpec` 推不出形状的那两格** ──
# `content_query.plan` 是空列表（调用清单由 planner 经 PARAMS.tools/calls 注入，
# 见技能定义），所以 `_template_param_map` 配不到工具、两个参数一律落在 `any`。
# 而它们正是 native 收益最大的地方：白名单此前只是提示词里的一句话，现在可以变成
# 服务端强制的 `enum`。**别顺手往这张表里加别的键**——每加一格就少一格同源保证，
# `tests/test_native_plan.py` 把键集合钉成了字面量，加键必须同改测试（= 有人复核）。
_SCHEMA_OVERRIDES: dict[tuple[str, str], dict] = {
    ("content_query", "tools"): {
        "type": "array",
        "items": {"type": "string"},
        "description": "无参只读数据工具的点名列表（闭集由本轮角色决定）",
    },
    ("content_query", "calls"): {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "args": {"type": "object"},
            },
            "required": ["tool", "args"],
        },
        "description": "带参调用清单：[{tool, args}]，只给当前步",
    },
}


def _override_for(role: str | None, skill_name: str, param: str) -> dict | None:
    """取手写覆盖（含按角色展开的闭集）。没覆盖 → None。"""
    base = _SCHEMA_OVERRIDES.get((skill_name, param))
    if base is None:
        return None
    out = dict(base)
    if skill_name == "content_query" and param == "tools":
        out["items"] = {"type": "string", "enum": explicit_tools(role)}
    elif skill_name == "content_query" and param == "calls":
        # 闭集按角色展开 —— 白名单只有 `callable_query_tools` 这一处来源
        # （见 agent/skills.py 的注：手写名单是漏项来源）。
        out["items"] = {
            **base["items"],
            "properties": {
                **base["items"]["properties"],
                "tool": {"type": "string", "enum": callable_query_tools(role)},
            },
        }
    return out


def build_tool_schema(role: str | None) -> list[dict]:
    """本轮这个身份能选的技能 → OpenAI `tools` 数组。

    **来源只有一处**：`visible_skills(role)` × `skill_param_specs(skill)`——与渲染
    planner 菜单（`build_planner_context`）用的是同一张表。因此"模型能选的"恒等于
    "今天这个身份本来就能选的"：管理员技能在 `role != "admin"` 时结构上选不出来，
    不需要在这里再判一次角色。

    `complete_when` 拼进 description：它在文本档里本来就只进提示词、**没有强制点**
    （没有任何代码读它），搬进 description 是等价的，且比原来离决策更近。
    """
    tools: list[dict] = []
    for skill in visible_skills(role):
        specs = skill_param_specs(skill)
        props: dict[str, dict] = {}
        required: list[str] = []
        for name, sp in specs.items():
            props[name] = _override_for(role, skill.name, name) or _param_schema(sp)
            if sp.required:
                required.append(name)
        desc = skill.description
        if skill.complete_when:
            desc = f"{desc}\n完成判定：{skill.complete_when}"
        fn: dict[str, Any] = {"name": skill.name, "description": desc,
                              "parameters": {"type": "object", "properties": props}}
        # 无必填就**不写** `required` 键：写空数组与不写语义相同，但少一个字段就少
        # 一处网关差异（有些网关对空 required 的处理不一致）。
        if required:
            fn["parameters"]["required"] = required
        tools.append({"type": "function", "function": fn})
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


def tool_calls_to_plan(resp: object, role: str | None) -> NativeDecision | None:
    """一次 `tool_calls` 返回 → `NativeDecision`；**判不了就返回 None**（调用方兜底）。

    None 的三种来源（都交给调用方退回既有的文本解析路径，**不猜**）：
      · `invalid_tool_calls` 非空 —— 参数被截断或不是合法 JSON（思考链吃光
        max_tokens 时 `finish_reason=length`，arguments 会断在半截）；
      · 模型点了不在本轮 schema 里的函数名；
      · `args` 不是对象（我们的 schema 全是 object，出现别的形状说明响应不合约定）。

    一条都没发 ⇒ 这是**闲聊轮**，等价文本档的 `SKILL=chat`：`params` 给**空**。

    ⚠️ **别把模型那句话塞进 `params.reply`**（20260927 一稿就是这么写的，跟着数据核
    完改掉了）：`chat` 技能在注册表里 `inputs={}`、`skill_param_specs` 也是空的，
    而 `planner_node` 会把 `PARAMS=` 原样写进计划文本（`plan_encode`）交给 narrator
    ——所以① 那句话**不会**被任何代码当字段读走；② 会白白记一条 `param_unknown`
    告警（那个信号是**注册表与提示词漂移的探针**，每轮闲聊都报一次等于把它淹掉）。
    文本档在这一格给的就是空 `PARAMS`，native 照做才是"同构"。
    回复正文由 narrator 依据 `reply_contract` 生成——**两条路都是这样**，不是本模块的取舍。

    content 也空 ⇒ 返回 None，由调用方走既有收尾（`_wrap_up_plan`），不在这里编一句话。
    """
    if list(getattr(resp, "invalid_tool_calls", None) or ()):
        return None
    calls = list(getattr(resp, "tool_calls", None) or ())
    base = {"finish_reason": finish_reason(resp), "raw_tool_calls": tuple(calls)}
    if not calls:
        if not _content_of(resp).strip():
            return None
        return NativeDecision(skill="chat", params={}, **base)
    head = calls[0] if isinstance(calls[0], dict) else {}
    name = str(head.get("name") or "")
    if name not in {s.name for s in visible_skills(role)}:
        return None
    args = head.get("args")
    if not isinstance(args, dict):
        return None
    notes: list[str] = []
    if len(calls) > 1:
        # 并发调用本应由 `parallel_tool_calls=False` 挡在服务端；网关若忽略该参数，
        # 这里只取第一条、其余记账。**绝不**把多个技能拼进同一份计划——那会绕过
        # "一张确认卡只装同一个技能的动作"（见 mainline §6.5）。
        rest = ",".join(str((c or {}).get("name") or "?") for c in calls[1:])
        notes.append(f"native_multi_call:{name}|{rest}")
    return NativeDecision(skill=name, params=args, notes=tuple(notes), **base)


def bind_native(llm: object, role: str | None) -> object:
    """把 LLM 绑上本轮的 schema。**`tool_choice` 固定 `auto`、`parallel_tool_calls=False`**。

    · `auto` 而不是 `required`：强制会把闲聊轮也逼成一次假技能调用（模型明明该答
      "你好呀"却得点一个技能）。带参清单的白名单因此靠 schema 的 `enum` 约束，
      而不是靠"必须调工具"。
    · `parallel_tool_calls=False`：下游"一张确认卡只装同一个技能的动作"是按一轮一条
      设计的（见 `docs/native-toolcalls-mainline.md` §6.5）。网关若忽略这个参数，
      `tool_calls_to_plan` 只取第一条并记账——**两道都在**，不互替。
    """
    return llm.bind_tools(build_tool_schema(role), tool_choice="auto",
                          parallel_tool_calls=False)


def tool_call_names(decision: NativeDecision) -> str:
    """trace 用：这次决策**实际点到了**哪些函数名（含被丢弃的并发调用，便于事后核算）。

    ⚠️ 零调用回**空串**、不回 `decision.skill`（20260927 二稿改）：两者必须可分辨——
    "模型显式点了 `chat`"与"模型一个函数都没点"是**不同的行为证据**（后者是 native
    契约没被遵守的信号，前者是遵守了）。回技能名会把这两格在 trace 里抹成同一个值，
    按 engine 切语料对账时那正是要看的那一格。
    """
    if not decision.raw_tool_calls:
        return ""
    return ",".join(str((c or {}).get("name") or "?") for c in decision.raw_tool_calls)
