#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`react_arm.make_ctx` 四个接缝的接线单测：离线、秒级、零网络零 LLM。

**被锁的缺陷**：这四个接缝（`prompt_builder` / `menu_builder` / `plan_builder` /
`executor_factory`）是"工具臂复用 `react` 那张真图"的**唯一**接口面。它们的失效方式
全都**不报错**，而且指向同一类后果——**一份看着像工具臂、其实在跑技能臂的假读数**：

 ① **接缝加了没接上**：`ctx.get("…")` 写错键名、或忘了在某个节点里读 ⇒ 那一格静默走
    缺省。报告里看起来是"工具菜单没效果"，而真相是那一格压根没生效；
 ② **缺省档被改坏**：`react` 臂自己变了一个人——它是全仓唯一还在跑的历史基线，基线悄悄
    换了循环，此后的所有 A/B 都拿两份不同的东西在比。

判据分三段：① 缺省档零行为变更（含**逐字节**的帧对比）② 四条接缝各自真的被读
③ 台账（键集 / 签名 / 源码里那一处 `ctx.get`）。

⚠️ 这里的绿只说明**接线在**。接缝接上之后行为对不对，只有真链路 A/B 答得了
（同 `tests/test_tool_menu.py` 末尾那条纪律）。
"""
import inspect
import itertools
import json
import re
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import ChatGenerationChunk  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

import agent.react_arm as R  # noqa: E402
from agent.principal import Principal  # noqa: E402
from agent.react_line import WorkflowExecutor  # noqa: E402
from agent.tools_arm import ToolsGoldenArm  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


# ── 替身 ─────────────────────────────────────────────────────────────
SEEN: list[list] = []      # 决策模型收到的消息表（`prompt_builder` 的判据读它）
BOUND: list[list] = []     # 绑给决策模型的工具名（`menu_builder` 的判据读它）


class _Res(str):
    """工具返回的最小克隆：`str` 子类 + `kind`/`meta`（同 `tools.base.ToolResult`）。"""

    def __new__(cls, text: str, kind: str = "ok", meta: dict | None = None):
        o = super().__new__(cls, text)
        o.kind = kind
        o.meta = meta or {}
        return o


class _Scripted(GenericFakeChatModel):
    """按脚本吐消息的假模型——**必须自己实现 `_stream`**（`tool_calls` 的保真度）。

    `GenericFakeChatModel._stream` 只读 `message.content`、把 `tool_calls` 整个丢掉；
    而 langgraph 每个节点都挂 `StreamMessagesHandler` ⇒ `_should_stream` 为真 ⇒ 走
    `_stream`。照抄基类就得到"模型一个工具都不调"的假现场（那不是臂的缺陷，是替身与
    真实之间的一处保真度缺口）。切法与真流一致：正文按空白切，`tool_calls` 挂在最后
    一个分片上。与 `tests/test_react_arm.py::_Scripted` 逐字同源。
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
RAN: list = []


def _nav_tool(**kw):
    RAN.append(kw)
    return _Res(f"页面已跳转：{kw.get('path')}",
                meta={"cmd": {**NAV_CMD, "url": kw.get("path")}})


def _call(workflow: str, **args) -> AIMessage:
    """一次决策轮（第一个形参不叫 `name`：写技能的参数恰好就叫 `name`，撞名会 TypeError）。"""
    return AIMessage(content="让我先看看", tool_calls=[
        {"name": workflow, "args": args, "id": f"call_{workflow}", "type": "tool_call"}])


def _arm(hooks: dict | None):
    """这一轮的臂对象：**只有 `ctx_hooks` 一格不同**，`stream` 一个字不覆写。

    这样造现场，本身就是一条判据：`ctx_hooks` 是类属性、`stream` 读 `self.ctx_hooks`
    ——若哪天有人把它挪进 `__init__` 或改成覆写 `stream`，下面每一条都会红。
    """
    class _Arm(R.ReactGoldenArm):
        ctx_hooks = dict(hooks or {})

    return _Arm(R._compiled())


def _run(script: list, tools: dict | None = None, *, hooks: dict | None = None,
         principal=None, msg: str = "带我去首页") -> list:
    """跑一次 `arm.stream`，收下全部 `(mode, data)` 二元组。

    `R._system_prompt` 换成一句桩：缺省档下它是提示词的**唯一**来源 ⇒ ②b 拿它当
    "缺省走的是它"的阳性对照（`prompt_builder` 在场时它必须**一次都不被调**）。
    """
    SEEN.clear()
    BOUND.clear()
    R._llm = lambda: _Scripted(messages=iter(script), ai_message_chunk=iter([]))
    R._narrator_llm = lambda: GenericFakeChatModel(
        messages=itertools.repeat(AIMessage(content="好了")))
    R._real_tools = lambda *a, **k: dict(tools or {"navigate_to": _nav_tool})
    R._system_prompt = lambda *a, **k: "你是测试用助手。"
    cfg = {"configurable": {"principal": principal}} if principal else {}
    return list(_arm(hooks).stream({"messages": [HumanMessage(content=msg)]}, cfg))


# 逐字节对比里**必须剔掉**的三样：`ts`（回执行的时间戳）、消息对象的 `id`（uuid）、
# `tool_call_id`。它们是**进程内随机量**，留着会让"等价"变成一条随机红的判据——而随机
# 红的判据比没有判据更坏（会被人当噪声关掉）。
_DROP_KEYS = {"ts", "id", "tool_call_id"}


def _scrub(o):
    """剔随机量：字典丢 `_DROP_KEYS`，消息对象降成「类型 + 正文 + name」。"""
    if isinstance(o, BaseMessage):
        return ["msg", type(o).__name__, str(o.content or ""),
                str(getattr(o, "name", "") or "")]
    if isinstance(o, dict):
        return {k: _scrub(v) for k, v in sorted(o.items()) if k not in _DROP_KEYS}
    if isinstance(o, (list, tuple)):
        return [_scrub(v) for v in o]
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return str(o)


def _proj(frames: list) -> list:
    """帧序列的**逐字节投影**（缺省档对比用）。

    `updates` 那一路的载荷全是可序列化的纯数据（计划文本、回执行、帧 meta），过一遍
    `_scrub` 之后才是稳定的；`messages` 那一路只取节点名 + 类型名 + 正文 + `tool_calls`。
    """
    out: list = []
    for mode, data in frames:
        if mode != "messages":
            for node, payload in (data or {}).items():
                out.append([mode, node, json.dumps(_scrub(payload), sort_keys=True,
                                                   default=str, ensure_ascii=False)])
            continue
        chunk, meta = data
        out.append([mode, str((meta or {}).get("langgraph_node") or ""),
                    type(chunk).__name__, str(getattr(chunk, "content", "") or ""),
                    json.dumps(_scrub(getattr(chunk, "tool_calls", None) or []),
                               sort_keys=True, default=str, ensure_ascii=False)])
    return out


def _ups(frames: list) -> list:
    return [d for m, d in frames if m == "updates"]


def _planner_plan(frames: list) -> dict:
    return next((u["planner"].get("plan_obj") or {} for u in _ups(frames)
                 if "planner" in u and u["planner"].get("plan_obj")), {})


ADMIN = Principal(uid=9, role="admin")
# 一条能走完 planner → execute → model → gate 的脚本（缺省档对比要的是一条**有内容**的
# 轨迹：零工具的轨迹会让"提示词/菜单/计划"三格都不参与，对比就成了空过）。
_SCRIPT = [_call("navigate", target="首页"), AIMessage(content="带你到首页了")]



def test_arm_seams():
    FAILED.clear()
    # ── ① 缺省档零行为变更 ───────────────────────────────────────────────
    print("\n① 缺省档 —— 四个接缝缺省 `None` ⇒ 与改动之前逐字节相同")
    check("`ReactGoldenArm.ctx_hooks` 是**空字典**（基类不许自带接缝）",
          R.ReactGoldenArm.ctx_hooks == {}, str(R.ReactGoldenArm.ctx_hooks))

    _ctx0 = R.make_ctx(config=None, principal=ADMIN, role="admin", user_msg="带我去首页",
                       state={"messages": []}, real_tools={})
    check("缺省建出来的执行器**逐字就是 `WorkflowExecutor`**（不是它的某个子类）",
          type(_ctx0["executor"]) is WorkflowExecutor, type(_ctx0["executor"]).__name__)
    check("四个接缝在账里都在场且都是 `None`（在场 = 三处 `ctx.get` 读的是同一批键）",
          all(_ctx0.get(k) is None for k in ("prompt_builder", "menu_builder",
                                             "plan_builder", "executor_factory")),
          str([k for k in ("prompt_builder", "menu_builder", "plan_builder",
                           "executor_factory") if _ctx0.get(k) is not None]))

    # **逐字节**：一次走缺省基类，一次走"四个接缝显式传 None"的子类。后者是工具臂那条路径
    # 的形状（子类恒有 `ctx_hooks`，只是这一轮四格都是 None）⇒ 两条必须产出同一串帧。
    _f_default = _run(_SCRIPT, principal=ADMIN)
    _f_explicit_none = _run(_SCRIPT, principal=ADMIN,
                            hooks={"prompt_builder": None, "menu_builder": None,
                                   "plan_builder": None, "executor_factory": None})
    _p_default, _p_none = _proj(_f_default), _proj(_f_explicit_none)
    check("**帧序列逐字节相同**（缺省基类 vs 四格显式 None 的子类）",
          _p_default == _p_none,
          f"{len(_p_default)} 帧 vs {len(_p_none)} 帧"
          + ("" if _p_default == _p_none else
             "；首处不同 = " + str(next((a, b) for a, b in zip(_p_default, _p_none) if a != b))))
    check("这条轨迹**非空过**：真的走完了 planner + execute 两个节点",
          bool(_planner_plan(_f_default))
          and any("execute" in u for u in _ups(_f_default)),
          f"planner={bool(_planner_plan(_f_default))} "
          f"execute={any('execute' in u for u in _ups(_f_default))}")


    # ── ② 四条接缝各自真的被读 ───────────────────────────────────────────
    print("\n② 四条接缝 —— 每一条都要证明'传进去真的被读'，不是被 `**kwargs` 吞掉")

    # ②a `menu_builder`：绑给模型的工具名 == 桩返回的那一个
    _menu_seen: list = []
    _SENTINEL_TOOL = StructuredTool(
        name="SEAM_ONLY_TOOL", description="接缝哨兵", args_schema={"type": "object",
                                                                  "properties": {}},
        func=lambda **kw: "哨兵")


    def _menu(role):
        _menu_seen.append(role)
        return [_SENTINEL_TOOL]


    _run(_SCRIPT, principal=ADMIN, hooks={"menu_builder": _menu})
    check("`menu_builder` 被调用且收到角色", _menu_seen == ["admin"], str(_menu_seen))
    check("绑给模型的菜单**就是桩返回的那一个**（缺省档下这是技能菜单，绝不可能是它）",
          BOUND and BOUND[0] == ["SEAM_ONLY_TOOL"], str(BOUND[:2]))
    _run(_SCRIPT, principal=ADMIN)
    check("阳性对照：缺省档绑的是技能菜单（名字集合里没有那个哨兵）",
          bool(BOUND) and "SEAM_ONLY_TOOL" not in BOUND[0], str(BOUND[0][:4]))

    # ②b `prompt_builder`：模型收到的 SystemMessage == 桩的正文
    _prompt_seen: list = []


    def _prompt(state, ctx):
        _prompt_seen.append(ctx.get("role"))
        return "SEAM-PROMPT-ONLY"


    def _sys_prompts() -> list[str]:
        return [str(m.content) for msgs in SEEN for m in msgs if isinstance(m, SystemMessage)]


    _run(_SCRIPT, principal=ADMIN, hooks={"prompt_builder": _prompt})
    check("`prompt_builder` 被调用且收到账（`ctx['role']`）",
          _prompt_seen == ["admin"], str(_prompt_seen))
    check("模型收到的系统提示**就是桩那一句**", "SEAM-PROMPT-ONLY" in _sys_prompts(),
          str(_sys_prompts()[:1])[:120])
    _run(_SCRIPT, principal=ADMIN)
    check("阳性对照：缺省档收到的是 `_system_prompt` 那句桩（不是哨兵）",
          not any("SEAM-PROMPT-ONLY" in s for s in _sys_prompts())
          and "你是测试用助手。" in _sys_prompts(), str(_sys_prompts()[:1])[:120])

    # ②c `plan_builder`：它**收到模型的原始 tool_calls**，返回值进 `planner` 那一格
    _plan_seen: list = []


    def _plan(calls, role):
        _plan_seen.append(([str((c or {}).get("name") or "") for c in calls or []], role))
        return {"workflow": "chat", "tools": [], "calls": [], "note": "SEAM-PLAN",
                "reply": "", "status": ""}


    _f_plan = _run(_SCRIPT, principal=ADMIN, hooks={"plan_builder": _plan})
    check("`plan_builder` 收到**模型的原始调用名**与角色",
          _plan_seen == [(["navigate"], "admin")], str(_plan_seen))
    check("返回值真的进了 `planner` 那一格（缺省档这里是 `navigate`，不是 `chat`）",
          _planner_plan(_f_plan).get("workflow") == "chat",
          str(_planner_plan(_f_plan).get("workflow")))
    check("阳性对照：缺省档的 `workflow` 是技能实例化出来的 `navigate`",
          _planner_plan(_f_default).get("workflow") == "navigate",
          str(_planner_plan(_f_default).get("workflow")))

    # ②d `executor_factory`：**逐字**就是那个工厂的返回值，且五格参数一个不少
    _got: list = []


    class _SeamExecutor(WorkflowExecutor):
        pass


    def _factory(ledger, *, role, principal, user_msg, state, real_tools):
        _got.append({"ledger": ledger, "role": role, "principal": principal,
                     "user_msg": user_msg, "state": state, "real_tools": real_tools})
        return _SeamExecutor(real_tools, ledger, role=role, principal=principal,
                             user_msg=user_msg, state=state)


    _ctx1 = R.make_ctx(config=None, principal=ADMIN, role="admin", user_msg="带我去首页",
                       state={"messages": []}, real_tools={"navigate_to": _nav_tool},
                       executor_factory=_factory)
    check("`executor_factory` 的返回值就是账里那个执行器（`type(...) is` 哨兵类）",
          type(_ctx1["executor"]) is _SeamExecutor, type(_ctx1["executor"]).__name__)
    check("五格参数**逐个显式传**（不是让调用方闭包捕获）",
          len(_got) == 1 and _got[0]["role"] == "admin" and _got[0]["principal"] is ADMIN
          and _got[0]["user_msg"] == "带我去首页"
          and _got[0]["ledger"] is _ctx1["ledger"]
          and _got[0]["real_tools"] == {"navigate_to": _nav_tool},
          str(sorted(_got[0]) if _got else "没被调用"))
    check("工厂收到的那本账**就是**账里那本（另建一份 ⇒ 调用方手上的账永远是空的）",
          _got and _got[0]["ledger"] is _ctx1["ledger"])


    # ── ③ 台账：加接缝必须显式改这里 ─────────────────────────────────────
    print("\n③ 台账 —— 键集、签名、源码里那一处 `ctx.get`")
    SEAMS = ("prompt_builder", "menu_builder", "plan_builder", "executor_factory")
    _params = set(inspect.signature(R.make_ctx).parameters)
    _arm_src = (ROOT / "agent" / "react_arm.py").read_text(encoding="utf-8")

    check("工具臂的 `ctx_hooks` 键集 == 那四个（加接缝必须显式改这条台账）",
          set(ToolsGoldenArm.ctx_hooks) == set(SEAMS), str(sorted(ToolsGoldenArm.ctx_hooks)))
    check("每个键都是 `make_ctx` 的具名形参（拼错键 ⇒ `**hooks` 只在运行时 TypeError）",
          set(ToolsGoldenArm.ctx_hooks) <= _params,
          str(sorted(set(ToolsGoldenArm.ctx_hooks) - _params)))
    check("三个决策侧接缝各有一处 `ctx.get(\"…\")`（少一处 ⇒ 那一格静默走缺省）",
          all(f'ctx.get("{k}")' in _arm_src for k in SEAMS[:3]),
          str([k for k in SEAMS[:3] if f'ctx.get("{k}")' not in _arm_src]))
    check("`executor_factory` 在 `make_ctx` 里被读（它是建账那一行的分支条件）",
          "if executor_factory else" in _arm_src)
    check("`stream` 把 `self.ctx_hooks` 摊进 `make_ctx`（不摊 ⇒ 子类的接缝一个都不生效）",
          "**self.ctx_hooks" in _arm_src)

    assert not FAILED, f"{len(FAILED)} 条未过：" + "；".join(FAILED)


if __name__ == "__main__":
    test_arm_seams()
