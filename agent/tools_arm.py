# -*- coding: utf-8 -*-
"""工具臂（`GOLDEN_ARM=tools`）：**菜单是真工具，不是技能模板**。

## 它要答的问题

生产（`graph` 臂）与第二条臂（`react` 臂）的动作空间都是 **50 条手写技能模板**
（`agent/workflow_defs/`，含展开层 ~5.5k 行）：模型点的是 `navigate` 这种**技能名**，
系统拿 `instantiate_plan` 把它展开成固定的工具序列。代价有两条——① 维护面（`graph.py`
11k 行 + 模板族）；② **每加一个能力就要写一条模板**，而这份成本**不随模型变强而下降**，
与"工具要少而可组合"的主流方向相反。

这一臂把菜单换成 `tools/base.py` 的 70 个工具按 `authz` 角色过滤后的那张表，模型**直接
点工具**，没有中间那一层。要量的是：**harness 撑不撑得住**。

## 撑不撑得住 = 这些件还在不在（本臂一件都不省）

  · **角色过滤（不扩权）**：菜单与执行期用 `authz.TOOL_SCOPE` **同一张表**
    （`tool_menu.visible_tools`）⇒ 菜单里能选的恒 ⊆ 执行期放行的；
  · **权限闸**：`run_specs` 里的 `authz.check` + `must_enforce` → `denial_frame`；
  · **同意闸（写操作弹卡）**：同一处的 `_consent_missing` → `ledger.pending`；
  · **参数引用 `$tool[i].field`**：`arg_refs.resolve_args`（在 `run_specs` 里）；
  · **生产 checker 的 PASS/BLOCK + 回执行**：`graph._check_spec`（经 `_verdict_of`）；
  · **无进展 / 预算 / 意图纠偏**：`react_line.convergence_verdict`（在 `planner_node` 里）；
  · **gate**：`graph.gate_node`（那一格没动）；
  · **零工具 narrator**：`react_arm.model_node`。

**一件都不重写**——本模块只做四件事（下面"四个接缝"），其余全部由 `react_arm` 那张真图
承载。这不是省事，是判据纪律：**复制一份循环就是复制一份判据**。

## 四个接缝（`react_arm.make_ctx` 的四个可选参数）

1. `menu_builder`：`menu_of(role)` —— `tool_menu.build_tool_menu` 那 70 个工具按角色过滤后
   绑给模型；
2. `prompt_builder`：`prompt_of(state, ctx)` —— 系统提示。**必须与 1 成对换**：只换菜单
   不换提示词，量出来的差是"提示词不匹配"而不是架构差（`eval/experiments/react_line_ab.py`
   20261011 那条教训）；
3. `plan_builder`：`plan_of(tool_calls, role)` —— 模型的调用 → `plan_obj`。`workflow` 那一格
   是**派生**的（见 `_label_of` 的注）；
4. `executor_factory`：`ToolExecutor` —— 把 `WorkflowExecutor.run` 里"技能 → 工具序列"那一步
   换掉，**其余（同意闸 → 权限闸 → 引用解析 → 真调用 → checker）走同一个 `run_specs`**。

## 射程与**如实记下的边界**（别把本臂的读数读大）

  · 第一轮只打算量**只读 + 导航 + 检索**族；写族（30 条技能、7 个写参数校正器）不在这一轮。
    但**菜单本身没有按射程收窄**：按角色过滤的整张表就是菜单。把 70 个工具并成"几个可
    组合的"**本身就是另一种手写分组**，量它等于量"手写分组 v2 vs 手写模板 v1"。
  · **搬不动的资产**：`complete_when` / `reply_contract` / `capability` 是**每条技能各自写的
    散文**，工具没有"多步任务怎么算完成"这个概念 ⇒ 它们没有对应的家，只能变成提示词里的
    全局规则。若本臂在只读族上退化，这是第一嫌疑，而且**分不清**是"没有模板"还是"丢了
    那几段散文"——这条边界写在这里，免得结论被读大。
  · `CAPABILITY_DENIAL_OBJECTS`（`workflow_defs/capability.py`）按**技能名**索引，gate 的
    能力否认那一族读的就是它；本臂的 `workflow` 标签是派生的 ⇒ 那一族在本臂上**可能不
    开火**，而 `arm_capability` **不追踪它**（它是要求型族，不是 `forbid_*`）⇒ 是个静默洞，
    只能靠人读报告发现。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from agent.frame_contract import spec_text
from agent.react_arm import ReactGoldenArm, _compiled, ledger_frame_for
from agent.react_line import RunLedger, WorkflowExecutor, spec_signature
from agent.tool_menu import build_tool_menu
from agent.tool_prompt import build_tool_prompt
from agent.workflow_params import check_call_args
from agent.workflow_plan import instantiate_plan

ARM_NAME = "tools"

# `plan_obj["workflow"]` 由**选了哪些工具**派生（见 `_label_of`）。这张表是"工具 → 技能
# 标签"的**唯一**映射处：加一个动作工具而忘了登记，症状是它被归进 `content_query`
# （`_label_of` 的兜底），gate 按族分派时读到的就是错的标签。
_LABEL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("navigate", ("navigate_to",)),
    ("effect", ("toggle_effect",)),
    ("darkmode", ("toggle_dark_mode",)),
    ("device_display", ("device_oled_display",)),
    ("device_query", ("list_devices",)),
)


def _label_of(tool_names: list[str]) -> str:
    """选了哪些工具 → `workflow` 标签。

    **为什么非要这一格**：`plan_codec.normalize_plan` 会把不在 `WORKFLOW_MAP` 里的取值
    改成 `chat`，而 gate 按 `plan["workflow"]` 分族（导航完成声明、`content_query` 的零帧
    分支、家族收窄）。所以它必须是 `WORKFLOW_MAP` 里的名字——哪怕只是个粗标签。

    **这是一个真实的口径差，不许在报告里抹掉**：生产那一格是**模型自己选的技能名**，本臂
    是系统从工具名倒推的粗标签。两臂的 gate 结果若有差异，可能来自这一格而不是行为。
    """
    for label, tools in _LABEL_RULES:
        if any(t in tools for t in tool_names):
            return label
    return "content_query" if tool_names else "chat"


def menu_of(role: str | None) -> list[StructuredTool]:
    """工具级菜单：与 `tool_prompt.tool_menu_text` 同一份数据（`tool_menu.build_tool_menu`）。

    `func` 只是占位——真正被调的是 `ToolExecutor` → `WorkflowExecutor.run_specs` → 真工具。
    占位体故意抛异常：接线断了要**响亮**地失败，而不是"调了个什么都不做的桩"（后者会让整轮
    看起来跑通了、其实一个工具都没执行）。与 `react_arm._workflow_menu` 逐字同源。
    """
    out: list[StructuredTool] = []
    for item in build_tool_menu(role):
        fn = (item.get("function") or item) if isinstance(item, dict) else {}
        name = str(fn.get("name") or "")
        if not name:
            continue
        out.append(StructuredTool(
            name=name, description=fn.get("description") or "",
            args_schema=fn.get("parameters") or {"type": "object", "properties": {}},
            func=(lambda _n=name, **_k: (_ for _ in ()).throw(
                RuntimeError(f"工具菜单占位体被直接调用（{_n}）——包装层没接上")))))
    return out


def _strip_slots(args: dict) -> dict:
    """摘掉两个**跨切面槽位**：`intents`（意图清单）与 `reply_kind`（短应答自我申报）。

    它们不是工具参数，是 harness 的申报口（消费方分别是 `tasks.normalize_intents` 与
    `prompt_context.parse_reply_kind`），绑在每个函数上只为让模型有地方填。混进 spec 会被
    checker 当成"多写的参数"。摘法与 `planner_llm.tool_calls_to_plan` 同一条口径
    （那边也是摘走、不进 `params`）——**不是**"本臂不支持这两个通道"。
    """
    out = dict(args or {})
    out.pop("intents", None)
    out.pop("reply_kind", None)
    return out


def plan_of(tool_calls: list, role: str | None) -> dict:
    """模型的 `tool_calls` → `plan_obj`。与 `react_arm._plan_of` 同形，只差**不实例化技能**。

    `tools` 那一格是 spec 串（`frame_contract.spec_text` 是 spec 串形态的唯一事实源）；
    `calls` 是本图自己的键（`execute_node` 的清单来源）——两处都必须有，理由见
    `react_arm._plan_of` 的 docstring。
    """
    names: list[str] = []
    tools: list[str] = []
    calls: list[dict] = []
    for c in tool_calls or []:
        name = str((c or {}).get("name") or "")
        if not name:
            continue
        args = _strip_slots((c or {}).get("args") or {})
        names.append(name)
        tools.append(spec_text(name, args))
        calls.append({"workflow": name, "params": args})
    if not calls:
        return {}
    return {"workflow": _label_of(names), "tools": tools, "note": "", "reply": "",
            "status": "", "calls": calls}


def prompt_of(state: dict, ctx: dict) -> str:
    """系统提示（`react_arm._prompt_of` 的接缝）。背景信息与技能臂**逐格同源**。

    待办台账那一帧走 `react_arm.ledger_frame_for`——**不是**在这儿重写一遍：那一条同时
    负责"进了提示词就必须留痕"（`require_ledger_frame` 三条判据读的唯一证据），两处
    抄一份就等于让其中一臂的证据通道可以静默失效。
    """
    messages = list(state.get("messages") or [])
    user_msg = str(ctx.get("user_msg") or "")
    return build_tool_prompt(
        messages=messages, role=ctx.get("role"), principal=ctx.get("principal"),
        config=ctx.get("config"), user_msg=user_msg,
        ledger_frame=ledger_frame_for(messages, ctx.get("principal"), ctx.get("config"),
                                      user_msg),
        correction=str(ctx.get("correction") or ""),
        exec_rows=(state.get("ledger") or {}).get("exec_rows"))


class ToolExecutor(WorkflowExecutor):
    """执行器：`run(工具名, 参数)`——**没有技能展开那一步**。

    `WorkflowExecutor.run` 的第一件事是 `instantiate_plan`（技能 → 工具序列）；本臂的调用
    **本身就是**工具序列，所以这里只做两件生产资产搬不走的预处理（见 `_specs_for`），
    然后交给 `run_specs`——同意闸、权限闸、引用解析、真调用、checker 全在同一处。
    """

    def run(self, tool: str, params: dict) -> tuple[str, int]:
        """→ `(给模型看的正文, 真正执行了几个工具)`（第二项是无进展判据的输入）。"""
        name, args = str(tool), dict(params or {})
        # 无进展判据的键：必须与**模型的原始 `tool_calls`** 同源（`plan_of` 记的
        # `calls[].workflow/params` 就是这两个值）——错一格 ⇒ 无进展判据恒不触发。
        sig = spec_signature(name, args)
        specs, note = self._specs_for(name, args)
        if not specs:
            self.ledger.replies.append({"workflow": name, "reply": note[:200]})
            return (f"{note}\n【内部回执】{name}：本工具**没有执行任何动作**。"), 0
        return self.run_specs(specs, sig=sig, workflow=name)

    def _specs_for(self, name: str, args: dict) -> tuple[list[str], str]:
        """这一条工具调用 → `(spec 串列表, 零工具时的注记)`。**生产资产的两个落点**。

        ① **导航**（`navigate_to`）：别名（"留言板"）、字面路径白名单、IoT 两种措辞
           （"已下线" vs "本站未部署"）、`confirm=False` 这四件都长在
           `instantiate_plan("navigate", ...)` 里，而且**只有那一处**。裸工具默认
           `confirm=True` ⇒ 不接这一步，模型少填一个 `confirm` 就会走"等主人确认"那条路
           （导航整族的形状都变了），别名还会因为过不了路径白名单变成错误帧。所以这里
           **借生产的解析器**，不另写一份。
        ② **参数必填 / 枚举**：`check_call_args` 与 `content_query` 的 `PARAMS.calls` 校验
           用的是同一份 `tool_arg_schemas()`。不接这一步，缺必填会变成 `fn(**args)` 的
           `TypeError` → **错误帧**，而生产是"零工具 + 问一句"——两种形态落在**不同的断言
           族**上（`require_zero_exec` vs 错误帧），还白烧一轮。
        """
        if name == "navigate_to":
            plan = instantiate_plan("navigate", {"target": args.get("path", "")}, self.role)
            specs = [str(s) for s in (plan.get("tools") or [])]
            if specs:
                return specs, ""
            return [], str(plan.get("note") or "") or "这个页面现在去不了，我没有发起跳转。"
        checked = check_call_args(name, args)
        if checked.get("bad"):
            bad = "、".join(str(b) for b in checked["bad"])
            return [], f"这条调用缺参数或参数不合法（{bad}），我没有执行；请补齐后再说一次。"
        return [spec_text(name, dict(checked.get("args") or args))], ""


def _executor_factory(ledger: RunLedger, *, role: str | None, principal: Any,
                      user_msg: str, state: dict, real_tools: dict | None) -> ToolExecutor:
    """`make_ctx(executor_factory=...)` 要的那一格。除执行器换成 `ToolExecutor` 外，
    参数**逐字同 `make_ctx` 里那一行**——两臂的执行器在"谁挡权限、谁判同意、谁记台账"
    上必须完全同源。"""
    return ToolExecutor(real_tools if real_tools is not None else {}, ledger,
                        role=role, principal=principal, user_msg=user_msg,
                        grant=state.get("confirm_grant"), state=state)


class ToolsGoldenArm(ReactGoldenArm):
    """工具臂的臂对象：**只换 `ctx_hooks` 那四格**，`stream` 一个字不覆写。"""

    name = ARM_NAME
    ctx_hooks = {"menu_builder": menu_of, "plan_builder": plan_of,
                 "prompt_builder": prompt_of, "executor_factory": _executor_factory}


def build() -> ToolsGoldenArm:
    """`golden_arm.build_agent()` 的入口（约定名，改名要连着改那边）。"""
    return ToolsGoldenArm(_compiled())
