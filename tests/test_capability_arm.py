# -*- coding: utf-8 -*-
"""能力索引臂（`agent/capability_arm.py`）的判据：离线、零网络零 LLM。

**为什么单起一套**：本臂是**唯一**"菜单按轮变"的臂，而它的失效方式全是静默的——
菜单没换（第二轮还绑着能力表 ⇒ 模型点不出工具）、换早了（写族那一轮也切走 ⇒ 多意图里
剩下的点不到）、提示词没跟着换（菜单是工具表、提示词还在讲技能表 ⇒ 自相矛盾的提示词）。
这三种都不让别的套件红，而读数会掉得像是"模型变笨了"。

判据分五组：

  ① **两态菜单**：`ctx["caps"]` 空 ⇒ 索引态（名字集合 == 可见技能 + 三个记账口）；
     非空 ⇒ 工具态（名字集合 == 那条能力的工具面 ∩ authz 可见 + 三个记账口）。
  ② **只换 `description`**：索引态 schema 剥掉描述后与 `build_tool_schema(role)` 剥掉后
     **逐字节相同**（函数名 / 参数 / `required` / `enum` 一个字符不动）。
  ③ **端到端跑一轮**（`test_arm_seams` 那套脚本化假模型）：第 0 轮点能力 ⇒ 零工具、
     `plan["workflow"]` 是能力名；第 1 轮绑的是那条能力的工具面；工具真被调、帧发出来。
  ④ **提示词成对换**：索引态 == `react_arm._system_prompt` 那一份；工具态含能力简报的
     三格（工具面 / 完成判定 / 回复契约）。
  ⑤ **不扩权 + 不重写 harness**：工具态菜单 ⊆ authz 可见工具；够不着的 `caps` 名被丢掉；
     源码里不出现权限闸/同意闸/引用解析的第二份实现（只许调 `run_specs`）。
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
from agent import workflow_defs as W  # noqa: E402
from agent.capability_index import capabilities_for  # noqa: E402
from agent.principal import Principal  # noqa: E402
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


# ── ② 只换 description ────────────────────────────────────────────────────
def test_index_menu_only_swaps_description():
    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k != "description"}
        if isinstance(node, list):
            return [strip(x) for x in node]
        return node

    for role in (None, "user", "admin"):
        from agent.planner_llm import build_tool_schema
        prod = {t["function"]["name"]: strip(t["function"]) for t in build_tool_schema(role)}
        for tool in CA.capability_menu(role, {}):
            schema = tool.args_schema if isinstance(tool.args_schema, dict) else {}
            got = strip({"name": tool.name, "parameters": schema})
            check(f"索引态只换描述、结构一字节不动：{tool.name}（role={role}）",
                  tool.name in prod and got == strip(prod[tool.name]),
                  f"{str(got)[:60]} vs {str(prod.get(tool.name))[:60]}")

    # 反控：索引态的描述**确实被换过**（否则上面那条判的是"没换"）
    from agent.capability_index import capability_of, index_entry
    nav = [t for t in CA.capability_menu("admin", {}) if t.name == "navigate"][0]
    want = index_entry(capability_of("navigate", "admin"))
    check("反控：索引态描述 == `index_entry`（不是原散文）", nav.description == want,
          nav.description[:60])


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


def test_capability_arm():
    """收集器入口（`tests/test_tests_dir_shape.py` 要一个顶层 `def test_*`）。"""
    FAILED.clear()
    test_menu_two_phases()
    test_index_menu_only_swaps_description()
    test_end_to_end_turn()
    test_prompt_follows_menu()
    test_no_privilege_growth_and_no_rewrite()
    assert not FAILED, f"{len(FAILED)} 条未过：" + "；".join(FAILED)


if __name__ == "__main__":
    test_capability_arm()
    print("\n✅ 全部通过")
