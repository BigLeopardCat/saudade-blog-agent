# -*- coding: utf-8 -*-
"""任务台账登记（资产对齐 G，20261019）：三个伪函数 + 意图清单 → `__TASK__` 帧。

## 为什么这条测试存在

三个任务伪函数（`task_hold` / `task_drop` / `task_intents`）由 `build_tool_schema`
**挂在每一个技能上、对所有角色可见**（20261015 撤档，见 `bind_planner_tools` 的注）
⇒ 模型**发得出来**。而 react 臂的 `_plan_of` 拿技能名去 `instantiate_plan`，那三个
名字**不在 `WORKFLOW_MAP` 里** ⇒ `WORKFLOW_MAP.get(name) or WORKFLOW_MAP["chat"]`
静默回退成 `chat`、零工具、零注记、零 trace。离线直调实测（本文件 ① 的负控）：

    task_hold -> chat [] ''      task_drop -> chat [] ''      task_intents -> chat [] ''

形状是"模型说「这件事我记下了 / 这件事不做了」，系统把它读成闲聊"。**没有任何东西会红**
——`require_task_state` 那一族在本臂上从来没被满足过，报告里只是一条稳定的红。

同族的第二个出口是**挂在每个技能上的 `intents` 字段**：本臂此前也没摘 ⇒ 模型枚举的
清单被当成技能参数送进 `instantiate_plan`，换来一条 `param_unknown` 告警 + 清单一个
字节都不剩。

## 钉住的契约（这里红 = 某条接线掉了）

  · **三个伪函数从调用里摘出来**（`planner_llm._split_pseudo_calls`，生产同一个）；
  · **显式登记 ⇒ `__TASK__` 帧**（`_decide_register` → `frame_payload`）；
  · **意图清单 ⇒ 自动登记帧**（`_auto_task_frames`，经壳的 sink 并进返回）；
  · **三格非技能参数摘走**（`intents` / `reply_kind` / `evidence`），且**与生产同源**
    ——机械对差（`tool_calls_to_plan` 的源码里 `args.pop(` 哪几个名字，这里就得摘哪几个）；
  · **点了菜单外的名字 ⇒ 响亮**（确定性收尾 + `native_fallback` 事件），**不是**静默 chat；
  · **只登记轮只纠偏一次**（`_CORRECTED_LATCH` 那一格：本臂的"纠过没有"住在
    `ctx["corrected"]` 布尔里，不译成生产读的那一格 ⇒ 每轮都纠、而死循环）。

用法：SAUDADE_IGNORE_ENV_FILE=1 IOT_ENABLED=1 \\
        .venv/bin/python tests/test_react_task_ledger.py
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    AIMessageChunk,
    HumanMessage,
)
from langchain_core.outputs import ChatGenerationChunk  # noqa: E402

import agent.graph as G  # noqa: E402
import agent.react_arm as R  # noqa: E402
import agent.react_parity as P  # noqa: E402
from agent.principal import Principal  # noqa: E402
from agent.workflow_plan import instantiate_plan  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc + (f"  [{detail}]" if detail else ""))


ADMIN = Principal(uid=1, role="admin")

# 一次运行一个会话 id：**登记帧的幂等键里含着会话**，取不到就不落库（见
# `_decide_register` 那条 `task_declare_noconv`）。所以这条测试必须给会话 id，
# 否则 ② 那一节判的是"帧没发"而看起来像"接线掉了"。
_CONV = 7
_CFG = {"configurable": {"principal": ADMIN, "conversation_id": _CONV}}

_GOAL = "把草稿「小猫咪」的标签补齐"
_STEPS = [{"tool": "get_article_detail", "label": "读那篇草稿"}]


class _Scripted(GenericFakeChatModel):
    """按脚本吐回复的假模型（与其余 react 套件同款）。

    `_stream` **必须自己实现**：基类只读 `content`、按空白切片，`tool_calls` 整个丢掉
    ⇒ 得到"模型一个工具都不调"的假现场（本文件要的恰恰是"点了伪函数"）。
    """

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    def _generate(self, messages, *a, **k):  # noqa: ANN001, ANN003
        if SINK is not None:
            SINK.append(list(messages))
        return super()._generate(messages, *a, **k)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        msg = self._generate(messages, stop=stop, run_manager=run_manager,
                             **kwargs).generations[0].message
        text = str(msg.content or "")
        toks = [t for t in re.split(r"(\s)", text) if t] or [""]
        for i, tok in enumerate(toks):
            last = i == len(toks) - 1
            chunk = AIMessageChunk(
                content=tok, id=getattr(msg, "id", None),
                tool_calls=list(getattr(msg, "tool_calls", None) or []) if last else [])
            if run_manager:
                run_manager.on_llm_new_token(tok, chunk=ChatGenerationChunk(message=chunk))
            yield ChatGenerationChunk(message=chunk)


SINK: list | None = None
EVENTS: list | None = None


def _call(name: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[
        {"name": name, "args": args, "id": f"call_{name}", "type": "tool_call"}])


def _tc(msg: AIMessage) -> dict:
    """`AIMessage` 里那一条原始 `tool_calls`（`resp.tool_calls` 的形状：**dict**）。"""
    return dict(msg.tool_calls[0])


def _hold(goal: str = _GOAL, **extra) -> AIMessage:
    return _call("task_hold", goal=goal, steps=list(_STEPS), **extra)


def _run(script: list, *, msg: str = "帮我把小猫咪那篇的标签补齐") -> tuple[list, list, list]:
    """跑一轮 → `(帧, trace 事件, 每次决策的提示词 ctx)`。

    ⚠️ **`record` 要同时打在两处**：本臂自己的 `record` 绑在 `react_arm` 上，而
    `_decide_register` / `_auto_task_frames` 用的是 **`agent.graph` 上那个**（它们是
    生产那一侧的函数，`from utils.trace import record` 各自绑了一份）。只打一处会让
    "生产那几条事件没落"看起来像"接线掉了"——而它只是没被拦到。

    ⚠️ **数"模型被问了几次"要数 `_system_prompt`，不能数 `_Scripted._generate`**：
    narrator 用的是同一个 `_Scripted` 类 ⇒ 它的调用也会进 SINK，"被问 3 次"里混着
    narrator 那一次（本文件 ⑥ 第一版就是这么写错的）。
    """
    global SINK, EVENTS
    SINK, EVENTS = [], []
    _sink, _ev = SINK, EVENTS
    sysctx: list = []
    _orig = (R._llm, R._real_tools, R._narrator_llm, R.record, R._system_prompt,
             G.record)

    def _record(*a, **k):  # noqa: ANN002, ANN003
        _ev.append(a)

    def _sysprompt(state, ctx):  # noqa: ANN001
        sysctx.append(dict(ctx))
        return "你是测试用助手。"

    try:
        R._llm = lambda: _Scripted(messages=iter(script), ai_message_chunk=iter([]))
        R._real_tools = lambda *a, **k: {}
        R._narrator_llm = lambda: _Scripted(
            messages=iter([AIMessage(content="嗯")] * 4), ai_message_chunk=iter([]))
        R.record = _record
        R._system_prompt = _sysprompt
        G.record = _record
        frames = list(R.build().stream({"messages": [HumanMessage(content=msg)]}, _CFG))
        return frames, _ev, sysctx
    finally:
        (R._llm, R._real_tools, R._narrator_llm, R.record, R._system_prompt,
         G.record) = _orig


def _plans(frames: list) -> list[dict]:
    return [(d or {}).get("planner") for m, d in frames
            if m == "updates" and (d or {}).get("planner")]


def _frames_of(frames: list) -> list[dict]:
    """整轮里所有 `task_frames` 载荷（producer 就是逐帧发的，见 `server.py`）。"""
    return [f for m, d in frames if m == "updates"
            for u in [(d or {}).get("planner")]
            for f in ((u or {}).get("task_frames") or []) if isinstance(f, dict)]


def _events(ev: list, node: str, event: str) -> list:
    return [a for a in ev if a[:2] == (node, event)]


# ── ① 三个伪函数都被摘出来（负控：老行为是静默 chat） ────────────────────────
def test_split() -> None:
    print("\n① 三个伪函数**从调用里摘出来**：不再静默降级成零工具 chat")
    rest, declare, intents, notes = P.split_pseudo_calls(
        [_tc(_call("task_hold", goal=_GOAL, steps=list(_STEPS))),
         _tc(_call("navigate", target="首页")),
         _tc(_call("task_intents", intents=[{"goal": "看看留言板", "workflow": "chat"}]))],
        "admin")
    check("`rest` 里只剩真技能（伪函数不在里面 ⇒ `_plan_of` 不会拿它们去实例化）",
          [c["name"] for c in rest] == ["navigate"], str([c["name"] for c in rest]))
    check("登记归一出来了（`normalize_declaration`，生产同一个）",
          isinstance(declare, dict) and declare.get("goal") == _GOAL, str(declare))
    check("清单归一失败记一笔（`task_intents_invalid`——`chat` 不在可见技能里）",
          notes == ["task_intents_invalid"], str(notes))

    print("\n  负控：**把拆分摘掉**（老行为）⇒ 三条都落回零帧静默 chat")
    for name, args in (("task_hold", {"goal": _GOAL, "steps": list(_STEPS)}),
                       ("task_drop", {"goal": _GOAL}),
                       ("task_intents", {"intents": [{"goal": "看留言板",
                                                      "workflow": "guestbook_read"}]})):
        plan = instantiate_plan(name, dict(args), "admin")
        check(f"  老行为：`{name}` ⇒ 静默回退成 chat、零工具、零注记",
              plan.get("workflow") == "chat" and not plan.get("tools")
              and not str(plan.get("note") or ""),
              f'{plan.get("workflow")} {plan.get("tools")} {plan.get("note")!r}')


# ── ② 显式登记 ⇒ `__TASK__` 帧 ──────────────────────────────────────────────
def test_explicit() -> None:
    print("\n② 显式登记（`task_hold`）⇒ 收尾计划 + 一帧 `__TASK__`（不再当闲聊）")
    frames, ev, sysctx = _run([_hold(pending_question="你要补哪个标签？")] * 3)
    check("trace 落 `planner.task_declare`（生产同款事件名）",
          len(_events(ev, "planner", "task_declare")) == 1,
          str([a[:2] for a in ev if a[0] == "planner"]))
    got = _frames_of(frames)
    check("发了一帧，且带 `task_id`（`producer` 只发带 task_id 的帧）",
          len(got) == 1 and str(got[0].get("task_id") or "").startswith("at_"),
          str(got))
    check("帧的 `state` 是 `input_required`（有待问的问题）",
          got and got[0].get("state") == "input_required", str(got and got[0]))
    last = (_plans(frames)[-1].get("plan_obj") or {})
    check("计划是**收尾计划**、零工具（不是把伪函数名当技能去执行）",
          last.get("workflow") == "chat" and not (last.get("tools") or []),
          f'{last.get("workflow")} {last.get("tools")}')
    check("注记写明「只做了登记」（主人读到的是「记下了」而不是「办好了」）",
          "登记" in str(last.get("note") or ""), str(last.get("note"))[:60])
    after = [k for m, d in frames[
        max(i for i, (m, d) in enumerate(frames)
            if m == "updates" and (d or {}).get("planner")) + 1:]
        if m == "updates" for k in (d or {})]
    check("收尾那一份计划**之后没有 execute**（登记轮不执行任何东西）",
          "execute" not in after, str(after))

    print("\n  撤下（`task_drop`）走同一条链，帧的 state 是 `cancelled`")
    frames2, _, _ = _run([_call("task_drop", goal=_GOAL)] * 3, msg="小猫咪那篇不弄了")
    got2 = _frames_of(frames2)
    check("撤下也发帧、`state=cancelled`（不是「什么都没发生」）",
          len(got2) == 1 and got2[0].get("state") == "cancelled", str(got2))


# ── ③ 意图清单 ⇒ 自动登记（经壳的 sink） ────────────────────────────────────
def test_auto() -> None:
    print("\n③ 意图清单 ⇒ 自动登记帧（`_auto_task_frames`，经薄壳的 sink 并进返回）")
    frames, ev, sysctx = _run([_call("task_intents",
                             intents=[{"goal": "把「下雨」特效关掉", "workflow": "effect"}])] * 3,
                      msg="先帮我把下雨关掉，再看看留言板")
    # 数 ==1：这一轮零工具 ⇒ 决策一次、然后直去 narrator（出口路由那一格，
    # `tests/test_react_zero_tool_route.py` 钉着）。数成 ≥1 会让"模型被问三遍、
    # 同一个 goal 发三帧"看起来也通过。
    check("trace 落 `planner.task_auto`（生产同款事件名，由 `_auto_task_frames` 自己记）",
          len(_events(ev, "planner", "task_auto")) == 1,
          str([a[:2] for a in ev if a[0] == "planner"]))
    got = _frames_of(frames)
    check("自动登记那几帧**经壳并进了 `task_frames`**（写进 sink 而不并 ⇒ 一帧都不发）",
          bool(got) and any("下雨" in str(f.get("goal") or "") for f in got), str(got))
    check("`task_frames` 出现在**planner 那一格**（producer 就是从那里读的）",
          any("task_frames" in (u or {}) for u in _plans(frames)),
          str([sorted(u) for u in _plans(frames)]))


# ── ④ 三格非技能参数摘走，且与生产**机械同源** ──────────────────────────────
def test_injected_args() -> None:
    print("\n④ 三格非技能参数（`intents` / `reply_kind` / `evidence`）摘走")
    rest, intents = P.strip_injected_args(
        [{"name": "effect", "args": {"effect": "rain", "action": "off",
                                     "reply_kind": "pos", "evidence": "主人说的",
                                     "intents": [{"goal": "把通知都标成已读",
                                                  "workflow": "notice_read"}]}}],
        "admin")
    check("三格都不在 `args` 里了（留着会跟着 `params` 走到 `PARAMS=` 与参数校验）",
          set(rest[0]["args"]) == {"effect", "action"}, str(sorted(rest[0]["args"])))
    check("技能自己的参数**一个都没动**",
          rest[0]["args"] == {"effect": "rain", "action": "off"}, str(rest[0]["args"]))
    check("清单从**字段那一路**收下来了（与 `task_intents` 调用那一路合并，按原序）",
          intents == ({"goal": "把通知都标成已读", "workflow": "notice_read"},), str(intents))
    # 字段那一路与调用那一路**合并**（生产 `intents = tuple(intents) + tuple(inline)`）：
    # 少合一边的形状是"模型在技能调用上枚举的那几件一件都不登记"，而两路的清单都非空时
    # 才看得出来（只有一路时合并与覆盖长得一样）。
    _both, _merged = P.strip_injected_args(
        [{"name": "effect", "args": {"effect": "rain",
                                     "intents": [{"goal": "把通知都标成已读",
                                                  "workflow": "notice_read"}]}}],
        "admin", ({"goal": "把「下雨」关掉", "workflow": "effect"},))
    check("两路都在 ⇒ **合并**（调用那一路在前，按原序），不是覆盖",
          [i["workflow"] for i in _merged] == ["effect", "notice_read"],
          str([i["workflow"] for i in _merged]))

    print("\n  机械对差：生产 `tool_calls_to_plan` 摘哪几个名字，这里就得摘哪几个")
    # 判据是**源码里 `args.pop(` 的那些名字**，不是某个字符串常量：生产将来加第四格
    # 注入参数（`args.pop(NEW_ARG, None)`），这里没跟上 ⇒ 当场红。这是本文件里唯一
    # 一条"两份实现会不会分家"的机械闸。
    from agent.planner_llm import tool_calls_to_plan
    _pop = re.compile(r"args\.pop\((\w+)")
    prod = set(_pop.findall(ast.get_source_segment(
        (ROOT / "agent" / "planner_llm.py").read_text(encoding="utf-8"),
        next(n for n in ast.walk(ast.parse(
            (ROOT / "agent" / "planner_llm.py").read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef)
            and n.name == tool_calls_to_plan.__name__)) or ""))
    arm = set(_pop.findall((ROOT / "agent" / "react_parity.py").read_text(encoding="utf-8")))
    check("生产那一侧确实摘了东西（前提守卫：这条闸不是空的）", bool(prod), str(prod))
    check("**两边的名字集合逐字相同**（生产加一格而这里没跟 ⇒ 这里红）",
          prod == arm, f"生产={sorted(prod)} 本臂={sorted(arm)}")


# ── ⑤ 点了菜单外的名字 ⇒ 响亮（不是静默 chat） ──────────────────────────────
def test_unknown_loud() -> None:
    print("\n⑤ 点了菜单里没有的函数名 ⇒ 确定性收尾 + `native_fallback`（不静默降级）")
    frames, ev, sysctx = _run([_call("nonexistent_skill", x=1)] * 3)
    check("trace 落 `planner.native_fallback`，`disposition=unknown_tool`（生产同款事件）",
          [a[:2] for a in _events(ev, "planner", "native_fallback")] == [
              ("planner", "native_fallback")],
          str([a[:2] for a in ev if a[0] == "planner"]))
    last = (_plans(frames)[-1].get("plan_obj") or {})
    check("收尾计划带**如实的那句话**（主人读得到「为什么没做」）",
          "菜单里没有的函数名" in str(last.get("note") or ""), str(last.get("note"))[:70])
    check("**零工具**（不拿一个来路不明的名字去执行）", not (last.get("tools") or []),
          str(last.get("tools")))


# ── ⑥ 只登记轮：纠偏一次，第二次不再纠（不死循环） ──────────────────────────
def test_correct_once() -> None:
    print("\n⑥ 只登记、零工具零问题 ⇒ 纠偏**一次**；第二次仍这样 ⇒ 认它（不死循环）")
    # 无 `pending_question` + 有步骤 ⇒ `_decide_register` 走纠偏那一支。
    frames, ev, sysctx = _run([_hold()] * 6)
    check("`planner.task_correct` **恰好一次**（生产读 `c.correction` 那一格；"
          "本臂的「纠过没有」住在 `ctx[\"corrected\"]` 里，不译 ⇒ 每轮都纠、返回 `{}` "
          "不吃轮数 ⇒ 死循环）",
          len(_events(ev, "planner", "task_correct")) == 1,
          str(len(_events(ev, "planner", "task_correct"))))
    check("决策模型被问了**两次**（第一次 + 纠偏后的一次），不是被打转到撞预算",
          len(sysctx) == 2, str(len(sysctx)))
    last = (_plans(frames)[-1].get("plan_obj") or {})
    check("第二次认它 ⇒ 收尾计划（不再空转）",
          last.get("workflow") == "chat" and not (last.get("tools") or []),
          f'{last.get("workflow")} {last.get("tools")}')


def test_latch_control() -> None:
    """⑥b 反控：把「纠过没有」那一格**译回去**（老写法：直接透传 `ctx["correction"]`）
    ⇒ 第二次登记仍然被纠 ⇒ 上面那条 `== 1` 当场红。

    这条反控判的是 `register` 里那句哨兵**真的在起作用**：本臂的"纠过没有"住在
    `ctx["corrected"]` 布尔里，而 `_decide_register` 读的是 `c.correction` 的真假
    ——不译就恒读到空串、每轮都纠，而纠偏出口返回 `{}`（不吃轮数）⇒ **死循环**。
    """
    print("\n⑥b 反控：不译那一格 ⇒ 第二次仍然纠（证明哨兵不是装饰）")
    st = {"receipts": []}
    dec = P.task_decision({"goal": _GOAL, "steps": list(_STEPS),
                           "pending_question": "", "state": "running"}, (), "chat")

    def _act(**over):  # noqa: ANN003
        ctx = {"role": "admin", "user_msg": "帮我记一下", "page_ctx": "",
               "principal": ADMIN, "config": _CFG, "corrected": True,
               "correction": "", **over}
        return P.register(st, ctx=ctx, plan_obj={"workflow": "chat", "tools": []},
                          rounds=1, has_frames=False, decided=dec)[1]

    check("纠过 ⇒ `next`/`return`（**不再纠**：哨兵被 `_decide_register` 读成真）",
          _act() != "continue", _act())
    check("**没纠过** ⇒ `continue`（反控成立：这条判据两边都会动，不是恒绿）",
          _act(corrected=False) == "continue", _act(corrected=False))
    check("老写法（把布尔丢掉、只透传可能已清空的 `correction`）⇒ **仍然 `continue`**"
          "（正是它会让每一轮都纠一次）",
          _act(corrected=False, correction="") == "continue")


def main() -> int:
    test_split()
    test_explicit()
    test_auto()
    test_injected_args()
    test_unknown_loud()
    test_correct_once()
    test_latch_control()

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
