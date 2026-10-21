# -*- coding: utf-8 -*-
"""能力索引臂（`agent/capability_arm.py`）的判据：离线、零网络零 LLM。

**为什么单起一套**：本臂是**唯一**"菜单按轮变"的臂，而它的失效方式全是静默的——
菜单没换（第二轮还绑着能力表 ⇒ 模型点不出工具）、换早了（写族那一轮也切走 ⇒ 多意图里
剩下的点不到）、提示词没跟着换（菜单是工具表、提示词还在讲技能表 ⇒ 自相矛盾的提示词）。
这三种都不让别的套件红，而读数会掉得像是"模型变笨了"。

判据分六组：

  ① **三态菜单**：`ctx["route"]` 有值 ⇒ 填参态（那一条确定性能力的 schema）；否则
     `ctx["caps"]` 空 ⇒ 索引态（名字集合 == 可见技能 + 三个记账口）；非空 ⇒ 工具态
     （名字集合 == 那条能力的工具面 ∩ authz 可见 + 三个记账口）。
  ② **索引态只留「名字 + 一行」**：能力项**零参数 schema**、描述 == 一行索引；记账口的
     参数**一个都不许被连带摘掉**。
  ③ **端到端跑一轮**（`test_arm_seams` 那套脚本化假模型）：第 0 轮点能力 ⇒ 零工具、
     `plan["workflow"]` 是能力名；第 1 轮绑的是那条能力的工具面；工具真被调、帧发出来。
  ④ **提示词成对换**：索引态 == `react_arm._system_prompt` 那一份；工具态含能力简报的
     三格（工具面 / 完成判定 / 回复契约）。
  ⑤ **不扩权 + 不重写 harness**：工具态菜单 ⊆ authz 可见工具；够不着的 `caps` 名被丢掉；
     源码里不出现权限闸/同意闸/引用解析的第二份实现（只许调 `run_specs`）。
  ⑥ **路由轮只路由**（20261021）：`kind=workflow` 的能力点中当轮**零调用**、记
     `ctx["route"]`；下一轮菜单是它自己的 schema（含参数）、提示词是填参那份；填完参数
     才产出 `calls` 进 `execute`。`chat` 零调用且下一站是**叙述层**（不占执行轮）。
"""

from __future__ import annotations

import itertools
import re
import sys
from pathlib import Path
from typing import Any

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage  # noqa: E402
from langchain_core.outputs import ChatGenerationChunk  # noqa: E402

from agent import capability_arm as CA  # noqa: E402
from agent import react_arm as R  # noqa: E402
from agent import tool_menu as TM  # noqa: E402
from agent import tool_prompt as TP  # noqa: E402
from agent import workflow_defs as W  # noqa: E402
from agent.capability_index import capabilities_for  # noqa: E402
from agent.principal import Principal  # noqa: E402
from agent.prompts import BLOG_ASSISTANT_PROMPT  # noqa: E402
from agent.tool_prompt import PSEUDO_NAMES  # noqa: E402

FAILED: list[str] = []


check = collector(FAILED)

SEEN: list[list] = []
BOUND: list[list] = []
RAN: list = []


class _Res(str):
    """工具返回的最小克隆：`str` 子类 + `kind`/`meta`（同 `tools.base.ToolResult`）。"""

    def __new__(cls, text: str, kind: str = "ok", meta: dict | None = None):
        o = super().__new__(cls, text)
        o.kind = kind
        o.meta = meta or {}
        return o


class _Scripted(GenericFakeChatModel):
    """按脚本吐消息的假模型。与 `tests/test_arm_seams.py::_Scripted` 逐字同源。

    **必须自己实现 `_stream`**：基类只读 `content`、把 `tool_calls` 整个丢掉，而 langgraph
    每个节点都挂 `StreamMessagesHandler` ⇒ 照抄基类就得到"模型一个工具都不调"的假现场。
    """

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        BOUND.append([str(getattr(t, "name", "") or "") for t in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        SEEN.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        msg = self._generate(messages, stop=stop, run_manager=run_manager,
                             **kwargs).generations[0].message
        toks = [t for t in re.split(r"(\s)", str(msg.content or "")) if t] or [""]
        for i, tok in enumerate(toks):
            last = i == len(toks) - 1
            chunk = AIMessageChunk(
                content=tok, id=getattr(msg, "id", None),
                tool_calls=(list(getattr(msg, "tool_calls", None) or []) if last else []))
            if run_manager:
                run_manager.on_llm_new_token(tok, chunk=ChatGenerationChunk(message=chunk))
            yield ChatGenerationChunk(message=chunk)


NAV_CMD = {"kind": "navigate", "url": "/", "mode": "direct"}


def _nav_tool(**kw):
    RAN.append(kw)
    return _Res(f"页面已跳转：{kw.get('path')}",
                meta={"cmd": {**NAV_CMD, "url": kw.get("path")}})


def _call(workflow: str, **args) -> AIMessage:
    """一次决策轮（第一个形参不叫 `name`：写技能的参数恰好就叫 `name`，撞名会 TypeError）。"""
    return AIMessage(content="让我先看看", tool_calls=[
        {"name": workflow, "args": args, "id": f"call_{workflow}", "type": "tool_call"}])


ADMIN = Principal(uid=9, role="admin")


def _run(script: list, tools: dict | None = None, *, msg: str = "带我去首页",
         principal: Any = ADMIN) -> list:
    """跑一次 `arm.stream`（本臂真对象，不套壳——`ctx_hooks` 就是它自己的那四格）。

    `principal` 缺省是 **admin**：`tool_menu.visible_tools(None)` 是空集（authz
    fail-closed），不给身份 ⇒ 工具态菜单里一件真工具都没有，那一组判据就成了空过。
    缺省档那一组（`principal=None`）另有一条判据专门钉它。
    """
    SEEN.clear()
    BOUND.clear()
    RAN.clear()
    # 存旧值、跑完还原：本仓的 `_process_isolation` 会替我们收，但**收了也会打一行
    # `[跨套件全局] …改了没还原`**——那行噪声会让"谁真的漏了"变得看不出来。
    old = (R._llm, R._narrator_llm, R._real_tools, R._system_prompt)
    R._llm = lambda: _Scripted(messages=iter(script), ai_message_chunk=iter([]))
    R._narrator_llm = lambda: GenericFakeChatModel(
        messages=itertools.repeat(AIMessage(content="好了")))
    R._real_tools = lambda *a, **k: dict(tools or {"navigate_to": _nav_tool})
    R._system_prompt = lambda *a, **k: "你是测试用助手（索引态提示词桩）。"
    cfg = {"configurable": {"principal": principal}} if principal else {}
    try:
        return list(CA.CapabilityArm(R._compiled()).stream(
            {"messages": [HumanMessage(content=msg)]}, cfg))
    finally:
        R._llm, R._narrator_llm, R._real_tools, R._system_prompt = old


def _plans(frames: list) -> list[dict]:
    """帧里所有 `planner` 那一格的 update（按发生顺序）。空 update（`None`）跳过。"""
    return [d["planner"] for mode, d in frames
            if mode == "updates" and isinstance(d, dict) and d.get("planner")]


def _sys_prompts() -> list[str]:
    return [str(m.content) for msgs in SEEN for m in msgs if type(m).__name__ == "SystemMessage"]


# ── ① 两态菜单 ────────────────────────────────────────────────────────────
def test_menu_two_phases():
    for role in (None, "user", "admin"):
        want = {w.name for w in W.visible_workflows(role)} | set(PSEUDO_NAMES)
        got = {t.name for t in CA.capability_menu(role, {})}
        check(f"索引态菜单 == 可见技能 + 三个记账口（role={role}）", got == want,
              f"多 {sorted(got - want)[:4]} / 少 {sorted(want - got)[:4]}")

        nav = CA.capability_menu(role, {"caps": ["navigate"]})
        # `role=None`（身份不明）那一档**只该剩记账口**：`tool_menu.visible_tools` 走
        # authz，身份不明 ⇒ 零权限 ⇒ 一件真工具都不可见。这不是缺陷，是 fail-closed；
        # 判据写死它，免得哪天有人为了"让探针跑起来"把它放宽。
        want_tools = set(PSEUDO_NAMES) if role is None else {"navigate_to"} | set(PSEUDO_NAMES)
        check(f"工具态菜单 == 那条能力的工具面 + 三个记账口（role={role}）",
              {t.name for t in nav} == want_tools, str([t.name for t in nav]))

    # 多能力（多意图）：工具面取**并集**，不是交集也不是覆盖
    both = {t.name for t in CA.capability_menu("admin", {"caps": ["navigate", "effect"]})}
    check("两条能力 ⇒ 工具面是并集", both == {"navigate_to", "toggle_effect"} | set(PSEUDO_NAMES),
          str(sorted(both)))

    # 反控：一句"恒返回索引态"的实现过不了上面那条工具态判据（两边名字集合不同）
    check("反控：索引态与工具态的名字集合**确实不同**（否则上面两条判不出东西）",
          {t.name for t in CA.capability_menu("admin", {})}
          != {t.name for t in CA.capability_menu("admin", {"caps": ["navigate"]})})


# ── ② 索引态只留「名字 + 一行」：零参数 schema ────────────────────────────
def test_index_menu_is_thin():
    """路由轮的菜单相对生产 schema 的差别是**两处**，两处都是"路由轮不需要"：

      · `description` → **一行索引**（`Capability.line`，不是完整索引项——工具面与完成
        判定归工具态/填参轮的简报）；
      · 能力项的 `parameters` → **整片摘掉**。33 条模板能力的参数在路由轮一条都用不上，
        实测 12,996 字符，而路由轮**每请求都要过一遍**。
      · 两个跨切面槽位（`intents` / `reply_kind`）也不挂（`slots=False`）：本臂折算计划
        时走 `tool_menu.strip_slots`，填了就扔。

    ⚠️ 判据要**同时**钉"该摘的摘了"与"不该动的没动"：只判前者，把 `description` 也
    清空、或把记账口的参数一起摘了，都会过。
    """
    from agent.capability_index import capability_of
    from agent.planner_llm import build_tool_schema
    from agent.prompt_context import REPLY_KIND_ARG
    from agent.tasks import INTENTS_ARG

    for role in (None, "user", "admin"):
        prod = {t["function"]["name"]: t["function"] for t in build_tool_schema(role, slots=False)}
        for tool in CA.capability_menu(role, {}):
            cap = capability_of(tool.name, role)
            if cap is None:
                continue
            props = (tool.args_schema or {}).get("properties") or {}
            check(f"索引态能力项零参数 schema：{tool.name}（role={role}）",
                  props == {}, str(sorted(props))[:60])
            check(f"索引态能力项描述 == 一行索引：{tool.name}（role={role}）",
                  tool.description == cap.line, tool.description[:60])
            check(f"索引态没有那两个没人读的槽位：{tool.name}（role={role}）",
                  INTENTS_ARG not in props and REPLY_KIND_ARG not in props)
            check(f"函数名没被顺手改掉：{tool.name}（role={role}）",
                  tool.name in prod, tool.name)
    # 反控 ①：**参数确实被摘掉了**（否则上面那条判的是"本来就空"）
    on = {t["function"]["name"]: t["function"] for t in build_tool_schema("admin")}
    check("反控：生产 schema 里 `navigate` 的参数非空（索引态那一片不是「本来就没有」）",
          bool((on["navigate"]["parameters"].get("properties") or {})),
          str(sorted(on["navigate"]["parameters"].get("properties") or {}))[:60])
    # 反控 ②：`slots=True`（生产那条路）两个槽位照旧挂着——摘的只是本臂这一份
    check("反控：生产 schema（slots=True）两个槽位照旧在（没顺手把生产也摘了）",
          INTENTS_ARG in on["navigate"]["parameters"]["properties"]
          and REPLY_KIND_ARG in on["navigate"]["parameters"]["properties"])
    # 反控 ③：**记账口的参数不许被连带摘掉**——`task_intents` 的 `intents` 是
    # `tasks.normalize_intents` 真读的那一格（`slots=False` 都特意留着它）。
    # 这条是"该摘的摘、不该动的没动"里那个**没动**。
    pseudo = {t.name: (t.args_schema or {}) for t in CA.capability_menu("admin", {})}
    check("反控：`task_intents` 自己的 `intents` 参数没被连带摘掉",
          INTENTS_ARG in ((pseudo.get("task_intents") or {}).get("properties") or {}),
          str(sorted(((pseudo.get("task_intents") or {}).get("properties") or {}))))
    # 反控 ④：`_thin` 是"只动能力项"的**判据级**反证——手工拿一条能力项过一遍，
    # 参数必须当场变空；同一份数据里的记账口必须原样。
    raw = [t for t in build_tool_schema("admin", slots=False)
           if t["function"]["name"] in ("navigate", "task_intents")]
    thin = {i["function"]["name"]: i["function"]["parameters"] for i in CA._thin(raw)}
    check("反控：`_thin` 只清能力项的参数（记账口原样）",
          thin["navigate"] == {"type": "object", "properties": {}}
          and bool((thin["task_intents"].get("properties") or {})),
          f"{thin['navigate']} / {sorted((thin['task_intents'].get('properties') or {}))}")


# ── ③ 端到端跑一轮 ────────────────────────────────────────────────────────
def test_end_to_end_turn():
    frames = _run([_call("navigate", target="首页"), _call("navigate_to", path="首页"),
                   AIMessage(content="已经带你过去了")])
    plans = _plans(frames)
    check("跑了至少两轮 planner（索引态那一轮 + 工具态那一轮）", len(plans) >= 2, str(len(plans)))
    if len(plans) >= 2:
        first, second = plans[0], plans[1]
        check("第 0 轮：**零调用**（只选定能力，不执行）",
              "navigate_to(" not in (first.get("plan") or ""), (first.get("plan") or "")[:80])
        check("第 0 轮的 workflow 是**模型自己选的能力名**",
              (first.get("plan_obj") or {}).get("workflow") == "navigate",
              str((first.get("plan_obj") or {}).get("workflow")))
        check("第 1 轮：真工具的 spec 进了计划",
              "navigate_to(" in (second.get("plan") or ""), (second.get("plan") or "")[:80])

    check("绑过**两次**菜单（索引态一次、工具态一次；菜单没换 ⇒ 这里只有一次）",
          len(BOUND) == 2, str([b[:3] for b in BOUND]))
    if len(BOUND) == 2:
        check("第 0 轮绑的是索引态（里面有 `navigate` 这条能力名）",
              "navigate" in BOUND[0] and "navigate_to" not in BOUND[0], str(BOUND[0][:6]))
        check("第 1 轮绑的是**那条能力的工具面**（`navigate_to` + 三个记账口）",
              set(BOUND[1]) == {"navigate_to"} | set(PSEUDO_NAMES), str(BOUND[1]))

    tool_msgs = [m for msgs in SEEN for m in msgs if type(m).__name__ == "ToolMessage"]
    check("真工具**被执行了**（帧发出来了，名字是真工具名）",
          any(getattr(m, "name", "") == "navigate_to" for m in tool_msgs),
          str([getattr(m, "name", "") for m in tool_msgs]))
    check("桩工具真的被调用过（不是「零执行 + 看起来跑通」）", bool(RAN), str(RAN))


# ── ④ 提示词成对换 ────────────────────────────────────────────────────────
def test_prompt_follows_menu():
    _run([_call("navigate", target="首页"), _call("navigate_to", path="首页"),
          AIMessage(content="好了")])
    prompts = _sys_prompts()
    check("第 0 轮的系统提示**就是 `_system_prompt` 那一份**（索引态不另写提示词）",
          prompts and prompts[0] == "你是测试用助手（索引态提示词桩）。", str(prompts[:1])[:90])
    check("第 1 轮换了提示词（菜单换了提示词不换 = 自相矛盾的提示词）",
          len(prompts) >= 2 and prompts[1] != prompts[0], f"{len(prompts)} 份")

    # 工具态那份必须带能力简报的三格——B、C 两族的修复点就在这三格里
    from agent.capability_index import capability_brief, capability_of
    brief = capability_brief(capability_of("navigate", None))
    second = prompts[1] if len(prompts) >= 2 else ""
    check("工具态提示词含**工具面**那一句", "本能力由这些工具完成：navigate_to" in second,
          second[:80])
    check("工具态提示词含**完成判定**",
          "完成判定：" in second and "navigate_to 的执行回执" in second)
    check("工具态提示词含**该调的都调上**那句（B 族的修复点）",
          "该调的工具都调上" in second)
    check("反控：简报确实进了提示词（逐字子串，不是「看着像」）", brief.split("\n")[0] in second,
          brief.split("\n")[0])

    # ⚠️ 提示词里那张清单必须与**绑给模型的 schema** 是同一批工具（20261021）。
    # 不判这条的后果实测过：schema 只绑了 `navigate_to`，提示词却印着全部 73 件，
    # 而抬头上写着"**唯一可选集合**"——模型照着文本去点一个它结构上点不到的名字，
    # 正是 POC 里 A 族（13 条路由红）的形态。
    # 只取 `{tool_menu}` 那一格（"可调用工具（" 到 "判定规则：" 之间）——整份提示词里
    # 还有别的 `· ` 行（立场改写、纪律那几段），不圈定范围就会把它们当成工具名。
    seg = second[second.find("可调用工具（"):second.find("判定规则：")]
    listed = [ln.split(" — ")[0].removeprefix("· ").strip()
              for ln in seg.splitlines() if ln.startswith("· ")]
    check("工具态提示词里的工具清单 == 那条能力的工具面（不是全表）",
          set(listed) == {"navigate_to"}, str(listed[:8]))
    full, face = TP.tool_menu_text("admin"), TP.tool_menu_text("admin", {"navigate_to"})
    check("反控：全量菜单文本**确实更长**（否则上面那条判的是「本来就短」）",
          len(full) > len(face), f"{len(full)} vs {len(face)}")

    # 装配顺序：静态块在契约**之前**（前缀缓存的承重件，同提交 `8bbe3f0` 的手法）。
    # 判据读**真发出去那一份**、且用**资产自己的第一行**当地标（不手抄一句提示词正文：
    # 抄一句就多一个漂移源，资产改了这个判据会静默失效）。
    head = next(ln for ln in (BLOG_ASSISTANT_PROMPT or "").splitlines() if ln.strip())
    check("工具态提示词是「静态资产在前、契约在后」（前缀缓存的承重件）",
          0 <= second.find(head) < second.find("可调用工具（"),
          f"人设首行 @{second.find(head)}，菜单抬头 @{second.find('可调用工具（')}")
    # 契约**内部**同理：静态的判定规则要在易变的「页面上下文」之前
    check("契约内部也是「静态规则在前、易变数据在后」",
          0 <= second.find("判定规则：") < second.find("页面上下文："),
          f"判定规则 @{second.find('判定规则：')}，页面上下文 @{second.find('页面上下文：')}")


# ── ⑤ 不扩权 + 不重写 harness ─────────────────────────────────────────────
def test_no_privilege_growth_and_no_rewrite():
    # 工具态菜单 ⊆ authz 可见工具（`tool_menu.visible_tools` 就是执行期那一张表）
    for role in ("user", "admin"):
        visible = set(TM.visible_tools(role))
        for cap in capabilities_for(role):
            if not cap.react:
                continue
            names = {t.name for t in CA.capability_menu(role, {"caps": [cap.name]})}
            check(f"工具态菜单 ⊆ authz 可见工具：{cap.name}（role={role}）",
                  names - set(PSEUDO_NAMES) <= visible,
                  str(sorted(names - set(PSEUDO_NAMES) - visible)))

    # 够不着的 caps 名（越权/不存在）被丢掉 ⇒ 退回索引态，绝不把别人的工具面绑上
    for junk in ("account_freeze", "no_such_skill", ""):
        names = {t.name for t in CA.capability_menu("user", {"caps": [junk]})}
        check(f"反控：够不着的 caps 名不生效（{junk!r}）",
              "account_freeze" not in names, str(sorted(names)[:5]))

    # 源码锁：权限闸 / 同意闸 / 引用解析 / checker 的第二份实现一个都不许有
    src = (ROOT / "agent" / "capability_arm.py").read_text(encoding="utf-8")
    for banned in ("authz.check", "must_enforce", "requires_consent", "resolve_args",
                   "_check_spec", "instantiate_plan("):
        check(f"不重写 harness：源码里没有 `{banned}`（只许调 `run_specs`）",
              banned not in src, banned)
    check("执行器是 `ToolExecutor` 的子类（`run_specs` 那一整套原样继承）",
          "class CapabilityExecutor(ToolExecutor)" in src)
    check("臂只换 `ctx_hooks` 那四格（`stream` 一个字不覆写）",
          "def stream(" not in src and set(CA.CapabilityArm.ctx_hooks) == {
              "menu_builder", "plan_builder", "prompt_builder", "executor_factory"},
          str(sorted(CA.CapabilityArm.ctx_hooks)))


# ── ⑥ 路由轮只路由（`kind=workflow` 的确定性快道） ────────────────────────
def test_route_round_only_routes():
    """20261021：`kind=workflow` 的能力**两段式**——路由轮只记，填参轮才执行。

    判据取**行为**（`plan_obj` 里有没有调用、菜单绑的是谁、下一站是哪），不取实现：
      · 路由轮：零调用 + `ctx["route"]` 记下 + 下一站 `planner`（**不回 execute**）；
      · 填参轮：菜单换成**那一条**的 schema（参数回来了）+ `calls` 产出 + 下一站 `execute`；
      · `chat`：零调用 + 下一站 `model`（闲聊不该占一个执行轮）；
      · ReAct 能力 / 裸工具名：与改动之前**逐字同形**（这一拨没有动它们）。
    """
    from agent.capability_index import capability_of
    from agent.tool_prompt import route_block_text

    WF, SKILL = "dashboard_todo_add", "navigate"

    # ① 路由轮：零调用、记 route、下一站 planner
    ctx: dict = {}
    obj = CA.plan_of([{"name": WF, "args": {}, "id": "a"}], "admin", ctx)
    check("路由轮**零调用**（写族不再当轮执行）", obj.get("calls") == [], str(obj.get("calls")))
    check("路由轮把能力名记进 `ctx[\"route\"]`", ctx.get("route") == WF, str(ctx.get("route")))
    check("路由轮**作废菜单**（下一轮才绑得上它的 schema）", ctx.get("menu", "未清") is None)
    check("路由轮的下一站是 `planner`（等填参数），不是 `execute`",
          obj.get("route_to") == "planner", str(obj.get("route_to")))
    check("`plan_obj['workflow']` 仍是**模型自己选的能力名**（gate 按它分族）",
          obj.get("workflow") == WF, str(obj.get("workflow")))

    # ② 填参轮：菜单换成那一条（参数回来了）、产出调用、下一站 execute
    fill = {t.name: t for t in CA.capability_menu("admin", ctx)}
    check("填参轮菜单 == 那一条能力 + 三个记账口",
          set(fill) == {WF} | set(PSEUDO_NAMES), str(sorted(fill)))
    props = (fill[WF].args_schema or {}).get("properties") or {}
    check("填参轮**参数回来了**（路由轮摘掉的那一片）", bool(props), str(sorted(props)))
    check("  反控：索引轮那一条的参数是空的（两态确实不同形）",
          not (({t.name: t for t in CA.capability_menu("admin", {})}[WF].args_schema
                or {}).get("properties") or {}))
    obj2 = CA.plan_of([{"name": WF, "args": {"text": "买菜"}, "id": "b"}], "admin", ctx)
    check("填参轮产出调用（执行层按名字走 `instantiate_plan` 那条链）",
          [c.get("workflow") for c in obj2.get("calls") or []] == [WF], str(obj2.get("calls")))
    check("填参轮完成后**路由作废**（下一轮回索引，多意图里剩下的才点得到）",
          not ctx.get("route"), str(ctx.get("route")))
    check("填参轮的下一站是 `execute`", obj2.get("route_to") == "execute",
          str(obj2.get("route_to")))

    # ③ 提示词成对换：填参那份说的是"只填参数"，**不是**"该调的工具都调上"
    from agent.capability_index import route_brief
    prompt = CA.capability_prompt({"messages": []}, {"role": "admin", "route": WF,
                                                     "user_msg": "记一下买菜"})
    check("填参轮提示词含那一条能力的简报",
          route_brief(capability_of(WF, "admin")).split("\n")[0] in prompt)
    check("填参轮提示词说的是「只做它一件事」", "这一轮只做一件事" in prompt, prompt[:0])
    check("**反控**：填参轮提示词里没有「该调的工具都调上」"
          "（那批工具由系统展开、一件都没绑，说了就是让模型点一个点不到的名字）",
          "该调的工具都调上" not in prompt)
    check("填参轮提示词里那一段 == `route_block_text`（不是 `capability_block_text`）",
          route_block_text(route_brief(capability_of(WF, "admin"))) in prompt)

    # ④ `chat`：零调用 + 下一站**叙述层**（不占执行轮）
    cctx: dict = {}
    cobj = CA.plan_of([{"name": "chat", "args": {}, "id": "c"}], "admin", cctx)
    check("`chat` 零调用", cobj.get("calls") == [], str(cobj.get("calls")))
    check("`chat` 的下一站是 `model`（正文归叙述者）", cobj.get("route_to") == "model",
          str(cobj.get("route_to")))
    check("`chat` 不占路由格（下一轮不该去给它填参数）", not cctx.get("route"),
          str(cctx.get("route")))

    # ⑤ 没有动的那两档：ReAct 能力照旧进 `caps`、裸工具名照旧产出调用
    rctx: dict = {}
    robj = CA.plan_of([{"name": SKILL, "args": {"target": "首页"}, "id": "d"}], "admin", rctx)
    check("ReAct 能力照旧进 `ctx[\"caps\"]` 且零调用",
          rctx.get("caps") == [SKILL] and robj.get("calls") == [],
          f"{rctx.get('caps')} / {robj.get('calls')}")
    check("  ReAct 那一轮的下一站也是 `planner`（下一轮绑它那一批工具）",
          robj.get("route_to") == "planner", str(robj.get("route_to")))
    bctx: dict = {}
    bobj = CA.plan_of([{"name": "list_notes", "args": {}, "id": "e"}], "admin", bctx)
    check("裸工具名照旧产出调用（执行层按名字分派、BLOCK 由它判）",
          [c.get("workflow") for c in bobj.get("calls") or []] == ["list_notes"],
          str(bobj.get("calls")))

    # ⑥ 端到端（真图）：路由轮之后**没有 execute 帧**，填参轮之后才有。
    # ⚠️ 身份必须带 `write.console`（`Principal(uid=9, role="admin")` 没有）——那一档下
    # 写 spec 会被**硬权限闸**拦下（`authz.check` 不许 + `must_enforce`）⇒ 既不上卡也不
    # 执行，判据会红在"没弹卡"上，而那不是本组要判的事。
    frames = _run([_call(WF), _call(WF, text="买菜"), AIMessage(content="记下了")],
                  tools={"create_dashboard_todo": lambda **kw: _Res("已记下：买菜")},
                  msg="帮我记一下买菜", principal=Principal(uid=1, role="superadmin"))
    plans = _plans(frames)
    ex = [d["execute"] for mode, d in frames
          if mode == "updates" and isinstance(d, dict) and d.get("execute")]
    check("端到端：跑了至少两轮 planner", len(plans) >= 2, str(len(plans)))
    if len(plans) >= 2:
        check("端到端：**路由轮的 `calls` 是空的**（零执行）",
              (plans[0].get("plan_obj") or {}).get("calls") == [],
              str((plans[0].get("plan_obj") or {}).get("calls")))
        check("端到端：填参轮的 `calls` 非空",
              bool((plans[1].get("plan_obj") or {}).get("calls")),
              str((plans[1].get("plan_obj") or {}).get("calls")))
    check("端到端：绑过两次菜单，第一次是索引态、第二次是那一条能力",
          len(BOUND) >= 2 and WF in BOUND[1] and set(BOUND[1]) == {WF} | set(PSEUDO_NAMES),
          str([b[:3] for b in BOUND]))
    check("端到端：填参轮之后图真的走到了 `execute`（不是又回 planner）",
          bool(ex), str(len(ex)))

    # 快道接的是**生产那条展开链**：能力名 → `instantiate_plan` → 真工具 spec → 同意闸。
    # 这里判到"真工具 spec 进了 `pending`"为止——再往下是写通道，归 `tests/test_confirm*.py`。
    from agent.react_line import RunLedger as _RL
    _led = _RL()
    CA.CapabilityExecutor({"create_dashboard_todo": lambda **kw: _Res("已记下")}, _led,
                          role="superadmin",
                          principal=Principal(uid=1, role="superadmin")).run(WF, {"text": "买菜"})
    check("快道把能力名展开成它计划里的**真工具**（`instantiate_plan` 那条链）",
          [p.get("tool") for p in _led.pending] == ["create_dashboard_todo"],
          str([p.get("tool") for p in _led.pending]))
    # 弹卡那一轮的形状：`pending_confirm` ⇒ END（纯函数，不依赖现场）。
    # ⚠️ 离线现场**弹不出卡**：`_confirm_popup` 在 `settings.jwt_secret` 为空时不签发
    # 令牌、直接不弹（`graph.py` 那一支有 20260922 的 CI 实测记录），而本套件钉着
    # `SAUDADE_IGNORE_ENV_FILE=1`。所以卡面本身归 `tests/test_confirm*.py` 判。
    check("弹卡那一轮到此为止（`pending_confirm` ⇒ END，不叫 narrator）",
          R.route_after_execute({"pending_confirm": {"q": "?"}}, None) == "end")


def test_capability_arm():
    """收集器入口（`tests/test_tests_dir_shape.py` 要一个顶层 `def test_*`）。"""
    FAILED.clear()
    test_menu_two_phases()
    test_index_menu_is_thin()
    test_end_to_end_turn()
    test_prompt_follows_menu()
    test_no_privilege_growth_and_no_rewrite()
    test_route_round_only_routes()
    assert not FAILED, f"{len(FAILED)} 条未过：" + "；".join(FAILED)


if __name__ == "__main__":
    test_capability_arm()
    print("\n✅ 全部通过")
