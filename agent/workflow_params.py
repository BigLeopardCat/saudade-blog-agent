# -*- coding: utf-8 -*-
"""技能参数的 schema 层：生成侧规格 + 事前校验 + 实参绑定判据。

**为什么单独成模块**（20261017 批 2 刀 2c）：这一族原先住在 `agent/workflows.py`，
而它与注册表字面量**没有任何关系**——它读的是工具自己的 `args_schema`（`tools/base.py`）
与技能的 `inputs`/`plan` 模板，消费方是菜单渲染（`render_workflow_params`，住在
`agent/workflow_menu.py`）、实例化（`agent/workflow_plan.py`）、planner 侧
（`agent/planner_llm.py`）、跨轮任务（`agent/tasks.py`）与 gate。定义跟着消费方走。

**分层纪律**（与 `agent/workflow_model.py` 同一条）：本模块的出边**只有三层**——
标准库、`agent.workflow_model`（`Workflow` / `ParamSpec` / `_NO_DEFAULT`）、
`agent.refs`（`is_ref`）与 `tools.base`（惰性取工具注册表）。**不许反向 import**
`workflow_menu` / `workflow_plan` / `workflows`：菜单与校验读的是同一份规格
（`ParamSpec`），而两者谁也不该 import 谁——这就是规格当初下沉的原因。

⚠️ `split_tool_spec` / `_TOOL_SPEC_RE`（`工具名({json})` 的字面形状）**没有**跟着
这一族走：它们不是参数 schema，而是执行清单条目的形状，真正的家是
`agent/tool_specs.py`（那边的头注已经点了名）。今天搬不过去——`tool_specs` →
`agent.decisions` → `agent.workflows` 是一条现成的链，再让 `workflows` →
`tool_specs` 就成环。等收尾刀把 `agent/workflows.py` 删掉、各 import 方改指新模块
之后，它就能搬回自己的家。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from typing import Any

from agent.refs import is_ref
from agent.workflow_model import _NO_DEFAULT, ParamSpec, Workflow

__all__ = [
    "TOOL_ARG_TYPE_SHORT",
    "arg_enum",
    "arg_type_short",
    "args_are_bound",
    "check_call_args",
    "check_workflow_params",
    "expand_template_args",
    "param_problem_note",
    "tool_arg_schemas",
    "workflow_param_specs",
]

# 本模块**自己的** logger（`%(name)s` 那一列因此显示 `agent.workflow_params`）——
# 从 `agent/workflows.py` 搬出来时不再共用那个名字：日志里指着一个文件、代码在另一个
# 文件，正是"看着在跑、其实没跑"那一族。人类可读前缀仍是 `[workflows]`（它是这套
# 技能注册表子系统的标签，不是文件名的标签），所以按前缀 grep 的老办法照旧有效。
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 技能参数 schema（参数通道 schema 化的校验侧，20260925）
# ---------------------------------------------------------------------------
# 为什么要有这一段：planner 填 PARAMS 时，菜单里只有**中文散文**（`inputs`）——
# 必填/类型/默认值一概看不见，它只能靠常识猜；而 `instantiate_plan` 拿到 PARAMS 后
# **什么都不查**。后果两类，都是实测过的形态：
#   · 必填漏了 → 模板展开出 `{"effect": null}` 送进工具 → pydantic 报错 →
#     `__ERROR__` 帧 → 白烧一轮规划（而"缺哪个参数"这一轮就该说清楚）；
#   · 参数名写错/臆造 → **静默忽略**（planner 以为传了、其实没人读）——与
#     "剔空白名单静默"同族，20260913 那次事故的教训在这里同样成立。
# 生成侧那半（只读工具菜单的 `名字:类型*`）20260925 已由 `graph._menu_arg_signature`
# 补上；这里补**技能这半**，并且**菜单与校验读同一份规格**（判据与展示同源不同形）。
#
# 三条来源约定（都是"不造第二份真相"）：
#   · **类型/默认值从工具自己的 `args_schema` 派生**：技能模板里的 `$占位` 与工具
#     参数一对一，因此不需要另维护一张手写参数表（手写名单是漏项来源——20260913
#     工具枚举、20260925 菜单签名，两次教训一致）；
#   · **推不出映射的参数**（navigate 的 target、content_query 的 tools/calls、
#     read_article 的 article_id：由技能自己的代码消费）→ 类型 `any`，**不校验类型**，
#     只保留散文说明；
#   · **枚举的来源 = 工具类型上的 `Literal[...]`**（20260926 批 5 落地）：闭集写在
#     参数类型上 ⇒ pydantic 渲染进工具的 JSON Schema（`{"enum": [...]}`），本层用
#     `arg_enum()` 派生（`ParamSpec.choices`），**不造第二份手写枚举表**。此前这条
#     注释写的是"枚举没有来源 ⇒ 本层不造枚举表"——那句在工具侧补上 `Literal` 之后
#     就过期了，改的是**工具**、不是在这里手抄一份。
#     ⚠️ **带别名归一层的参数仍然没有枚举**（`audit_board_comment.verdict` 的
#     `通过`/`驳回` 等）：pydantic 跑在工具体之前，写成 `Literal` 会先拒掉中文写法，
#     把关不掉的那条容错通道关死——那几族的闭集来源仍是 `adminops` 的别名表。
# `_NO_DEFAULT` 哨兵与 `ParamSpec`（参数规格）已下沉到 `agent/workflow_model.py`
# （20261017 批 2 刀 2a）：菜单渲染（`workflow_menu`）与事前校验（`workflow_params`）
# 共用同一份规格，而两者谁也不该 import 谁。


# JSON-Schema 属性片段 → 短类型名。**纯展示与校验共用的写法映射**，不是判据本身。
# （20260925 从 `graph._menu_arg_type` 搬来这里：技能菜单与工具菜单必须同一套写法，
#   两处各写一份就会漂移成"str"和"string"两种叫法。）
TOOL_ARG_TYPE_SHORT = {"string": "str", "integer": "int", "number": "num",
                       "boolean": "bool", "array": "list", "object": "dict"}


def arg_type_short(spec: object) -> str:
    """JSON-Schema 的属性片段 → 短类型名；可空（`anyOf` 里带 `null`）取非 null 那一支。"""
    if not isinstance(spec, dict):
        return "?"
    for key in ("anyOf", "oneOf"):
        cand = spec.get(key)
        if isinstance(cand, list):
            for one in cand:
                if isinstance(one, dict) and one.get("type") not in (None, "null"):
                    return TOOL_ARG_TYPE_SHORT.get(str(one["type"]), str(one["type"]))
    t = spec.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    return TOOL_ARG_TYPE_SHORT.get(str(t), str(t)) if isinstance(t, str) else "?"


def arg_enum(spec: object) -> tuple[str, ...]:
    """JSON-Schema 的属性片段 → 闭集取值（`enum`）；没有闭集 → 空元组。

    闭集的来源是**工具类型上的 `Literal[...]`**（见 `tools/base.py` 那段契约），这里
    只做派生，不判谁该有闭集。

    ⚠️ 可空参数（`Optional[Literal[...]]`）的 `enum` **嵌在 `anyOf` 里**、不在顶层：
    `{"anyOf": [{"enum": [...], "type": "string"}, {"type": "null"}]}`（pydantic 2.13
    实测）。只看顶层会得出"它没有闭集"——`get_moderation_status.status` 正好是这一格
    （而它恰恰是那个**静默失效**的参数，见 `check_workflow_params` 的注）。所以遍历写法
    与 `arg_type_short` **逐字同构**：两处各写一套必然漂移成"一个读得到一个读不到"。
    """
    if not isinstance(spec, dict):
        return ()
    for key in ("anyOf", "oneOf"):
        cand = spec.get(key)
        if isinstance(cand, list):
            for one in cand:
                got = arg_enum(one)
                if got:
                    return got
    e = spec.get("enum")
    if isinstance(e, list) and e and all(isinstance(x, str) for x in e):
        return tuple(e)
    return ()


_TOOL_ARG_SCHEMAS: dict[str, dict] = {}   # 工具名 → {"properties": {...}, "required": frozenset}（首次用时建一次）
# ⚠️ 20261012 加锁：此前是裸 dict 的 check-then-act —— execute 跑在线程池里、工具会被
# 并发调用，两个线程可以同时进 `if not _TOOL_ARG_SCHEMAS` 各自往**同一个全局 dict** 里
# 填；而调用方正拿着这个 dict 迭代时就会 `RuntimeError: dictionary changed size during
# iteration`。与 `tools/base.py::_last_display_lock` 是同一类竞态、同一套处理：
# 填的过程在锁内、**只在整张表建好之后**才发布（半成品不对外可见）。
_TOOL_ARG_SCHEMAS_LOCK = threading.Lock()


def tool_arg_schemas() -> dict[str, dict]:
    """工具名 → `{"properties": {参数名: JSON-Schema 片段}, "required": frozenset(必填名)}`。

    **首次调用时建、之后复用**（import 期不做这活：工具注册表在 import 期未必就绪，
    而且大多数进程（测试子集）根本不查参数）。
    `required` 是 **JSON Schema 里与 `properties` 平级的那个数组**——别往每个字段里塞
    自己发明的标记（那样 pydantic 不认识、我们也就读不到了）。
    """
    if not _TOOL_ARG_SCHEMAS:
        with _TOOL_ARG_SCHEMAS_LOCK:
            if not _TOOL_ARG_SCHEMAS:      # 双检：另一个线程可能刚建好
                # 同文件已在模块级导入 tools.base，这里只是取注册表
                from tools.base import get_all_tools
                built: dict[str, dict] = {}
                for t in get_all_tools():
                    schema = getattr(t, "args_schema", None)
                    try:
                        js = schema.model_json_schema() if schema is not None else {}
                    except Exception:   # 取不到就跳过：**不因为一个工具没有 schema 而拦住参数校验**
                        logger.warning("[workflows] 取 %s 的 args_schema 失败，该工具的参数不参与校验",
                                       getattr(t, "name", t))
                        continue
                    if not isinstance(js, dict):
                        continue
                    req = js.get("required")
                    built[t.name] = {
                        "properties": js.get("properties") or {},
                        "required": frozenset(req) if isinstance(req, list) else frozenset(),
                    }
                _TOOL_ARG_SCHEMAS.update(built)
    return _TOOL_ARG_SCHEMAS


def _template_param_map(workflow: Workflow) -> dict[str, tuple[str, str]]:
    """技能参数名 → (工具名, 工具参数名)：从 `workflow.plan` 模板的 `$占位` 反查。

    只认 `$名字` 这一种（`$tool[0].field` 那种参数引用是另一层语义，由 execute 解析，
    见 `agent/refs.py`）——与 `instantiate_plan` 模板展开时的判据逐字相同。
    """
    out: dict[str, tuple[str, str]] = {}
    for tool_name, tmpl in (workflow.plan or ()):
        for arg, val in tmpl.items():
            if isinstance(val, str) and val.startswith("$") and not is_ref(val):
                out.setdefault(val[1:], (tool_name, arg))
    return out


def workflow_param_specs(workflow: Workflow) -> dict[str, ParamSpec]:
    """这个技能的合法参数表：名字 → 规格。见本节头注的三条来源约定。

    **合法集合 = `inputs` 的键**（既有事实，不新增名单）。工具的 `args_schema` 只用来
    给这些名字**派生类型/默认值/必填**（按名字与模板占位符配对），不用来扩集合——
    理由是可证伪的：模板占位符里有一类是**给工具看的管道**，不是给 planner 填的参数。
    navigate 就是现场：它的模板写着 `$path`、`confirm` 写死成字面量 `False`（跳转恒直达，
    20260926 起删掉了 `mode`），而 `path` 由技能自己的代码算出来（`NAV_MAP` 的映射结果）
    ⇒ 模板里那一行是**死代码**；把它当成 planner 可填的参数渲染进菜单，等于请它去填一个
    没人读的参数（它真填了还会被判成"本技能参数"，连"没人读"的告警都不会响）。
    """
    tmap = _template_param_map(workflow)
    schemas = tool_arg_schemas()
    out: dict[str, ParamSpec] = {}
    for name, desc in workflow.inputs.items():
        tool_name, arg = tmap.get(name, ("", ""))
        entry = schemas.get(tool_name) or {}
        prop = (entry.get("properties") or {}).get(arg) if tool_name else None
        required = bool(arg) and arg in (entry.get("required") or frozenset())
        default: Any = prop.get("default", _NO_DEFAULT) if isinstance(prop, dict) else _NO_DEFAULT
        out[name] = ParamSpec(type=arg_type_short(prop) if prop else "any",
                              required=required,
                              default=default,
                              desc=str(desc),
                              from_tool=f"{tool_name}.{arg}" if tool_name else "",
                              choices=arg_enum(prop) if prop else ())
    # 必填以**技能自己的声明**为准（工具形状只给了个默认）——技能参数与工具参数
    # 不是同一层：`device_display.text` 在 pydantic 里必填，但技能在代码侧被空参调用
    # （文案由执行层创作），这种"形状必填、策略可选"的分叉必须由技能自己说清楚。
    for name in workflow.required_params:
        if name in out:
            out[name] = replace(out[name], required=True)
    for name in workflow.optional_params:
        if name in out:
            out[name] = replace(out[name], required=False)
    return out


def _contains_ref(value: Any) -> bool:
    """值里（含 list/dict 元素位置）是否含**未解析的参数引用**（`$tool[N].field`）。

    引用是**程序化取值**、不是字面量：它的类型由被引用的那次工具返回决定，规划期
    根本不知道 ⇒ 一律**放行、不做类型判据**（"类型不对"在这里只可能是误报）。
    判据本身来自 `agent.refs`（`REF_RE`），这里只做容器的递归遍历。
    """
    if is_ref(value):
        return True
    if isinstance(value, dict):
        return any(_contains_ref(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_ref(v) for v in value)
    return False


def _coerce_value(want: str, value: Any) -> tuple[bool, Any]:
    """按派生的类型把值归一成工具能收的形态 → `(能不能用, 归一后的值/说明)`。

    只做**无损**的那几种（"3"→3、123→"123"、真假词→bool）：它们与 pydantic 的宽松
    模式同解，归一后工具行为不变，只是省掉一次 `__ERROR__` 帧。归不了的**不许猜**
    （猜一个值去写操作比报错更坏，同写路径"缺参守卫"的取向）。
    """
    if want in ("any", "?", ""):
        return True, value
    if want == "str":
        if isinstance(value, str):
            return True, value
        if isinstance(value, bool):
            return True, "true" if value else "false"
        if isinstance(value, (int, float)):
            return True, str(value)
        return False, f"要字符串，收到 {type(value).__name__}"
    if want == "int":
        if isinstance(value, bool):
            return False, "要整数，收到布尔"
        if isinstance(value, int):
            return True, value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return True, int(value.strip())
        return False, f"要整数，收到 {value!r}"
    if want == "bool":
        if isinstance(value, bool):
            return True, value
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
            return True, value.strip().lower() in ("true", "1", "yes")
        if isinstance(value, int) and value in (0, 1):
            return True, bool(value)
        return False, f"要布尔，收到 {value!r}"
    if want == "list":
        return (True, value) if isinstance(value, list) else (False, "要列表")
    if want == "dict":
        return (True, value) if isinstance(value, dict) else (False, "要对象")
    return True, value      # 不认识的类型标记：不拦（宁可少说）


def _bad_choice(name: str, value: Any, choices: tuple[str, ...]) -> str | None:
    """值不在闭集里 → 一句"为什么用不了"（含**合法取值**）；在闭集里/没有闭集 → None。

    与 `_coerce_value` 的分工：那个判"类型收不收得下"，这个判"值在不在闭集里"，合起来
    才是"这个参数能不能用"。**为什么必须补这一条**（20260926 批 5）：闭集此前只活在
    散文说明与工具体内 ⇒ 一个**看似合理的臆造值**（`doc_type="article"`、
    `status="ai_reject"`）会一路走到工具，然后分两种坏法：
      · 撞 `KeyError` → `__ERROR__` 帧（白烧一轮，且 planner 从错误里读不出合法取值）；
      · 被工具**静默降级**成默认视图——`get_moderation_status.status` 落到
        `render_moderation_status` 的 `focus = status if status in FOCUS_NAMES else None`，
        回的是混合视图，而叙述完全可以把它说成"这些是被驳回的"，**答错且无声**。
    报成 `bad` ⇒ 与"缺必填/类型不对"走同一条道：零工具 + 这句事实交回 planner 同轮改，
    不猜、也不烧工具轮。合法取值直接印在这句话里（planner 下一轮就看得见）。
    """
    if not choices or not isinstance(value, str):
        return None
    if value in choices:
        return None
    return f"{name}={value!r}（不在可选值里：{' / '.join(choices)}）"


def check_workflow_params(workflow: Workflow, params: dict,
                       specs: dict[str, ParamSpec] | None = None) -> dict:
    """PARAMS 事前校验（纯函数）→ `{"fixed": [...], "unknown": [...], "missing": [...], "bad": [...]}`。

    · `fixed`：归一过的值（`article_id "3"→3`），只作说明——**不是问题**；
    · `unknown`：系统不认识的参数名（planner 写了但没人读）——**响亮但不阻断**
      （没有"点名却未执行"的损失，工具照常跑，见 planner_node 的 trace 事件）；
    · `missing` / `bad`：必填没给、值归不了 → **调用方必须零工具**（展开成 `null`
      实参只有一条路：工具层报错，白烧一轮）。

    **作用范围**（20260925 定）：只覆盖 `instantiate_plan` 的**通用模板分支**——
    写技能各自的 `_expand_*` 守卫更专门（缺参零工具 + 自己的追问话术），navigate 的
    target 另有 NAV_MAP 这条更准的判据，content_query 的条目走 `check_call_args`。
    换句话说：**哪儿模板会被原样展开、哪儿才需要这一层**，不是全局门。
    """
    specs = specs if specs is not None else workflow_param_specs(workflow)
    params = params if isinstance(params, dict) else {}
    fixed, unknown, missing, bad = [], [], [], []
    for name, value in params.items():
        if name not in specs:
            unknown.append(name)
            continue
        if value in (None, ""):
            continue                      # 空值当"没给"处理（missing 那一支判）
        if _contains_ref(value):
            continue                      # 参数引用：类型由被引用的返回决定，规划期不判
        ok, got = _coerce_value(specs[name].type, value)
        if not ok:
            bad.append(f"{name}={value!r}（{got}）")
            continue                  # 类型都不对，闭集无从谈起
        why = _bad_choice(name, got, specs[name].choices)
        if why:
            bad.append(why)
            continue                  # 值不在闭集里：**不要**再记成"归一过"（会自相矛盾）
        if got is not value and got != value:
            fixed.append(f"{name} {value!r}→{got!r}")
    for name, sp in specs.items():
        if sp.required and params.get(name) in (None, ""):
            missing.append(name)
    return {"fixed": fixed, "unknown": unknown, "missing": missing, "bad": bad}


def param_problem_note(workflow: Workflow, chk: dict, specs: dict[str, ParamSpec] | None = None) -> str:
    """参数不齐/不可用时给 planner 的确定性话术（零 LLM、只说机器能保证的事实）。

    **不许**出现"站内没有/查不到"这类台账话术——这一层压根没读过台账，缺的是参数。
    这句话就是 planner 下一轮看到的"工具帧"，因此它必须**同时**给出：缺什么、本技能
    收哪些参数（带类型与必填标记）、下一步能做什么。
    """
    why = []
    if chk["missing"]:
        why.append("必填参数没给：" + "、".join(chk["missing"]))
    if chk["bad"]:
        why.append("参数值用不了：" + "、".join(chk["bad"]))
    specs = specs if specs is not None else workflow_param_specs(workflow)
    sig = "、".join(f"{n}:{s.type}{'*' if s.required else ''}"
                    for n, s in specs.items())
    return (f"{workflow.name}：{'；'.join(why)}（本技能参数：{sig}）：不调用任何工具，"
            f"重新决策——参数要么补齐（主人原话里能取到就据实填、取不到就如实问清），"
            f"要么改用别的技能（比如 WORKFLOW=chat 如实说明）")


def _param_problem_plan(workflow: Workflow, chk: dict,
                        specs: dict[str, ParamSpec] | None = None,
                        note: str | None = None) -> dict:
    """参数不合格时的零工具计划（`instantiate_plan` 的几个分支共用这一份形状）。

    `dropped` 刻意留空、另给 `param_problem`：`dropped` 的语义是**工具可达性**
    （"你够不到这个工具"），而这里工具可达、是参数不齐——混进同一个键会让
    `_drop_correction` 把两件事讲成一件。

    `note`（20260929 批 G）：给了就用它当注记，否则现算一份通用的
    `param_problem_note`。**这个口子是必须的**——`planner_node` 的 `param_correct`
    把 `plan_obj["note"]` 当**纠偏正文**喂回给 planner，而各写族展开函数写好的那句
    （"缺少正文（text）：…如实向主人问清指的是哪一条（可先选 dashboard_todo_list
    把列表读出来给他挑）"）比通用话术具体得多：用通用话术盖掉它，等于把"该填哪个
    参数、下一步去读哪"这些**机器已知的事实**又还回给模型猜。
    三个既有调用点（navigate 的事前校验、模板占位符、本批的零工具兜底）都不传它，
    行为与从前逐字相同。
    """
    return {
        "workflow": workflow.name,
        "tools": [],
        "dropped": [],
        "param_unknown": chk.get("unknown") or [],
        "note": note or param_problem_note(workflow, chk, specs),
        "reply": workflow.reply_contract,
        "chat": False,
        # 参数不齐是**系统已知**的处境（不是猜的）：批 3 的洞④ 豁免读这个值
        # （旧判据 grep 注记里的「不调用任何工具」，而注记文案是人写的）。
        "status": "param_missing",
        "param_problem": chk,
    }


def args_are_bound(args: Any) -> bool:
    """这份实参**能不能脱离本轮现场直接执行**（＝这一步算不算"已绑定实参"）。

    两个不是"已经能调"的形态，**都来自展开器自己的产出**，所以判据贴着它：
      · 值里有 `None` ⇒ 必填参数没给到（`expand_template_args` 只为"必填而没值"写
        None，可选而没值的那一格它直接不落键）——真发出去只会换回一个 `__ERROR__`；
      · 值里有**未解析的参数引用** `$tool[N].field` ⇒ 它的值要等**前面那次调用**回来
        才知道，本轮的现场之外根本取不到（`expand_template_args` 刻意原样透传，见
        它的注）。跨轮把一个带引用的步骤当"已绑定"去执行 = 拿着一个永远取不到值的
        占位符去写。

    空 dict **算已绑定**（`list_tags({})` 这种无参工具本身就是完整的一次调用）——
    它和"还不知道怎么调"（没有 `args` 这一格）是两件不同的事，别在这里合流。
    """
    if not isinstance(args, dict):
        return False
    return all(v is not None and not _contains_ref(v) for v in args.values())


def expand_template_args(tmpl: dict, params: dict,
                         specs: dict[str, ParamSpec] | None = None) -> dict:
    """把 `workflow.plan` 的参数模板实例化成工具实参（通用分支的唯一展开点）。

    `$名字` = 取 PARAMS 里同名参数；**参数引用**（`$tool[0].field`）是另一层语义，
    必须原样透传给 execute 解析——否则这里会去 PARAMS 里查 `"tool[0].field"` 拿到
    None，把引用悄悄变成空参数（20260919 两套 `$` 语法共存的口子）。

    20260925 补一条：**没给值的可选参数不落进实参**。此前模板里的 `$action` 会展开成
    `{"effect": "sakura", "action": null}`——而 `toggle_effect` 的 `action` 有默认值
    `"on"`，显式传 null 反而**覆盖掉**默认值、直接被 pydantic 判非法 ⇒ 白烧一轮。
    "模板里写了 `$p` 而 planner 没填"的语义是**用工具的默认值**，不是"显式传空"。
    （只丢 None/空串；`[]`/`0`/`False` 都是有语义的值，一律保留。）
    """
    specs = specs if specs is not None else {}
    args: dict = {}
    for k, v in tmpl.items():
        if not (isinstance(v, str) and v.startswith("$") and not is_ref(v)):
            args[k] = v
            continue
        got = params.get(v[1:])
        sp = specs.get(v[1:])
        if got in (None, "") and (sp is None or not sp.required):
            continue
        args[k] = got
    return args


def check_call_args(tool_name: str, args: dict) -> dict:
    """`content_query` 的 `PARAMS.calls[].args` 校验 → `{"args": 归一后的, "bad": [...]}"`。

    用的是**同一个** `tool_arg_schemas()`（单一来源）：必填没给、值归不了 → 这条例目
    被剔除（调用方把原因拼进 `dropped` 后缀）；只多写了系统不认识的参数 → **只剔掉
    那个参数**、调用照常（pydantic 本来就忽略多余字段，剔掉它只是让"已忽略"这件事
    在 trace 里留痕而不是静默）；把某个可选参数显式填成**它自己的默认值** → 同样剔掉，
    理由见下面那一支（"两条通道合成同一个字形"，20261005）。
    """
    schemas = tool_arg_schemas()
    entry = schemas.get(tool_name)
    if entry is None:                      # 注册表里没有/没有 schema：不拦（白名单那一层已判过工具名）
        return {"args": dict(args), "bad": [], "unknown": []}
    props, req = entry.get("properties") or {}, entry.get("required") or frozenset()
    out, bad, unknown = {}, [], []
    for name, value in (args or {}).items():
        if name not in props:
            unknown.append(name)
            continue
        want = arg_type_short(props[name])
        if _contains_ref(value):
            out[name] = value
            continue
        if value in (None, ""):
            # 没给值的**可选**参数不落进实参（同 `expand_template_args` 的理由：显式
            # null 会覆盖掉工具自己的默认值、被 pydantic 判非法）。必填那一格由下面的
            # `req` 循环照旧报"缺必填"——所以这里直接丢掉不影响它。
            continue
        ok, got = _coerce_value(want, value)
        if ok:
            # 闭集校验与技能参数那条**同源同判**（`arg_enum` 读同一份工具 schema）。
            # 今天 calls 白名单里的工具只有 `get_article_detail.doc_type` 带闭集——
            # 而它此前正是"臆造值 ⇒ 字典 KeyError ⇒ __ERROR__ 帧"的那一格。
            why = _bad_choice(name, got, arg_enum(props[name]))
            if why:
                bad.append(why)
            elif "default" in props[name] and got == props[name]["default"]:
                # **显式填了默认值本身**的参数也不落进实参（20261005）。执行侧与"没填"
                # 逐字等价（pydantic 会补上同一个默认值），但它会让**渲染出的 spec 不
                # 同一个字形**——而跨轮的重复调用拦截（`graph._search_retry_kind`）比的
                # 正是 spec 字面量。实测（trace `20261005T194519`）：第 0 轮 planner 走
                # 无参点名通道（渲染成 `list_talks({})`）、第 1 轮改走 calls 通道并顺手
                # 填了默认的 `offset: 0`（渲染成 `list_talks({"offset": 0})`）⇒ 同一件读
                # 被判成两件，白跑一轮 planner + 多打一次上游接口，直到第 2 轮才拦下。
                # 这与上面"没给值的可选参数不落进实参"是同一条道理：判据是**这次调用
                # 实际要什么**，不是"模型写了哪些键"——两条通道因此在同一个字形上合流。
                # 比较用 `==` 不做类型判据：`got` 已被 `_coerce_value` 按 schema 归一，
                # 与同一份 schema 的默认值同类，`True == 1` 那种跨类相等在这条路上无害
                # （落到默认值本身，执行结果一字不差）。
                # 不记 `unknown`（它不是没人读的参数）、不记 `bad`（更不是错），也不打
                # 日志：这是模型的常规写法，逐条告警只会把真问题淹掉。
                continue
            else:
                out[name] = got
        else:
            bad.append(f"{name}={value!r}（{got}）")
    for name in req:
        if (args or {}).get(name) in (None, ""):
            bad.append(f"缺必填 {name}")
    return {"args": out, "bad": bad, "unknown": unknown}


# ---------------------------------------------------------------------------
# 参数名别名层（20261005）：它读 `workflow_param_specs`，所以与上面同住一层
# ---------------------------------------------------------------------------
# 参数名别名层（20261005）：**同一个语义槽在不同技能里叫法不同**。
#
# 现场（`announcement_update_zero_write`，历史 33 次里 5 次）：planner 发
# `{"name": "维护通知", "new_title": "维护通知2"}`，而本技能的合法参数是
# `title`/`new_title`/`content` ⇒ `title` 落进 missing ⇒ 写族的纠偏注记只说"缺少公告
# 标题（title）"、**一个字都没提它写的那个 `name`**（`param_unknown` 只进日志与 trace，
# 不进注记）⇒ planner 看不出"我填的值其实就在手上"，下一轮直接回落 `workflow=chat`，
# 回复成「主人，我这边没有看到执行记录喵～」——**连"去按标题查一次"这个动作都没发生**。
#
# 根因不是"模型笨"，是**参数名没有归一层**：tag/category 族与账号族一律叫 `name`
# （`workflow_param_specs` 实测：tag_update/tag_delete/category_update/category_delete/
# account_freeze/account_unfreeze/account_set_role/mute_account/unmute_account/
# quota_reset/notice_send），公告族却叫 `title`。planner 从多数派学来的名字撞在公告上。
#
# 语义与既有的"取值别名层"同构（`adminops._VERDICT_ALIASES`）。
_PARAM_ALIASES = {"title": ("name",)}


def _param_alias_fix(workflow: Workflow, params: dict) -> tuple[dict, list[tuple[str, str]]]:
    """把 planner 用错的参数名搬到本技能真正的槽上，返回 `(新 params, 搬迁清单)`。

    规则（三条同时成立才搬）：
      ① `src ∈ params` 且 **`src ∉ specs`**——本技能根本不收这个名字；
      ② `dst ∈ specs`——本技能确有这个槽；
      ③ `params.get(dst)` 是 None/空串——**已有值不覆盖**（`new_title` 那类"改成什么"
         与它相邻，双写打架时宁可不猜）。

    ①就是护栏：`name` 在 11 个技能里是**合法**字段 ⇒ 规则对它们不触发；尤其
    `notice_send` **同时**有 `name`（收件人）与 `title`（通知标题），正是这条把它挡在
    门外——否则收件人会被搬去当标题，那是**写操作**上的静默错值。
    触发面因此收敛到 `title ∈ specs` 且 `name ∉ specs` 的那 5 个技能：
    tag_create / category_create / announcement_create / announcement_update /
    announcement_delete——语义一致（都是"目标/新建项的名字"）。

    **纯函数**（不改入参、不读全局、可离线单测）。命中由 `planner_node` record，
    **不许静默**——"悄悄归一"正是本仓那些事故的形状（见 `_drop_correction` 的整段理由）。
    """
    specs = workflow_param_specs(workflow)
    out = dict(params)
    moved: list[tuple[str, str]] = []
    for dst, srcs in _PARAM_ALIASES.items():
        if dst not in specs or out.get(dst) not in (None, ""):
            continue
        for src in srcs:
            if src in out and src not in specs:
                out[dst] = out.pop(src)
                moved.append((src, dst))
                break
    return out, moved
