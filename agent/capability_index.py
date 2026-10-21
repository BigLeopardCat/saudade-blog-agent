# -*- coding: utf-8 -*-
"""能力索引：planner 选**能力**，索引把该能力的**工具面**交给执行层（ADR-0015 阶段 2）。

## 它要解决的问题（POC 的五族红，逐族对上）

工具级菜单 POC（`docs/tool-menu-poc-20261020.md`）把菜单从 50 条技能模板换成 70 件裸工具，
读数 0.6725 vs 0.9825。三族的成因各不相同，而**同一件东西**能同时解释它们：

  · **A 族（13 条，路由）**：菜单是**全部**工具 ⇒ 近义工具里选错。索引把这一轮的工具面
    收到**一条能力那一批**（多数 1–3 件），选项少了，选错的面就小了。
  · **B 族（8 条，定序）**：失败形态是 `checker 验收回执缺少工具 X`。裸工具表里
    **没有"这条能力该调哪些工具"这个要求**——`Workflow.plan` 里写着，换菜单时丢了。
    索引项**带着它**：执行层因此知道"这批里缺了哪一件"。
  · **C 族（4 条，检索）**：失败形态是回复正文缺关键词。`complete_when` / `reply_contract`
    是**每条能力各自写的散文**，工具没有"多步任务怎么算完成"这个概念 ⇒ 它们在工具臂上
    **没有家**。索引项把它们带上，执行层的提示词就有地方挂。

## 单一来源：索引项**全部**从 `workflow_defs` 派生，零新增手写

"这条能力涉及哪些工具" = `[n for n, _ in wf.plan] + list(wf.extra_plan_tools)`——与
`workflow_defs._derive_write_workflow_names` **逐字同一条算法**（那边用它判"是不是写技能"）。
索引不另立一张分组表：**重新分组本身就是另一种手写分组**，量它等于量"手写分组 v2 vs
手写模板 v1"（ADR-0015 取舍 #4）。索引行同理，走 `tool_index.index_line`（机械切、不改写）。

## 与 `tool_index` 的分工（别把两个模块读成一个）

  · `tool_index`（ADR-0015 阶段 0）管的是**同一份菜单怎么变短**：把每个技能的描述散文压成
    一行，配一个 `describe_skill` 按需取回。它**不改变动作空间**。
  · 本模块管的是**动作空间本身**：把"能力"这个单位连同它的工具面与验收契约交到执行层，
    让执行层用**这一批工具**跑 ReAct。它**不压缩描述**。

两者叠加使用，互不依赖：`capability_index` 不知道 `describe_skill` 存在。

## 射程（ADR-0015 阶段 2，如实记边界）

`react_scoped` 为真的能力走"执行层 ReAct"；为假的（写族 30 条 + `chat`）**继续走模板**
（`instantiate_plan` 展开成固定序列）——写族有 9 个参数校正器、28 处读技能级
`plan_obj["params"]` 的落点，硬并进来是另一个量级的风险，且与本模块无关。
`content_query`（`dynamic`）**在射程内**：它的工具面本来就是一个按角色给的只读白名单
（`workflow_tools`），检索族正是最该做 ReAct 的那一族。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.tool_index import index_line, skill_description
from agent.workflow_defs import WORKFLOW_MAP, WRITE_WORKFLOW_NAMES, visible_workflows
from agent.workflow_model import Workflow
from agent.workflow_tools import callable_query_tools, explicit_tools

__all__ = [
    "Capability",
    "capabilities_for",
    "capability_brief",
    "capability_kind",
    "capability_of",
    "index_entry",
    "react_scoped",
    "tools_of",
]


@dataclass(frozen=True)
class Capability:
    """索引里的一项 = 一条技能 + 它的工具面 + 它的验收契约。

    **为什么不复用 `Workflow` 直接当索引项**：`Workflow` 是**注册表**的形状（`inputs` /
    `plan` 的固定序列 / `optional_params`），而索引项要的是**决策侧**的形状（一行索引、
    工具名集合、能不能做 ReAct）。两者字段不同、消费方不同；让 `Workflow` 长出决策字段
    会把"注册表"和"菜单"两件事焊死在一个类型上（加菜单格就要动注册表契约）。
    """

    name: str
    #: 一行索引（`tool_index.index_line` 机械切出来的**原文前缀**，绝不改写）
    line: str
    #: 这条能力涉及的工具（`plan` ∪ `extra_plan_tools`；`dynamic` 的按角色现算）
    tools: tuple[str, ...]
    #: 工具面**由参数指定**而不是写在 `plan` 里（今天只有 `content_query` 一条）
    dynamic: bool
    #: 本阶段走执行层 ReAct（见模块头注《射程》）
    react: bool
    #: **执行方式**（见 `capability_kind`）：`workflow` = 确定性快道；`skill` = ReAct；
    #: `tool` = 裸工具（今天没有这种条目，见 `capability_kind` 的注）
    kind: str
    complete_when: str
    reply_contract: str
    capability: str
    chat: bool


def _plan_tools(workflow: Workflow) -> tuple[str, ...]:
    """`plan` 的固定序列 ∪ `extra_plan_tools`（条件工具），**保序去重**。

    与 `workflow_defs._derive_write_workflow_names` 取的是同一对来源——那边拿它判
    "有没有写工具"，这边拿它当工具面。写成一处而不是两处：两处一旦分叉，
    "索引说这批工具"与"展开器真派这批工具"会悄悄不一致，而分叉没有判据看得见。
    `extra_plan_tools` 必须算进来——`article_tags` 的 `create_tag` 就在那一格，
    只看 `plan` 会漏掉它（同 `Workflow.extra_plan_tools` 的注）。
    """
    return tuple(dict.fromkeys([n for n, _ in workflow.plan] + list(workflow.extra_plan_tools)))


def tools_of(name: str, role: str | None) -> tuple[str, ...]:
    """一条能力的工具面。**`dynamic` 的那条按角色现算**（白名单在 `workflow_tools`）。

    `role=None`（身份不明/单测）⇒ 白名单只剩公开那半，失败取向往保守一侧倒
    （与 `visible_workflows`、authz 同向）。
    """
    workflow = WORKFLOW_MAP.get(str(name or ""))
    if workflow is None:
        return ()
    if not workflow.plan and not workflow.chat:
        return tuple(dict.fromkeys([*explicit_tools(role), *callable_query_tools(role)]))
    return _plan_tools(workflow)


def react_scoped(name: str) -> bool:
    """这条能力本阶段走"执行层 ReAct"还是"模板展开"。

    判据 = **不在写名单里 ∧ 不是闲聊**。写名单用 `WRITE_WORKFLOW_NAMES`（派生值，
    "计划里有没有写作用域的工具"）而不是手抄一份：它就是写族的边界，索引再抄一份
    必然漂。导航 / 特效 / 夜间 / 设备四件**在射程内**——它们的作用域是 `write.page` /
    `write.device`（动的是主人的浏览器、不是站内数据），不写名单、也不弹卡，
    与"只读 + 导航 + 检索"那句射程一致。

    `chat` 走模板：它零工具，`WorkflowExecutor.run` 那条"纯应答技能"分支要把
    `reply_contract` 记进 `ledger.replies`，ReAct 那条路上没有这个落点。
    """
    workflow = WORKFLOW_MAP.get(str(name or ""))
    if workflow is None or workflow.chat:
        return False
    return workflow.name not in WRITE_WORKFLOW_NAMES


def capability_kind(name: str) -> str:
    """一条能力的**执行方式**——planner 路由到它之后，执行层该怎么跑它。

    三个取值，只有前两个今天有实例：

      · `workflow`：**确定性快道**。这条能力的 `plan` 是固定工具序列、参数由 planner
        填完就能展开（写族 + `chat`）⇒ 路由到它之后**不跑 ReAct**，直接
        `instantiate_plan` → `execute`。判据就是 `react_scoped` 的否命题，**不另立一张
        写名单**（`WRITE_WORKFLOW_NAMES` 是派生值，抄第二份必然漂）。
      · `skill`：**ReAct**。这条能力交给执行层一批工具，由它自己选（只读 / 导航 /
        检索族，见模块头注《射程》）。
      · `tool`：**裸工具**。没有技能包裹、planner 直接点它。⚠️ **今天一条都没有**，
        而且是**事实如此、不是没实现**：注册表里 70 件工具、49 件被技能的 `plan` 覆盖，
        剩下 21 件（`list_notes` / `rag_search` / `get_weather` …）全是
        `content_query` 那条 `dynamic` 能力的工具面——它们**已经有家**，就是
        `content_query`。把它们提成独立条目 = 换掉 `content_query` 的路由面，而工具级
        菜单 POC 量到的正是那一族红（A 族 13 条，`docs/tool-menu-poc-20261020.md`）。
        所以这一格**留着不用**：等真有"没有技能包裹的裸工具"时它自动有实例，
        判据（`tests/test_capability_index.py`）锁的是**派生关系**，不是"今天有几条"。

    ⚠️ **名字不在注册表里 ⇒ 响亮抛，不回落成 `tool`**：回落会把"技能名打错一个字"
    静默变成"这是一件裸工具"，而那种漂移没有判据看得见（本仓点名的失效模式）。
    裸工具那一条路**不走这个函数**——它的 `kind` 由裸工具索引自己给。
    """
    key = str(name or "").strip()
    workflow = WORKFLOW_MAP.get(key)
    if workflow is None:
        raise KeyError(f"{key!r} 不在技能注册表里——裸工具不走 capability_kind")
    return "skill" if react_scoped(workflow.name) else "workflow"


def capability_of(name: str, role: str | None) -> Capability | None:
    """一条技能名 → 索引项；**不在可见菜单里的一律 None**（不猜、不兜）。

    可见性判据用 `visible_workflows(role)`——与 `build_tool_schema`、planner 菜单、
    `task_intents` 归一化、`describe_skill_text` 是**同一张表**。所以"索引里有什么"
    恒等于"这一轮本来就能选什么"，**不扩权**。
    """
    key = str(name or "").strip()
    if not key or key not in {w.name for w in visible_workflows(role)}:
        return None
    return _of(WORKFLOW_MAP[key], role)


def capabilities_for(role: str | None) -> list[Capability]:
    """这一轮这个身份的能力索引（**顺序 = 注册表顺序**，即菜单顺序）。

    顺序是一份契约（`workflow_defs` 头注）：菜单排列变了就是提示词字节变了。
    """
    return [_of(w, role) for w in visible_workflows(role)]


def _of(workflow: Workflow, role: str | None) -> Capability:
    return Capability(
        name=workflow.name,
        line=index_line(skill_description(workflow, role)),
        tools=tools_of(workflow.name, role),
        dynamic=not workflow.plan and not workflow.chat,
        react=react_scoped(workflow.name),
        kind=capability_kind(workflow.name),
        complete_when=workflow.complete_when,
        reply_contract=workflow.reply_contract,
        capability=workflow.capability,
        chat=workflow.chat,
    )


def _tools_clause(cap: Capability) -> str:
    """工具面那一句。**`dynamic` 的那条不能列名字**：它的白名单按角色算，
    列出来就是一份"这个身份能用哪些检索工具"的清单，而那句话在 `content_query` 的
    参数说明里已经有一份（`workflow_tools` 是唯一来源）——写第二份必然漂。"""
    if cap.dynamic:
        return "工具由参数指定（见工具菜单）。"
    if not cap.tools:
        return "本能力不调用任何工具。"
    return "本能力由这些工具完成：" + "、".join(cap.tools) + "。"


def index_entry(cap: Capability) -> str:
    """索引项进**菜单**的那段描述（`description` 那一格的全部内容）。

    三段：一行索引（选它干什么）+ 工具面（它由谁完成）+ 完成判定（怎么算做完）。
    第二段是本模块存在的理由——**裸工具表里没有"这条能力该调哪些工具"这个要求**，
    B 族的 `checker 验收回执缺少工具 X` 就是从那儿来的。
    """
    rows = [cap.line, _tools_clause(cap)]
    if cap.complete_when:
        rows.append(f"完成判定：{cap.complete_when}")
    return "\n".join(rows)


def capability_brief(cap: Capability) -> str:
    """索引项进**执行层提示词**的那段（ReAct 那一轮告诉模型"你现在在完成哪件事"）。

    比 `index_entry` 多两格：`capability`（这一族是干什么的，注册表原文）与
    `reply_contract`（回复该怎么组织）。C 族的失败形态正是"回复正文缺关键词"——
    这两格在工具臂上没有家，在这里有。
    """
    rows = [f"这一轮要完成的能力：{cap.name}"]
    if cap.capability:
        rows.append(f"它管的是：{cap.capability}")
    rows.append(cap.line)
    rows.append(_tools_clause(cap))
    if cap.complete_when:
        rows.append(f"完成判定：{cap.complete_when}")
    if cap.reply_contract:
        rows.append(f"回复契约：{cap.reply_contract}")
    return "\n".join(rows)
