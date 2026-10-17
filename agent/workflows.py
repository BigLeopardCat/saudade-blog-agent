# -*- coding: utf-8 -*-
"""技能注册表：固定行为（固定流程任务）的静态定义。

产品级"plan 写进 workflow"的落地：执行步骤是模板化数据，不是模型编的自由文本。
planner 只从本注册表选技能 + 填参数（受限规划，不再自由写 STEPS），
instantiate_plan 把模板实例化为计划（两态一次写入：对象是唯一事实源、文本是渲染，
含 TOOLS 行执行清单），execute 确定性逐条执行。

导航映射表是本模块的单一事实来源（页面别名→路径，替代散落在 prompt 里的白名单）：
planner 选 navigate 技能时能看到映射表，"去物联网平台"→ /device-console/ 的
业务知识从此属于系统数据而非模型猜测（修复 planner 跑题的根因）。

架构位置：
  planner（选技能 + 填参数）→ instantiate_plan（技能模板实例化 plan 文本，
  TOOLS 行 = 执行清单）→ execute（确定性逐条执行）→ model（零工具叙述）→ gate（确定性检查）
"""

from __future__ import annotations

import re
from typing import Any

# 依赖导入（20261012 统一上移）：这一组此前夹在模块级数据（`_NEAR_MISS_CONTRACT`、
# `NAV_MAP`）之后，是 ruff E402 全部 27 条里**唯一不在脚本里**的一组——而
# `docs/lint-baseline.md` 当时把 E402 一律说成"脚本形态"，口径与事实不符。
# 位置不改变求值时刻（模块级 import 都在 import 期跑），但把 import 夹在数据之后
# 违反本仓 `pyproject.toml` 自己的 lint 契约，读的时候也分不清"哪些是依赖、哪些是数据"。
# 上移安全：`agent.workflows` 的导入者只有 `eval/` 与 `tests/`，无循环导入。
#
# 各条目为什么长这样：
#   · agent.authz / agent.principal —— 管理读工具清单从 authz 的 scope 表**派生**
#     （20260924）、技能可见性按角色过滤（20260921 管理助手；20260926 起是**管理员族**，
#     超管同权；20261002 加 CHAT_ONLY_ROLES 的否决支，见 visible_workflows）。
#     **20261017 批 2 刀 2e-1 起这两行 import 撤掉了**：两者都只被工具清单族与
#     可见性判据读，而那一族搬进了 `agent/workflow_tools.py`、可见性判据早在刀 2b 就
#     随注册表走了 ⇒ 本模块自己一个都不直接调了，别再往回加。
#   · agent.refs —— 参数引用 `$tool[0].field`（20260919）。刀 2c/2d 之后本模块
#     **一个 refs 判据都不直接调了**（`is_ref` 随 `_norm_id_or_ref` 搬进
#     `agent/workflow_expand.py`，`_contains_ref` 随参数层搬进 `agent/workflow_params.py`）
#     ⇒ 这一行 import 也一并撤掉，别再往回加。
#   · tools.base —— **只剩 `IOT_ENABLED` 一条，而且它只是转发**（`agent/decisions.py`
#     按 `from agent.workflows import IOT_ENABLED` 取它）。IoT 开关与入口路径与工具层
#     **同源**（路径字面量的唯一出处是 tools/base.py，那里的白名单要按同一个开关收口）；
#     开关值本身出自 `config/settings.py` 的 `iot_enabled`。
#     `IOT_NAV_PATH` / `nav_path_valid` / `_NAV_EXACT_PATHS` 20261017 批 2 刀 2f 随
#     实例化层搬进 `agent/workflow_plan.py`（白名单校验在那儿）——全仓没有一处按
#     `agent.workflows` 取它们，本模块的 import 一并撤掉，别再往回加。
#     （`_TODO_TEXT_LIMIT` 与通知的 `_NOTICE_CONTENT_LIMIT` / `_NOTICE_TITLE_LIMIT` /
#       `_QUOTA_NOTE_LIMIT` 批 2 刀 2d 随展开层搬进 `agent/workflow_expand_write.py`；
#       `agent.adminops`（`A`）批 2 刀 2f 随实例化层搬走。同样别再往回加。）
# 计划状态闭集（20261017 批 2 刀 2c）：**专为转发**而导入——闭集的消费方都在别的
# 模块（`agent/graph.py` 的 gate 按值选文案、`agent/plan_codec.py` 的写端校验取值）。
from agent.plan_status import (  # noqa: F401
    PLAN_STATUS_ABSENCE_EXEMPT,
    PLAN_STATUS_NAV_NOTE,
    PLAN_STATUS_VALUES,
)

# 注册表字面量层（20261017 批 2 刀 2b）：47 条 `Workflow(...)` 按域搬进
# `agent/workflow_defs/`，派生件（靶通道表、四个桶、写名单、可见性判据）随注册表走，
# 导航映射表与能力否定词表各随其消费面走。本模块**继续按原名转发**
# （`from agent.workflows import NAV_MAP` 这类既有写法因此逐字不动）——转发是
# **迁移期的临时形态**，批 2 收尾时本模块整块消失，各 import 方各自改指新模块。
# 四处带 `noqa: F401` 的 import 块是"这些名字**专为转发**而导入"的标记：本模块不是它们
# 的消费方，而是别人 import 本模块的入口——那四行随本模块一起消失，别把 noqa 抄去别处。
# （这里**不能**把那个标记原样写出来：一行里出现 `noqa:` 加非代码的尾巴，ruff 会判
# "无效的 noqa 指令"，于是**那一行的抑制整条失效**——20261017 实测踩过。）
from agent.workflow_defs import (  # noqa: F401
    _FREE_TEXT_WRITE_WORKFLOWS,
    _IOT_ONLY_WORKFLOWS,
    _LEDGER_TARGET_WRITE_WORKFLOWS,
    _OWN_WRITE_WORKFLOWS,
    _SYSTEM_ONLY_WORKFLOWS,
    _WRITE_NAME_TARGET_WORKFLOWS,
    _WRITE_SCOPES,
    _WRITE_TARGET_CHANNEL,
    WORKFLOW_MAP,
    WORKFLOWS,
    WRITE_WORKFLOW_NAMES,
    _channel,
    _derive_write_workflow_names,
    visible_workflows,
)
from agent.workflow_defs.capability import (  # noqa: F401
    _NEAR_MISS_CONTRACT,
    _ROSTER_CHECK_TAIL,
    CAPABILITY_DENIAL_OBJECTS,
    CAPABILITY_DENIAL_VERBS,
)
from agent.workflow_defs.nav import (  # noqa: F401
    _IOT_NAV_ALIASES,
    _IOT_OFF_NOTE,
    _NAV_REAL_PAGES,
    _NAV_REF_HINT,
    _PUBLIC_NAME_TAKEN,
    DASHBOARD_PANELS,
    FUZZY_NAV_RULES,
    NAV_MAP,
    NAV_VALID_PATHS,
    _iot_alias_entries,
    fuzzy_nav_hit,
)

# 展开层（20261017 批 2 刀 2d / 2d-2）：本模块仍**消费**其中十二个（`_instantiate_plan`
# 的各展开分支与收尾后缀），其余是**专为转发**而导入——全仓既有写法
# （`from agent.workflows import _norm_id_list` / `_pos_int_list` 等）因此逐字不动。
# 刀 2d-2 为模块预算把**写技能那一族**再切到 `agent/workflow_expand_write.py`，所以这里
# 是两个 import 块：写族走 `..._write`，通用展开层（id 归一、own/todo/article_status）走
# `..._expand`。两个模块都**刻意不写 `__all__`**（整个面都是 `_` 前缀的内部件）。
from agent.workflow_expand import (  # noqa: F401
    _LIST_BREAK,
    _LIST_WRAP,
    _expand_article_status,
    _expand_own_workflow,
    _expand_todo_done_workflow,
    _expand_todo_reschedule_workflow,
    _expand_todo_workflow,
    _norm_id_list,
    _norm_id_or_ref,
    _norm_pos_int,
    _norm_true,
    _pos_int_list,
    _workflow_no_calls_suffix,
)
from agent.workflow_expand_write import (  # noqa: F401
    _PLACEHOLDER_WRAPS,
    _TEMPLATE_TOKEN_RE,
    _expand_change_set,
    _expand_write_workflow,
    _notice_placeholder,
    _tag_names,
    _unfilled_placeholder,
    _write_arg,
    _write_list_arg,
)

# 菜单渲染层（20261017 批 2 刀 2e-2）：本模块**一个都不消费**了（`instantiate_plan` /
# `_instantiate_plan` 不印菜单），整块**专为转发**而导入——全仓既有写法
# （`from agent.workflows import build_planner_context` / `render_workflow_params` 等，
# `agent/graph.py` 与多个测试都在用）因此逐字不动。
from agent.workflow_menu import (  # noqa: F401
    _NAV_MAP_LINES,
    _nav_map_lines,
    _workflow_plan_seq,
    build_planner_context,
    render_tool_marks,
    render_workflow_params,
)

# 下沉层（20261017 批 2 刀 2a）：数据模型与共享原语。本模块**继续按原名转发**
# （`from agent.workflows import Workflow` 这类既有写法因此逐字不动）——转发是
# **迁移期的临时形态**，批 2 收尾时本模块整块消失，54 个 import 方各自改指新模块。
# 刀 2e-2 之后本模块自己只消费两个 DROP_SUFFIX_*（`_instantiate_plan` 的丢弃话术），
# 其余五个随菜单渲染搬进 `agent/workflow_menu.py` ⇒ 这一块也成了**转发块**。
from agent.workflow_model import (  # noqa: F401
    _EXPLICIT_TOOLS_MARK,
    _NO_DEFAULT,
    _PARAM_TOOLS_MARK,
    DROP_SUFFIX_BAD_ARGS,
    DROP_SUFFIX_NOT_OBJECT,
    ParamSpec,
    Workflow,
)

# 参数 schema 层（20261017 批 2 刀 2c）：本模块仍**消费**其中六个（实例化分支、
# 菜单渲染、别名归一），其余是**专为转发**而导入——全仓既有写法
# （`from agent.workflows import check_workflow_params` 等）因此逐字不动。
from agent.workflow_params import (  # noqa: F401
    _PARAM_ALIASES,
    _TOOL_ARG_SCHEMAS,
    _TOOL_ARG_SCHEMAS_LOCK,
    TOOL_ARG_TYPE_SHORT,
    _bad_choice,
    _coerce_value,
    _contains_ref,
    _param_alias_fix,
    _param_problem_plan,
    _template_param_map,
    arg_enum,
    arg_type_short,
    args_are_bound,
    check_call_args,
    check_workflow_params,
    expand_template_args,
    param_problem_note,
    tool_arg_schemas,
    workflow_param_specs,
)

# 实例化层（20261017 批 2 刀 2f）：**专为转发**而导入——`agent/graph.py` 与多个测试
# 都按 `from agent.workflows import instantiate_plan` 取它，逐字不动。
from agent.workflow_plan import (  # noqa: F401
    _instantiate_plan,
    instantiate_plan,
)

# 工具清单层（20261017 批 2 刀 2e-1）：**专为转发**而导入——全仓既有写法
# （`from agent.workflows import _EXPLICIT_TOOLS` 等，测试里用得很多）因此逐字不动。
# 刀 2f 之后本模块自己也不再消费它们（白名单校验随实例化层搬走）。
from agent.workflow_tools import (  # noqa: F401
    _ADMIN_QUERY_TOOLS_ORDER,
    _CALLABLE_QUERY_TOOLS,
    _CALLABLE_QUERY_TOOLS_ORDER,
    _EXPLICIT_TOOLS,
    _EXPLICIT_TOOLS_ORDER,
    _admin_extra,
    callable_query_tools,
    explicit_tools,
    param_tools,
)
from tools.base import IOT_ENABLED  # noqa: F401

# 实例化层（`logger` / `instantiate_plan` / `_instantiate_plan`）已搬到
# `agent/workflow_plan.py`（20261017 批 2 刀 2f）：那一族的出边全在已经搬出去的层上，
# 一条边都不指向本文件留下的东西。本模块顶部 import 回来（迁移期转发）。

# ---------------------------------------------------------------------------
# 按名字写数据的共同转述契约（20260925）
# ---------------------------------------------------------------------------
# 已下沉到 `agent/workflow_defs/capability.py`（20261017 批 2 刀 2b）：它只被注册表字面量
# 引用，而字面量现在按域住在那六个模块里。本模块顶部 import 回来（迁移期转发）。

# ---------------------------------------------------------------------------
# 导航映射表（业务唯一数据源）——已下沉到 `agent/workflow_defs/nav.py`（批 2 刀 2b）
# ---------------------------------------------------------------------------
# 别名表 / IoT 别名与 `_IOT_OFF_NOTE` / `_NAV_REF_HINT` / `_NAV_REAL_PAGES` / 后台面板表 /
# `_PUBLIC_NAME_TAKEN` / `NAV_VALID_PATHS` / 口语模糊规则与 `fuzzy_nav_hit` 全在那里。
# 搬走的理由（顺序是契约、判据只能有一处）见该模块头注；本模块顶部 import 回来。


# ── 写技能的**靶通道**与四个桶 —— 已下沉到 `agent/workflow_defs/__init__.py`（批 2 刀 2b）
# `_WRITE_TARGET_CHANNEL` / `_channel` / `_WRITE_NAME_TARGET_WORKFLOWS` /
# `_LEDGER_TARGET_WRITE_WORKFLOWS` / `_OWN_WRITE_WORKFLOWS` / `_FREE_TEXT_WRITE_WORKFLOWS`
# 都跟着注册表走了（它们只读 `WORKFLOWS` 那一张表）。本模块顶部 import 回来（迁移期转发）。
# ── 能力否定判据词（20261003 洞⑫）—— 已下沉到 `agent/workflow_defs/capability.py`（批 2 刀 2b）
# `CAPABILITY_DENIAL_OBJECTS` / `CAPABILITY_DENIAL_VERBS` 只被注册表字面量引用，跟着走了。
# 本模块顶部 import 回来（迁移期转发）。



# ── 数字/清单归一（`_norm_pos_int` / `_LIST_WRAP` / `_LIST_BREAK` / `_pos_int_list`）
# 与展开层（`_expand_own_workflow` / `_expand_todo_*` / `_expand_article_status` /
# `_norm_id_*` / `_norm_true`）已搬到 `agent/workflow_expand.py`（20261017 批 2 刀 2d）；
# 写技能那一族（`_write_arg` / `_tag_names` / `_unfilled_placeholder` / `_notice_placeholder` /
# `_expand_change_set` / `_expand_write_workflow`）在刀 2d-2 落到
# `agent/workflow_expand_write.py`：那一族**一条出边都没有**，本模块顶部 import 回来
# （迁移期转发）。

# `NAV_VALID_PATHS` 已随导航映射表搬到 `agent/workflow_defs/nav.py`（批 2 刀 2b）；
# 本模块顶部 import 回来（迁移期转发）。

# ── planner 可点名的工具清单（`_EXPLICIT_TOOLS_ORDER` / `_CALLABLE_QUERY_TOOLS_ORDER` /
# `_ADMIN_QUERY_TOOLS_ORDER` 与三个按角色取法 `callable_query_tools` / `explicit_tools` /
# `param_tools`）已搬到 `agent/workflow_tools.py`（20261017 批 2 刀 2e-1）：那一族**一条
# 出边都没有**，本模块顶部 import 回来（迁移期转发）。


# 技能描述里的工具枚举标记（20260924）：枚举**不能是模块常量**（角色相关，得在
# build_planner_context 里按 role 展开）。两个标记本体已下沉到
# `agent/workflow_model.py`（20261017 批 2 刀 2a）——注册表字面量与菜单渲染器都要
# 看得见它们，而两边谁也不该 import 谁；本模块顶部已 import 回来，行为逐字不变。


# `render_tool_marks`（技能描述里的工具枚举标记按角色展开）已搬到
# `agent/workflow_menu.py`（20261017 批 2 刀 2e-2）：标记本体仍在 `agent/workflow_model.py`
# （两边都看得见），本模块顶部 import 回来（迁移期转发）。


# 口语模糊归一（`FUZZY_NAV_RULES` / `fuzzy_nav_hit`）已随导航映射表搬到
# `agent/workflow_defs/nav.py`（批 2 刀 2b）；本模块顶部 import 回来（迁移期转发）。


# `Workflow`（技能的数据模型 + 只读的 `contract` 取法）已下沉到
# `agent/workflow_model.py`（20261017 批 2 刀 2a）：它被注册表字面量、参数 schema、
# 展开层、实例化、菜单五族共用，谁也不该 import 谁 ⇒ 落在共同的下沉层，
# 本模块顶部 import 回来。

# 「先用 account_roster 核对账号名」那条契约的**共用尾巴**已下沉到
# `agent/workflow_defs/capability.py`（批 2 刀 2b）；本模块顶部 import 回来。


# ---------------------------------------------------------------------------
# 技能注册表本体（47 条 `Workflow(...)` 字面量）+ 由它派生的写名单
# ---------------------------------------------------------------------------
# 已搬到 `agent/workflow_defs/`（20261017 批 2 刀 2b）：字面量按域分住六个模块，
# `WORKFLOWS` 由 `__init__.py` **保序拼回**（顺序是契约——它驱动 `WORKFLOW_MAP`、
# planner 菜单排列与 `visible_workflows` 的产出顺序，改顺序就是改提示词字节）。
# `WORKFLOW_MAP` / `_WRITE_SCOPES` / `_derive_write_workflow_names` / `WRITE_WORKFLOW_NAMES`
# 都跟着走。本模块顶部 import 回来（迁移期转发）。


# ---------------------------------------------------------------------------
# 技能模板实例化：planner 选技能 + 参数 → plan 字段（契约的写端）
# ---------------------------------------------------------------------------

# 展开层本体（`_write_arg` / `_write_list_arg` / `_tag_names` / `_unfilled_placeholder` /
# `_notice_placeholder` / `_expand_change_set` / `_expand_write_workflow`）已搬到
# `agent/workflow_expand_write.py`（20261017 批 2 刀 2d-2）；
# `_expand_own_workflow` / `_expand_todo_*` / `_norm_id_*` / `_norm_true` 在
# `agent/workflow_expand.py`（批 2 刀 2d）。



# 计划状态闭集（`PLAN_STATUS_*`）已下沉到 `agent/plan_status.py`（20261017 批 2 刀 2c）：
# 消费方全在别处（`plan_codec` / `graph` / `decisions` / `eval`），本模块顶部 import 回来
# （迁移期转发，收尾刀整块消失）。


# 参数名别名层（`_PARAM_ALIASES` / `_param_alias_fix`）已下沉到
# `agent/workflow_params.py`（20261017 批 2 刀 2c）：它与参数 schema 同住一层（读
# `workflow_param_specs`），本模块顶部 import 回来（迁移期转发）。


# `_expand_article_status` 已随展开层搬到 `agent/workflow_expand.py`（批 2 刀 2d）。


# `instantiate_plan` / `_instantiate_plan` 已搬到 `agent/workflow_plan.py`（批 2 刀 2f）。
# 公开入口的薄壳与实例化本体都在那里，本模块顶部 import 回来（迁移期转发）。




# 剔空纠偏的"这条例目为什么不合法"后缀（`graph._drop_correction` 认它选话术）已下沉到
# `agent/workflow_model.py`（20261017 批 2 刀 2a）——实例化与展开两层都要拼它。


# `_workflow_no_calls_suffix` 已搬到 `agent/workflow_expand.py`（批 2 刀 2d）——实例化层
# 与展开层都要拼这句后缀。


# 技能参数 schema 一族（`arg_type_short` / `arg_enum` / `tool_arg_schemas` /
# `workflow_param_specs` 等）已下沉到 `agent/workflow_params.py`（20261017 批 2 刀 2c）：
# 菜单渲染与事前校验共用那一份规格。本模块顶部 import 回来（迁移期转发）。


# `render_workflow_params`（菜单里的参数行）已搬到 `agent/workflow_menu.py`（批 2 刀 2e-2）：
# 菜单渲染与事前校验共用那一份规格。本模块顶部 import 回来（迁移期转发）。


# 参数校验与归一（`_contains_ref` / `_coerce_value` / `_bad_choice` /
# `check_workflow_params` / `param_problem_note` / `_param_problem_plan`）已下沉到
# `agent/workflow_params.py`（20261017 批 2 刀 2c）。本模块顶部 import 回来（迁移期转发）。


# 一条 spec 的字面形状：`工具名({json 参数})`。写端在本模块（`tools.append`
# `f"{tool}({json.dumps(args, ensure_ascii=False)})"`，十余处），读端有三处
# （`graph._tool_args`、`tasks.bound_steps`、测试）——**拆分规则只能有一处**，
# 所以留在这里、贴着写端（同族教训见 agent/plan_codec.py `_esc_spec` 的注：
# "改一处要同步十三处"正是全仓审计点名的主导缺陷）。
_TOOL_SPEC_RE = re.compile(r"^(\w+)\((.*)\)$", re.DOTALL)


def split_tool_spec(spec: Any) -> tuple[str, str]:
    """`工具名({json 参数})` → `(工具名, 参数原文)`；**形状不匹配一律 `("", "")`**。

    只做拆分，**不解析参数**：参数原文怎么读由各消费方按自己的容错要求决定——
    `graph._tool_args` 要容模型的漂移（单引号/尾逗号/行注释，见 `_loads_tolerant`），
    而 `tasks.bound_steps` 读的是**本进程刚由 `json.dumps` 产出**的串（严格 JSON 就够）。
    把容错搬到这里会让前者变宽、后者变窄，两边都不对。
    """
    m = _TOOL_SPEC_RE.match(str(spec or "").strip())
    if not m:
        return "", ""
    return m.group(1), m.group(2)


# 实参绑定与模板展开（`args_are_bound` / `expand_template_args` / `check_call_args`）已下沉到
# `agent/workflow_params.py`（20261017 批 2 刀 2c）。本模块顶部 import 回来（迁移期转发）。


# ---------------------------------------------------------------------------
# prompt 注入块构建（planner 提示词注入用）——已搬到 `agent/workflow_menu.py`（批 2 刀 2e-2）
# ---------------------------------------------------------------------------
# `_nav_map_lines` / `_NAV_MAP_LINES` / `_workflow_plan_seq` / `build_planner_context` 全在那里。
# 那一族的出边全在已经搬出去的层上（模型/规格/工具清单/导航表/IoT 开关），一条边都不指向
# 本模块留下的实例化层——本模块顶部 import 回来（迁移期转发）。
