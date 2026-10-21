# -*- coding: utf-8 -*-
"""能力索引臂（`AGENT_ARM=capability`）：**planner 选能力，索引把工具面交给执行层**。

## 一条回合里的三态（20261021 起）

```
第 0 轮   菜单 = 能力索引（49 条，**只有名字 + 一行触发条件**，零参数 schema）
          模型点一条能力                     ⇒ 只**选定**，零调用
          ├ kind=workflow（写族）  ⇒ ctx["route"]，下一轮**填参轮**
          ├ kind=skill（只读/导航/检索）⇒ ctx["caps"]，下一轮**工具态**
          └ chat                    ⇒ 下一站**叙述层**（闲聊不占执行轮）
填参轮    菜单 = **那一条**能力的 schema（参数回来了）+ 多意图里那批工具
          模型填完参数调用它                  ⇒ 走生产那条 `instantiate_plan` 链
工具态    菜单 = 那条能力的工具面（多数 1–3 件，经 authz 过滤）
          模型点真工具                        ⇒ 执行层逐条真调（同一张图的 execute 节点）
零调用轮   模型自己宣布收尾                    ⇒ 零工具 narrator 照旧写正文
```

三态的**判据是 `ctx` 上那两格**：`ctx["route"]`（待填参数的确定性能力）与 `ctx["caps"]`
（选中的 ReAct 能力）。两者都在 `plan_of` 里被写，`menu_builder` / `prompt_builder` 读它们。
菜单变了就把 `ctx["menu"]` 清掉（见 `react_arm._bound_llm` 的三格注）——**作废由建菜单的
那条臂自己负责**。

**为什么路由轮不带参数 schema**：第 0 轮**每请求都过一遍**，而 33 条模板能力的参数在
"选哪件事"这一步一条都用不上（实测能力项 schema 21,118 字符描述 + 12,996 字符参数 →
2,708 + 0）。参数到**填参轮**才放回来，那时上下文里只有选中那一条。

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
    设备 + 检索）；**写族 30 条走"填参轮"快道**——索引态点到它们时 `plan_of` 只记
    `ctx["route"]`，下一轮模型在那一条的 schema 上填完参数，执行层才走
    `WorkflowExecutor.run`（`instantiate_plan` 展开），与生产**逐字同一条路**。
    所以本臂不是"把生产换掉"，是"给只读族换一条执行方式、给写族把参数挪到第二轮"。
  · **模板那一半仍然承重**：写族的 9 个参数校正器、`_confirm_grant_plan`、变更集展开
    全部原样；本臂只让它们**晚一轮**（先选、下一轮才填参数）。
  · **`chat` 不进执行**（20261021）：它是"这一轮没有业务"的表达，`plan_of` 零调用 +
    `route_to="model"` ⇒ 正文归叙述者。⚠️ 代价如实记：`WorkflowExecutor.run` 里那条
    "纯应答技能留痕 `ledger.replies`"在本臂上**不再发生**（`ledger.replies` 全仓无消费方，
    是只写列；`tests/test_react_line.py` ③c 仍逐字判着那条路本身没被删）。
  · **`plan["workflow"]` 是模型自己选的能力名**（不是 `tools_arm` 那种从工具名倒推的粗标签）
    ⇒ gate 的能力否认族（`CAPABILITY_DENIAL_OBJECTS` 按技能名索引）在本臂上**照常开火**，
    而 `tools_arm` 那一臂上是个静默洞（见该模块头注最后一段）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from agent import react_arm
from agent.capability_index import (
    Capability,
    capability_brief,
    capability_of,
    route_brief,
)
from agent.frame_contract import spec_text
from agent.planner_llm import build_tool_schema
from agent.react_line import RunLedger, WorkflowExecutor
from agent.tool_menu import build_tool_menu, strip_slots
from agent.tool_prompt import (
    PSEUDO_NAMES,
    build_tool_prompt,
    capability_block_text,
    route_block_text,
    tool_menu_text,
)
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


def _thin(items: list[dict]) -> list[dict]:
    """把**能力项**的参数 schema 整片摘掉（函数名与描述留着）。

    **为什么摘**（20261021）：路由轮只回答"这一轮是哪件事"，参数一个都用不上——而 33 条
    模板能力（写族）的参数是路由轮里最贵的一块（实测 12,996 字符，占能力项 schema 的
    三分之一），**每请求都要付一遍**。参数到**填参轮**（`_fill_menu`）才放回来，那时
    上下文里只有选中那一条。

    ⚠️ **三个记账口不清**：`task_intents` 的 `intents` 是 `tasks.normalize_intents` 真读
    的那一格（`slots=False` 都特意留着它），清掉等于把这个申报口做废——那正是"槽位填了
    没人读"的反面：这一格**有人读**。
    """
    out: list[dict] = []
    for item in items:
        fn = dict(item.get("function") or {})
        if _menu_name(item) not in PSEUDO_NAMES:
            fn["parameters"] = dict(_EMPTY_SCHEMA)
        out.append({"type": "function", "function": fn})
    return out


def _index_desc(role: str | None):
    """索引态那一格 `description`：**只留一行触发条件**（`Capability.line`）。

    完整索引项（工具面 + 完成判定）**不在路由轮**：那是"这一轮怎么把这件事做完"的交代，
    归工具态/填参轮的能力简报（`capability_brief` / `route_brief`）。路由轮要模型回答的
    只有"哪一条"，多给的字每请求都付一遍（实测：能力项描述 21,118 → 2,708 字符）。
    """
    def describe(fn: dict) -> str:
        cap = capability_of(_menu_name(fn), role)
        return cap.line if cap is not None else str(fn.get("description") or "")
    return describe


def _index_menu(role: str | None) -> list[StructuredTool]:
    """索引态菜单：**只留名字 + 一行触发条件，不带任何参数 schema**。

    与生产 planner 那份 schema 的差别是**两处**，两处都是"路由轮不需要"：
      · `description` → 一行索引（`_index_desc`）；
      · **能力项的 `parameters` 整片摘掉**（`_thin`）。ADR-0015 阶段 1 曾经要求"函数名 /
        参数 / `required` / `enum` 一个字符不动"，那是**当时**的自变量（菜单从 70 件裸工具
        换成 50 条技能，只该动描述）；现在要拨的自变量是"路由轮该不该看见参数"，摘掉参数
        就是这一拨本身。记账口不清（理由见 `_thin`）。

    **`slots=False`（20261021）**：不挂 `intents` / `reply_kind` 那两个跨切面槽位。本臂
    折算计划时走 `tool_menu.strip_slots`——**填了就扔**；而它们单份 799 字符 × 49 条能力
    = 本菜单的 **59%**（67,441 → 27,892 字符）。`build_tool_schema` 的缺省仍是 `True`
    ⇒ 生产那条路逐字节不变。⚠️ 这一格**不是**"顺手瘦身"：槽位是"给模型一个地方填"的
    申报口，填了没人读等于**骗模型以为申报过了**；摘掉之后意图那条路只剩 `task_intents`
    一个真被读的出口（它自带 `intents`，不受本参数影响）。
    """
    return _wrap(_thin(build_tool_schema(role, slots=False)), _index_desc(role))


def _fill_menu(role: str | None, cap: Capability,
               caps: list[Capability]) -> list[StructuredTool]:
    """填参轮菜单：**选中那一条能力的 schema 原样**（含参数）+ 多意图里那几条 ReAct 能力的
    工具面 + 三个记账口。

    与索引轮的差别**只有一处**：参数放回来了，而且**只有这一条**的。多意图（路由轮同时
    点了一条确定性能力和一条 ReAct 能力）时两批都在——那一轮本来就是两件事都要动。

    ⚠️ 菜单里**只有这一条确定性能力**（不像索引轮列全表）⇒ 模型在填参轮**改不了主意**
    （改主意要回索引轮）。这是有意的：给全表就是让它再选一遍，而它上一轮已经选过了；
    填不出参数时那条"零调用收尾"的出口交给叙述层回问主人（见 `tool_prompt.route_block_text`）。
    """
    items = [i for i in build_tool_schema(role, slots=False) if _menu_name(i) == cap.name]
    return _wrap(items, _index_desc(role)) + _tools_menu(role, caps)


def _tools_menu(role: str | None, caps: list[Capability]) -> list[StructuredTool]:
    """工具态菜单：**只留这几条能力那一批工具**（`tool_menu.build_tool_menu` 已按 authz
    过滤过一遍，这里再与能力面取交——两道都留着，因为它们的失效方向不同：authz 那道防
    "越权可见"，能力面这道防"菜单里出现这一轮不该有的工具"）。

    三个记账口（`task_hold` / `task_drop` / `task_intents`）**恒在**：它们是跨切面槽位，
    与这一轮做哪条能力无关（同 `build_tool_menu` 末尾那两行）。`caps` 为空时这一批只剩
    记账口——填参轮就是这么用的。

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


def _route_cap(ctx: dict | None, role: str | None) -> Capability | None:
    """`ctx["route"]`（路由轮选中的那条**确定性能力**名）→ 索引项。

    取不到、或取到的是 ReAct / 闲聊那两档 ⇒ `None`（**fail-closed**：不认识的 route
    名退回索引态，绝不因此把某条能力的参数绑上）。`chat` 排除在外是因为它**没有填参轮**
    ——路由到它就是"这一轮没有业务"，直接交叙述层（见 `plan_of`）。
    """
    cap = capability_of(str((ctx or {}).get("route") or ""), role)
    if cap is None or cap.react or cap.chat:
        return None
    return cap


def capability_menu(role: str | None, ctx: dict | None = None) -> list[StructuredTool]:
    """接缝①：**按轮变**的菜单，三态。

      · `ctx["route"]` 有值 ⇒ **填参轮**（那一条确定性能力的 schema + 多意图的工具面）；
      · 否则 `ctx["caps"]` 非空 ⇒ **工具态**（ReAct 那一批工具）；
      · 否则 ⇒ **索引态**（名字 + 一行，零参数 schema）。

    填参轮排在工具态**之前**：多意图时两批都在（`_fill_menu` 自己带上 `caps` 的工具面），
    而只有 `route` 的那一轮不该掉进"工具态"（那样它一条能力也点不到）。
    """
    caps = _caps_of(ctx, role)
    route = _route_cap(ctx, role)
    if route is not None:
        return _fill_menu(role, route, caps)
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
      · **填参轮**（`ctx["route"]` 有值）：菜单是**一条确定性能力**，而 `tool_menu_text`
        读的 `build_tool_menu` 里没有它 ⇒ 那一格走 `menu_text` 逐字交出去；简报走
        `route_brief`（`capability_brief` **去掉工具面那一句**——那批工具由系统自己展开、
        一件都没绑，说"该调的都调上"就是让模型去点一个结构上点不到的名字）。
    """
    caps = _caps_of(ctx, ctx.get("role"))
    route = _route_cap(ctx, ctx.get("role"))
    messages = list(state.get("messages") or [])
    if route is None and not caps:
        return react_arm._system_prompt(
            messages, ctx.get("role"), ctx.get("principal"),
            ctx.get("config"), ctx.get("user_msg") or "",
            exec_rows=(state.get("ledger") or {}).get("exec_rows"))
    user_msg = str(ctx.get("user_msg") or "")
    common = dict(
        messages=messages, role=ctx.get("role"), principal=ctx.get("principal"),
        config=ctx.get("config"), user_msg=user_msg,
        ledger_frame=react_arm.ledger_frame_for(
            messages, ctx.get("principal"), ctx.get("config"), user_msg),
        correction=str(ctx.get("correction") or ""),
        exec_rows=(state.get("ledger") or {}).get("exec_rows"))
    if route is None:
        return build_tool_prompt(
            **common,
            capability="\n\n".join(capability_brief(c) for c in caps),
            tool_names=_face_names(ctx.get("role"), caps))
    # 填参轮：菜单那一段逐字给（`_fill_menu` 绑的就是这两批），简报也逐字给。
    menus = [f"· {route.name} — {route.line}"]
    blocks = [route_block_text(route_brief(route))]
    if caps:
        menus.append(tool_menu_text(ctx.get("role"), _face_names(ctx.get("role"), caps)))
        blocks.append(capability_block_text(
            "\n\n".join(capability_brief(c) for c in caps)))
    return build_tool_prompt(**common, menu_text="\n".join(menus),
                             capability_block="\n\n".join(blocks))


def _classify(name: str, role: str | None, route: str) -> tuple[str, str]:
    """模型点的一个名字落到哪一路，返回 `(路, 值)`：

      · `("skill", 名字)` —— ReAct 那一档（`kind=skill`）：并进 `ctx["caps"]`，零调用；
      · `("chat", "")` —— 闲聊：零调用，下一站**叙述层**；
      · `("call", "")` —— 这一条**现在就执行**：填参轮那条确定性能力（`route == name`）、
        或认不出的名字/真工具名（执行层按名字分派，BLOCK 由它判）；
      · `("route", 名字)` —— 确定性能力（`kind=workflow`）的路由：只记下来，
        下一轮才给它填参数。

    **`chat` 单列一路**（而不是并进 `workflow`）：它没有填参轮——路由到它就是"这一轮没有
    业务"，给闲聊排一个执行轮等于让最常见的那条路多花一次 LLM 调用。
    """
    cap = capability_of(name, role)
    if cap is None:
        return ("call", "")
    if cap.react:
        return ("skill", name)
    if cap.chat:
        return ("chat", "")
    return ("call", "") if route == name else ("route", name)


def _next_hop(*, calls: list, caps_changed: bool, route: str, chat: bool) -> str:
    """下一站。**顺序是有意的**：真有调用就执行；选了 ReAct 能力或确定性能力 ⇒ 回
    `planner`（等它那一轮）；只剩闲聊 ⇒ 叙述层；都不沾（空计划）⇒ 照旧 `execute`
    （零调用那一轮，与改动之前同形）。"""
    if calls:
        return "execute"
    if route or caps_changed:
        return "planner"
    return "model" if chat else "execute"


def plan_of(tool_calls: list, role: str | None, ctx: dict | None = None) -> dict:
    """接缝③：模型的 `tool_calls` → `plan_obj`。**路由轮只路由，不执行**（20261021）。

    点到的名字分四路，只有最后一路产出 `calls`：

      · **射程内的能力**（`kind=skill`）⇒ 记进 `ctx["caps"]`。零调用是设计，不是空计划：
        下一轮菜单换成它那一批工具，由执行层做事。同一轮点了几条（多意图）⇒ 并起来。
      · **`kind=workflow` 的能力**（写族）⇒ **两段式**：路由轮只记 `ctx["route"]`（那一轮
        的菜单里它**没有参数 schema**，模型也填不出参数），下一轮菜单换成它自己的 schema
        （`_fill_menu`），模型填完参数才产出 `calls`。今天之前它是**当轮就执行**的，
        代价是第 0 轮要为 33 条模板能力背 12,996 字符参数——路由轮一条都用不上。
      · **`chat`** ⇒ 零调用，且下一站是**叙述层**（`route_to="model"`）。这正是主人说的
        "planner 路由到 chat"：闲聊 = 这一轮没有业务，正文归叙述者。它**不占执行轮**
        （给 chat 排一个填参轮等于让最常见的闲聊多花一次 LLM 调用）。
      · **认不出的名字 / 真工具名** ⇒ 照常产出 `calls`，执行层按名字分派（BLOCK 由它判）。

    **下一站由本函数指定**（`route_to`）：缺省臂的 `_plan_of` 从不写这一格 ⇒ `planner_node`
    走原来那条 `execute`，逐字节不变。这一格存在的理由：路由轮零调用，但下一站不是
    `execute`（等填参数的必须回 `planner`，闲聊的直接去 `model`），而"选了哪条能力"
    只有折算这一处看得见（`planner_node` 不往 `messages` 里写东西）。
    """
    ctx = ctx if ctx is not None else {}
    before = list(ctx.get("caps") or [])
    caps = list(before)
    route = str(ctx.get("route") or "")
    # 这一轮点到过的**全部**能力名（含写族与 `chat`）。它只用来定 `workflow` 那一格，
    # 不进 `ctx["caps"]`——写族与 `chat` 的菜单**不该**切到它们的工具面
    # （切了下一轮模型就看不见能力表，多意图里剩下的那几件再也点不到）。
    named: list[str] = []
    tools: list[str] = []
    calls: list[dict] = []
    new_route = ""
    chat = False
    for c in tool_calls or []:
        name = str((c or {}).get("name") or "")
        if not name:
            continue
        args = strip_slots((c or {}).get("args") or {})
        kind, val = _classify(name, role, route)
        if name not in named:
            named.append(name)
        if kind == "skill":
            if name not in caps:
                caps.append(name)
        elif kind == "chat":
            chat = True
        elif kind == "route":
            # 路由轮（或填参轮改了主意）：只记**第一条**，下一轮才给它填参数
            new_route = new_route or val
        else:
            tools.append(spec_text(name, args))
            calls.append({"workflow": name, "params": args})
    if not named and not calls:
        return {}
    ctx["caps"] = caps
    # 路由格：填参轮成功了 ⇒ 作废（下一轮回索引，多意图里剩下的才点得到）；模型在填参轮
    # 改点了别的确定性能力 ⇒ 换成它，接着填。**只在真的变了的时候清菜单**——`ctx["menu"]
    # 是"菜单按轮变"唯一的作废口（见 `react_arm._bound_llm`），多清一次就多重绑一次。
    next_route = new_route if (calls or new_route) else route
    if caps != before or next_route != route:
        ctx["menu"] = None
    ctx["route"] = next_route
    # `workflow` 取**第一条能力名**而不是 `|` 连起来：`plan_codec.normalize_plan` 会把
    # 不在 `WORKFLOW_MAP` 里的取值改成 `chat`（`a|b` 不在），而 gate 按它分族、
    # `CAPABILITY_DENIAL_OBJECTS` 按技能名索引——落成 `chat` 等于把那一族关掉。
    # 兜底顺序：这一轮选的 ReAct 能力 → 这一轮点到的任意能力（确定性/`chat`）→ `chat`。
    label = (caps or named or [""])[0]
    return {"workflow": label or "chat", "tools": tools, "note": "",
            "reply": "", "status": "", "calls": calls,
            "route_to": _next_hop(calls=calls, caps_changed=caps != before,
                                  route=next_route, chat=chat)}


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
