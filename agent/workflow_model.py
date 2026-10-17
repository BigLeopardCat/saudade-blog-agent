# -*- coding: utf-8 -*-
"""技能注册表的**数据模型层**：`Workflow` / `ParamSpec` 两个 dataclass 与它们共用的原语。

这是 `agent/workflows.py` 拆分的**下沉层**（20261017 批 2 刀 2a）：注册表字面量
（`agent/workflow_defs/`）、参数 schema（`agent/workflow_params.py`）、展开层
（`agent/workflow_expand.py`）、实例化（`agent/workflow_plan.py`）与菜单
（`agent/workflow_menu.py`）**都**要 import 这里的东西，而这里**谁也不 import**
（本模块对仓内其它模块零依赖，只依赖标准库）。

**分层纪律**：本模块只放"被两个以上上层模块共用、且自身出边为零"的名字。任何
只被一个上层模块用到的常量/函数，跟着那个模块走——别往这里囤。加东西之前先数
消费方：一个消费方 ⇒ 它住错了层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DROP_SUFFIX_BAD_ARGS",
    "DROP_SUFFIX_NOT_OBJECT",
    "DROP_SUFFIX_WORKFLOW_NO_CALLS",
    "ParamSpec",
    "Workflow",
]


@dataclass
class Workflow:
    name: str                          # 技能名（plan 字段的 WORKFLOW= 值）
    description: str                   # 触发条件（planner 选技能用）
    inputs: dict[str, str]             # 参数名 → 提取要求（planner 填 PARAMS 用）
    plan: list[tuple[str, dict]] = field(default_factory=list)  # 固定工具序列：(工具名, 参数模板)
    # 条件工具（20261009）：本技能的展开函数**可能**多派出去的工具名（不在 `plan` 模板
    # 里）。第一个用它的地方是 `article_tags`——要加的标签站内还没有时，同一张卡里先
    # `create_tag` 再 `set_article_tags`（见 `instantiate_plan` 那一段）。
    #
    # **为什么不是直接写进 `plan`**：`plan` 还有第二个读者——`tasks.intents_to_declarations`
    # 从它推"这件事要做哪几步"，写进去就等于宣布每个 article_tags 任务都必然包含一次
    # `create_tag`；而实际执行时标签已存在的那一条会被 `reached_specs` 摘掉、**永远不发
    # 那一步的回执**，于是那一行任务在台账里推不动也结不掉（`advance_by_receipts` 必须
    # 连续推进）——正是它自己注里写的"比不登记更坏"的僵尸行。
    # `plan` 与 `extra_plan_tools` 因此是两件事：前者="模板一定产出哪几件"，后者="令牌
    # 里还容得下哪几件"。`_confirm_grant_plan` 的形状检查读**两者之和**。
    extra_plan_tools: tuple[str, ...] = ()
    # 参数必填性（20260925，见"技能参数 schema"节）。**默认从工具 args_schema 派生**，
    # 这两个元组只用来盖掉派生结果——技能参数与工具参数不是同一层，形状必填 ≠ 策略必填。
    # 例：`device_display.text` 在 pydantic 里必填，但技能在代码侧被空参调用（`decisions.py`
    # 的 `instantiate_plan("device_display", {})`，文案由执行层创作）⇒ optional_params 里点名。
    # **只写有实证的例外，不写"我觉得该必填"的名字**——多一条就多一个 planner 被白白拦住的路。
    required_params: tuple[str, ...] = ()
    optional_params: tuple[str, ...] = ()
    complete_when: str = ""            # 完成判定（注入 planner 提示词，辅助收尾决策）
    reply_contract: str = ""           # 回复契约（model 遵守）
    # ⚠️ 这里**曾经**有一个 `planner_contract` 字段（20260927–20261017）：把"必须怎么做"
    # 那一句从 `description` 里拆出来、只渲染进提示词正文。**20261017 已并回
    # `description`**——拆它的依据被实测推翻：那次 `rag_talk_rag` 完整菜单 5/5 绿 → slim
    # 0/5 红**是信息丢失不是位置**（slim 臂 schema 里原样漏着未展开的
    # `__无参只读工具清单__`，而 `tools` 参数描述又被 `_SCHEMA_OVERRIDES` 盖成一句没有
    # 工具名的话 ⇒ 两个工具名在 schema 里两条路都没出现）。四臂重验
    # （`eval/contract_position_ab.py`，7 用例 × 12 遍）**逐档相同**：只住正文 84/84、
    # 只住 schema 84/84、并进 description 84/84、**整句删掉 73/84** ⇒ **位置无关，那句话
    # 本身承重**。`tools[]` 的形状因此回到行业惯例：每个函数一个 `description`，
    # 没有第二个字段。**别再拆出来**——拆一次就多守一条"两份会漂"的纪律。
    chat: bool = False                 # 闲聊（chat 轮零工具叙述，gate 声称检查按窄作用域）
    # 可见角色（20260921 管理助手）：**空集 = 所有角色可见**（含身份不明）。
    # 非空 ⇒ 只有列出的角色能在 planner 上下文里看到它。这是"身份"那道闸，
    # 比 execute 里的 authz.check 更早——看不到的技能 planner 选不出来，
    # 于是连"被拒一次"都不会发生。两层互不依赖：即便这里漏了，authz 仍然拦。
    roles: frozenset[str] = frozenset()
    # 能力清单里的一句话（20260921）：**给访客看的那份"你能做什么"由注册表渲染**，
    # 不再手写在 agent/context.py 的 SITE_GUIDE 里——手写清单与注册表必然漂移
    # （165525/165544 答"我不能新建标签"、165645 又真建了、165937 答"我可以"：
    # 同一能力三轮两种答案，因为清单是静态文本、注册表是活的）。
    # 空串 = 不列（read_article 这类系统快道专用技能不对外说）。
    capability: str = ""

    @property
    def contract(self) -> str:
        """描述**末段**那一句"必须怎么做"（没有则空串）。

        20261017 起契约与描述同住 `description`，接在末尾、以 `\\n` 起头（并进去时就是
        这么接的）。切法只在"每个技能最多一段契约、且描述本体里没有换行"时成立——
        `eval/contract_position_ab.py --dry-run` 与 `tests/test_slim_workflows.py` 各有
        一条判据盯着它（含换行的技能必须**恰好**是那 12 个带契约的），坏了会响、
        不会静默切错。

        ⚠️ 它是**只读的取法**、不是第二个字段：没有任何地方从它写回去，`description`
        仍是唯一存储（所以它不违反上面"别再拆出来"那条）。加它只是为了让判据侧
        （四臂探针 + 三个套件）有一个**共同的**切法，而不是各写一遍 `find("\\n")`。
        """
        i = self.description.find("\n")
        return "" if i < 0 else self.description[i + 1:]


# ---------------------------------------------------------------------------
# 技能参数 schema 的两件共用原语（20260925）
# ---------------------------------------------------------------------------
# 详见 `agent/workflow_params.py` 的"技能参数 schema"那一节（生成侧/校验侧同源那三条
# 约定）。这里只放**跨模块共用**的那两件：哨兵与规格 dataclass。
_NO_DEFAULT = object()   # 哨兵：`default=None` 在 schema 里是有意义的（可空参数），不能拿 None 当"没默认值"


@dataclass(frozen=True)
class ParamSpec:
    """一个技能参数的规格（**菜单渲染与事前校验共用这一份**）。"""
    type: str = "any"          # str/int/bool/list/dict/any（any = 推不出映射，不校验类型）
    required: bool = False
    default: Any = _NO_DEFAULT
    desc: str = ""             # 中文说明（就是 `inputs` 里的那句散文）
    from_tool: str = ""        # 派生自哪个工具的哪个参数（空 = 推不出映射）
    # 闭集取值（来自工具 schema 的 `enum`；空 = 这个参数没有闭集，**不是**"闭集为空"）。
    # 只用于**校验**，不进 `render_workflow_params`（菜单逐字节不动——`Literal` 不改变
    # `arg_type_short` 的结果，所以菜单本来也不会变，但这条要说清楚：想印进菜单是
    # 另一件事，会改 planner 提示词，得单独拍板）。
    choices: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 剔空纠偏的"这条例目为什么不合法"后缀
# ---------------------------------------------------------------------------
# `graph._drop_correction` 认它选话术。带后缀 = 工具本身可达、是**这条**不可用——不能
# 笼统说成"你够不到这个工具"（那是假的，会把 planner 往错方向推）。两个后缀各自对应
# 一种改法。
DROP_SUFFIX_NOT_OBJECT = "（args 非对象）"
DROP_SUFFIX_BAD_ARGS = "（参数不合格："
# 第三种后缀（20260925 批 C）：**工具够得着，但这条点名写错了技能**。真相与上两种
# 又不同——改法是"把 WORKFLOW 换成 content_query、清单原样搬过去"，所以话术必须再分一支
# （笼统讲成"你够不到这个工具"是假话，会把 planner 推去换工具而不是换技能）。
# 前缀单独成常量：`graph._drop_correction` 按前缀选话术，片段拼在后面。
DROP_SUFFIX_WORKFLOW_NO_CALLS = "（WORKFLOW 是 "


# ---------------------------------------------------------------------------
# 技能描述里的工具枚举标记（20260924）
# ---------------------------------------------------------------------------
# 枚举**不能是模块常量**：它是角色相关的，得在 `build_planner_context` 里按 role 展开
# （`render_tool_marks`）。占位标记刻意**不含花括号**——整份 planner 提示词最后要过
# `_PLANNER_PROMPT.format(...)`，带 `{}` 的标记会被当成占位符炸掉。
#
# 它们住在**下沉层**（而不是菜单模块）的原因：注册表字面量
# （`agent/workflow_defs/` 里那 47 条 `description`）要写这两个标记，菜单渲染器
# （`agent/workflow_menu.py`）要读它们——两边都得看得见，而两边谁也不该 import 谁。
_EXPLICIT_TOOLS_MARK = "__无参只读工具清单__"
_PARAM_TOOLS_MARK = "__带参工具清单__"
