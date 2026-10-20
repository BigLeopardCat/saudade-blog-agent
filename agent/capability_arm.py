# -*- coding: utf-8 -*-
"""能力索引臂（`AGENT_ARM=capability`）：**planner 选能力，索引把工具面交给执行层**。

## 一条回合里的两态

```
第 0 轮   菜单 = 能力索引（49 条，每条带「由这些工具完成」与「完成判定」）
          模型点一条能力（带参数）           ⇒ 只**选定**，不执行
第 1..n 轮 菜单 = 那条能力的工具面（多数 1–3 件，经 authz 过滤）
          模型点真工具                      ⇒ 执行层逐条真调（同一张图的 execute 节点）
零调用轮   模型自己宣布收尾                  ⇒ 零工具 narrator 照旧写正文
```

两态的**判据是同一格**：`ctx["caps"]`（这一轮选了哪条能力）。它在 `plan_of` 里被写，
`menu_builder` / `prompt_builder` 读它。菜单变了就把 `ctx["menu"]` 清掉（见
`react_arm._bound_llm` 的三格注）——**作废由建菜单的那条臂自己负责**。

## 为什么是这个形状（POC 的三族红，逐族对上）

工具级菜单 POC（`docs/tool-menu-poc-20261020.md`，0.6725 vs 0.9825）的失败分五族，其中
三族**同一件东西**就能解释：

| 族 | 条数 | 失败形态 | 索引怎么治 |
|---|---|---|---|
| A 路由 | 13 | 近义工具里选错 | 菜单从 70 件收到**一条能力那一批** |
| B 定序 | 8 | `checker 验收回执缺少工具 X` | 索引项带「由这些工具完成」+「该调的都调上」 |
| C 检索 | 4 | 回复正文缺关键词 | `complete_when` / `reply_contract` 有了落点 |

## harness 一件不丢（这是本臂的验收口径）

**一个字节都不重写**——本臂只换 `react_arm.make_ctx` 的四个接缝，其余全部由那张真图承载：

  · **角色过滤（不扩权）**：索引态走 `build_tool_schema`（= `visible_workflows`），
    工具态走 `tool_menu.build_tool_menu`（= `authz.TOOL_SCOPE`）——两处都是生产同一张表；
  · **权限闸 / 同意闸 / `$tool[i].field` 引用解析 / 生产 checker 的 PASS/BLOCK / 回执**：
    全部在 `WorkflowExecutor.run_specs` 里（`ToolExecutor` 逐字复用），本模块一行不碰；
  · **无进展 / 预算 / 意图纠偏**：`react_line.convergence_verdict`（在 `planner_node` 里）；
  · **gate**：`graph.gate_node`（那一格没动）；**零工具 narrator**：`react_arm.model_node`。

## 射程与**如实记下的边界**

  · 走 ReAct 的是 `capability_index.react_scoped` 为真的那些（只读 + 导航 + 特效 + 夜间 +
    设备 + 检索）；**写族 30 条与 `chat` 继续走模板**——索引态点到它们时，`plan_of` 直接
    产出技能级 `calls`，执行层走 `WorkflowExecutor.run`（`instantiate_plan` 展开），
    与生产**逐字同一条路**。所以本臂不是"把生产换掉"，是"给只读族换一条执行方式"。
  · **模板那一半仍然承重**：写族的 9 个参数校正器、`_confirm_grant_plan`、变更集展开
    全部原样；本臂只让它们**晚一轮**（索引态先选、下一轮工具态才可能点到写工具）。
    ⚠️ 实际上写族**不会**走到工具态：`react_scoped(favorite_add) is False` ⇒ 索引态那一轮
    就直接执行完了，菜单不会切。这是有意的（写族不进射程）。
  · **`plan["workflow"]` 是模型自己选的能力名**（不是 `tools_arm` 那种从工具名倒推的粗标签）
    ⇒ gate 的能力否认族（`CAPABILITY_DENIAL_OBJECTS` 按技能名索引）在本臂上**照常开火**，
    而 `tools_arm` 那一臂上是个静默洞（见该模块头注最后一段）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from agent import react_arm
from agent.capability_index import Capability, capability_brief, capability_of, index_entry
from agent.frame_contract import spec_text
from agent.planner_llm import build_tool_schema
from agent.react_line import RunLedger, WorkflowExecutor
from agent.tool_menu import build_tool_menu, strip_slots
from agent.tool_prompt import PSEUDO_NAMES, build_tool_prompt
from agent.tools_arm import ToolExecutor
from agent.workflow_defs import WORKFLOW_MAP

# ⚠️ `react_arm` 的四件**按模块属性取**（`react_arm._system_prompt` 之流），不 `from …
# import _system_prompt`：离线套件靠替换模块属性造现场（`tests/test_arm_seams.py::_run`），
# 而 `from … import` 在导入期就把名字绑死了 ⇒ 替换不生效、判据读到的还是原来那一份
# ——症状是"判据看着在跑、其实量的是没被换过的那条路"（本仓点名的失效模式）。

ARM_NAME = "capability"

_EMPTY_SCHEMA: dict = {"type": "object", "properties": {}}


def _placeholder(name: str):
    """菜单项的 `func` 占位体：**故意抛异常**。

    真正被调的是 `CapabilityExecutor` → `run_specs` → 真工具。接线断了要**响亮**地失败，
    而不是"调了个什么都不做的桩"——后者会让整轮看起来跑通了、其实一个工具都没执行
    （同 `react_arm._workflow_menu` / `tools_arm.menu_of` 那两处的理由）。
    """
    return lambda _n=name, **_k: (_ for _ in ()).throw(
        RuntimeError(f"能力菜单占位体被直接调用（{_n}）——包装层没接上"))


def _wrap(items: list[dict], describe: Any) -> list[StructuredTool]:
    """OpenAI tools 数组（`build_tool_schema` / `build_tool_menu` 的产物）→ `StructuredTool`。

    `describe(fn) -> str` 给出这一项的说明文字——**索引态**换成 `index_entry`（多出工具面
    与完成判定），**工具态**原样用菜单自带的那句。两种形态共用这一段包装，是因为它们的
    `args_schema` 来源必须完全同源（差一格就是"模型看得到的参数"与"执行期校验的参数"
    不是同一份）。
    """
    out: list[StructuredTool] = []
    for item in items:
        fn = (item.get("function") or item) if isinstance(item, dict) else {}
        name = str(fn.get("name") or "")
        if not name:
            continue
        out.append(StructuredTool(
            name=name, description=describe(fn),
            args_schema=fn.get("parameters") or _EMPTY_SCHEMA,
            func=_placeholder(name)))
    return out


def _index_menu(role: str | None) -> list[StructuredTool]:
    """索引态菜单：**与生产 planner 同一份 schema**，只把 `description` 那一格换成索引项。

    函数名 / 参数 / `required` / `enum` 一个字符不动（`build_tool_schema` 出什么就是什么）
    ——ADR-0015 阶段 1 就是"路由收窄"这一件事，改 schema 结构会把这个自变量弄脏。
    伪函数（`task_hold` / `task_drop` / `task_intents`）不在索引里 ⇒ 说明原样保留。

    **`slots=False`（20261021）**：不挂 `intents` / `reply_kind` 那两个跨切面槽位。本臂
    折算计划时走 `tool_menu.strip_slots`——**填了就扔**；而它们单份 799 字符 × 49 条能力
    = 本菜单的 **59%**（67,441 → 27,892 字符）。`build_tool_schema` 的缺省仍是 `True`
    ⇒ 生产那条路逐字节不变。⚠️ 这一格**不是**"顺手瘦身"：槽位是"给模型一个地方填"的
    申报口，填了没人读等于**骗模型以为申报过了**；摘掉之后意图那条路只剩 `task_intents`
    一个真被读的出口（它自带 `intents`，不受本参数影响）。
    """
    def describe(fn: dict) -> str:
        cap = capability_of(str(fn.get("name") or ""), role)
        return index_entry(cap) if cap is not None else str(fn.get("description") or "")

    return _wrap(build_tool_schema(role, slots=False), describe)


def _tools_menu(role: str | None, caps: list[Capability]) -> list[StructuredTool]:
    """工具态菜单：**只留这几条能力那一批工具**（`tool_menu.build_tool_menu` 已按 authz
    过滤过一遍，这里再与能力面取交——两道都留着，因为它们的失效方向不同：authz 那道防
    "越权可见"，能力面这道防"菜单里出现这一轮不该有的工具"）。

    三个记账口（`task_hold` / `task_drop` / `task_intents`）**恒在**：它们是跨切面槽位，
    与这一轮做哪条能力无关（同 `build_tool_menu` 末尾那两行）。

    **`slots=False`**：同 `_index_menu`——`intents` / `reply_kind` 在本臂**填了就扔**，
    每一件工具白背 799 字符。
    """
    return _wrap([i for i in build_tool_menu(role, slots=False)
                  if _menu_name(i) in _face_names(role, caps)
                  or _menu_name(i) in PSEUDO_NAMES],
                 lambda fn: str(fn.get("description") or ""))


def _face_names(role: str | None, caps: list[Capability]) -> set[str]:
    """这几条能力那一批**真工具名**（不含三个记账口）。

    菜单与**提示词里那张清单**读的是同一格（`tool_menu_text(role, names=...)`）——
    两份清单各说各话正是 POC 里 A 族那 13 条的成因：提示词写着"可调用工具（唯一可选
    集合）"却列出全部 73 件，而绑给模型的只有 4 件。
    """
    return {t for cap in caps for t in cap.tools}


def _menu_name(item: dict) -> str:
    fn = (item.get("function") or item) if isinstance(item, dict) else {}
    return str(fn.get("name") or "")


def _caps_of(ctx: dict | None, role: str | None) -> list[Capability]:
    """`ctx["caps"]`（能力名）→ 索引项，**取不到的静默丢掉**（不猜）。

    取不到只有一种成因：这个名字不在这一轮的可见菜单里（`capability_of` 的唯一 `None`
    条件）——那是模型点了够不着的本领，不该因此把它那一批工具绑上（**fail-closed**）。
    """
    names = list((ctx or {}).get("caps") or [])
    return [c for c in (capability_of(n, role) for n in names) if c is not None]


def capability_menu(role: str | None, ctx: dict | None = None) -> list[StructuredTool]:
    """接缝①：**按轮变**的菜单。`ctx["caps"]` 空 ⇒ 索引态；非空 ⇒ 工具态。"""
    caps = _caps_of(ctx, role)
    return _tools_menu(role, caps) if caps else _index_menu(role)


def capability_prompt(state: dict, ctx: dict) -> str:
    """接缝②：与菜单**成对**换的提示词（只换菜单不换提示词 ⇒ 量的差是提示词不匹配）。

      · **索引态**：`react_arm._system_prompt` **逐字那一份**——它就是生产 planner 的
        提示词（技能表 + 规则 + 输出契约）。选能力这一步与生产是同一份资产，不另写一份；
      · **工具态**：`tool_prompt.build_tool_prompt` + 能力简报（`capability_brief`）。
        简报里那三格（工具面 / 完成判定 / 回复契约）正是 B、C 两族的修复点。
        **`tool_names` 必须传**：不传 ⇒ `{tool_menu}` 那一格印的是**全部 73 件**工具，
        而绑给模型的 schema 只有这一批（多数 1–3 件）——两份自相矛盾的清单，且抬头上
        写着"**唯一可选集合**"。POC 的 A 族（13 条路由红）红在"近义工具里选错"，
        这一格是把那件事从 schema 侧治好之后又在文本侧放回去。
    """
    caps = _caps_of(ctx, ctx.get("role"))
    messages = list(state.get("messages") or [])
    if not caps:
        return react_arm._system_prompt(
            messages, ctx.get("role"), ctx.get("principal"),
            ctx.get("config"), ctx.get("user_msg") or "",
            exec_rows=(state.get("ledger") or {}).get("exec_rows"))
    user_msg = str(ctx.get("user_msg") or "")
    return build_tool_prompt(
        messages=messages, role=ctx.get("role"), principal=ctx.get("principal"),
        config=ctx.get("config"), user_msg=user_msg,
        ledger_frame=react_arm.ledger_frame_for(
            messages, ctx.get("principal"), ctx.get("config"), user_msg),
        correction=str(ctx.get("correction") or ""),
        exec_rows=(state.get("ledger") or {}).get("exec_rows"),
        capability="\n\n".join(capability_brief(c) for c in caps),
        tool_names=_face_names(ctx.get("role"), caps))


def plan_of(tool_calls: list, role: str | None, ctx: dict | None = None) -> dict:
    """接缝③：模型的 `tool_calls` → `plan_obj`。**两态由"点的是能力还是工具"分**。

      · 点的是**射程内的能力** ⇒ 记进 `ctx["caps"]`、**不产出调用**。零调用是设计，
        不是空计划：下一轮菜单换成它那一批工具，由执行层做事。同一轮点了多条能力
        （多意图）⇒ 并起来，下一轮菜单是它们工具面的并集。
      · 其余（写族能力 / `chat` / 真工具名）⇒ 照常产出 `calls`，执行层按名字分派。
    """
    ctx = ctx if ctx is not None else {}
    before = list(ctx.get("caps") or [])
    caps = list(before)
    # 这一轮点到过的**全部**能力名（含写族与 `chat`）。它只用来定 `workflow` 那一格，
    # 不进 `ctx["caps"]`——写族与 `chat` 这一轮就执行完了，菜单**不该**切到它们的工具面
    # （切了下一轮模型就看不见能力表，多意图里剩下的那几件再也点不到）。
    named: list[str] = []
    tools: list[str] = []
    calls: list[dict] = []
    for c in tool_calls or []:
        name = str((c or {}).get("name") or "")
        if not name:
            continue
        args = strip_slots((c or {}).get("args") or {})
        cap = capability_of(name, role)
        if cap is not None:
            if name not in named:
                named.append(name)
            if cap.react:
                if name not in caps:
                    caps.append(name)
                continue
        tools.append(spec_text(name, args))
        calls.append({"workflow": name, "params": args})
    if not named and not calls:
        return {}
    ctx["caps"] = caps
    if caps != before:
        # 能力面变了 ⇒ **菜单作废**（下一轮 `_bound_llm` 重建）。提示词跟着走：
        # `_prompt_of` 的缓存键就是菜单身份。
        ctx["menu"] = None
    # `workflow` 取**第一条能力名**而不是 `|` 连起来：`plan_codec.normalize_plan` 会把
    # 不在 `WORKFLOW_MAP` 里的取值改成 `chat`（`a|b` 不在），而 gate 按它分族、
    # `CAPABILITY_DENIAL_OBJECTS` 按技能名索引——落成 `chat` 等于把那一族关掉。
    # 兜底顺序：这一轮选的 ReAct 能力 → 这一轮点到的任意能力（写族/`chat`）→ `chat`。
    label = (caps or named or [""])[0]
    return {"workflow": label or "chat", "tools": tools, "note": "",
            "reply": "", "status": "", "calls": calls}


class CapabilityExecutor(ToolExecutor):
    """接缝④：**按名字分派**——是能力名就走模板展开，是工具名就直接调。

    `WorkflowExecutor.run` 的第一件事是 `instantiate_plan`（技能 → 固定工具序列）。
    写族与 `chat` 要的**正是**那一步（9 个参数校正器、变更集展开、纯应答留痕全在它下游）；
    工具态点到的真工具要的**正是**它的反面。所以这里不是"换一个执行器"，是"按名字选路"，
    两条路的下游（同意闸 → 权限闸 → 引用解析 → 真调用 → checker → 回执）**同一个**
    `run_specs`。
    """

    def run(self, name: str, params: dict) -> tuple[str, int]:
        key = str(name)
        if key in WORKFLOW_MAP:
            return WorkflowExecutor.run(self, key, dict(params or {}))
        return super().run(key, params)


def _executor_factory(ledger: RunLedger, *, role: str | None, principal: Any,
                      user_msg: str, state: dict, real_tools: dict | None) -> CapabilityExecutor:
    """接缝④的工厂。除执行器换成 `CapabilityExecutor` 外，参数**逐字同 `make_ctx` 里那一行**
    ——两臂的执行器在"谁挡权限、谁判同意、谁记台账"上必须完全同源。"""
    return CapabilityExecutor(real_tools if real_tools is not None else {}, ledger,
                              role=role, principal=principal, user_msg=user_msg,
                              grant=state.get("confirm_grant"), state=state)


class CapabilityArm(react_arm.ReactGoldenArm):
    """本臂的臂对象：**只换 `ctx_hooks` 那四格**，`stream` 一个字不覆写。"""

    name = ARM_NAME
    ctx_hooks = {"menu_builder": capability_menu, "plan_builder": plan_of,
                 "prompt_builder": capability_prompt, "executor_factory": _executor_factory}


def build() -> CapabilityArm:
    """`server._build_arm` / `golden_arm.build_agent` 的入口（约定名，改名要连着改那边）。"""
    return CapabilityArm(react_arm._compiled())
