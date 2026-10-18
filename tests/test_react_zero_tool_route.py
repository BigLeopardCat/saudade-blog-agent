# -*- coding: utf-8 -*-
"""零工具轮的出口路由（资产对齐 J，20261019）：去 narrator，**不是**去 execute。

## 为什么这条测试存在

`planner` 的出口路由判据只有一条（生产 `graph.route_after_planner`）：

    "execute" if plan["tools"] else "model"

本臂的四条出口里三条写对了，**最后那条硬写着 `"execute"`**（`8a82b83` 把第二条臂从
`create_agent` + 中间件换成真 StateGraph 时漏的：中间件那版"零工具轮就是收尾轮"是
**结构性**的，循环自己会停；拆成显式节点之后这条边要有人写出来）。

后果是一个**死循环**（实测，纯 `chat` 轮）：

    planner(零工具) → execute(空清单，什么都没做) → route_after_execute 见无受阻项
      → planner 再问一次模型 → 同样零工具 → …… 直到撞 MAX_PLAN_ROUNDS

一句"你好"要 **4 次决策调用 + 4 次 execute**，最后那次叙述发生在预算用完之后。
代价三重：钱（每轮多问 3 次）、时间（每轮多三次往返）、**读数**（模型被问同一句话
四遍，四遍的答案可以不同，而 golden 只看最后一遍）。

⚠️ **20261012 那次全量对照跑的是旧形状**（`create_agent` 那版），所以这个形状从来没
被比过——它正是"同一条臂在两次测量之间被换过骨架"的那种情形。

用法：SAUDADE_IGNORE_ENV_FILE=1 IOT_ENABLED=1 \\
        .venv/bin/python tests/test_react_zero_tool_route.py
"""
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

import agent.react_arm as R  # noqa: E402
from agent.principal import Principal  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc + (f"  [{detail}]" if detail else ""))


ADMIN = Principal(uid=1, role="admin")
_CFG = {"configurable": {"principal": ADMIN, "conversation_id": 7}}


class _Scripted(GenericFakeChatModel):
    """按脚本吐回复的假模型（与其余 react 套件同款，`_stream` 必须自己实现）。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

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


def _call(name: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[
        {"name": name, "args": args, "id": f"call_{name}", "type": "tool_call"}])


def _run(script: list, *, msg: str = "你好", patch: dict | None = None
         ) -> tuple[list, list]:
    """跑一轮 → `(帧, 每次决策的提示词 ctx)`。

    数"模型被问了几次"数的是 `_system_prompt`（**不能**数假模型的 `_generate`：
    narrator 用的是同一个类，它的调用会一起被数进去）。
    """
    sysctx: list = []
    _orig = (R._llm, R._real_tools, R._narrator_llm, R._system_prompt)
    _patched = {k: getattr(R, k) for k in (patch or {})}

    def _sysprompt(state, ctx):  # noqa: ANN001
        sysctx.append(dict(ctx))
        return "你是测试用助手。"

    try:
        R._llm = lambda: _Scripted(messages=iter(script), ai_message_chunk=iter([]))
        R._real_tools = lambda *a, **k: {}
        R._narrator_llm = lambda: _Scripted(
            messages=iter([AIMessage(content="嗯")] * 8), ai_message_chunk=iter([]))
        R._system_prompt = _sysprompt
        for k, v in (patch or {}).items():
            setattr(R, k, v)
        return list(R.build().stream({"messages": [HumanMessage(content=msg)]}, _CFG)), sysctx
    finally:
        (R._llm, R._real_tools, R._narrator_llm, R._system_prompt) = _orig
        for k, v in _patched.items():
            setattr(R, k, v)


def _steps(frames: list) -> list[str]:
    return [k for m, d in frames if m == "updates" for k in (d or {})]


# ── ① 纯闲聊轮：一次决策，然后直去 narrator ─────────────────────────────────
def test_chat_round() -> None:
    print("\n① 纯 `chat` 轮：**一次决策** ⇒ model（不是 execute ⇒ planner 打转）")
    frames, sysctx = _run([_call("chat", reply="你好呀")] * 4)
    check("模型只被问了**一次**（零工具的出口直去 narrator ⇒ 不再回头问一遍）",
          len(sysctx) == 1, str(len(sysctx)))
    check("帧序里**没有 execute**（计划零工具 ⇒ 没有东西可执行）",
          "execute" not in _steps(frames), str(_steps(frames)))
    check("叙述发生了（`model` 在帧序里）", "model" in _steps(frames), str(_steps(frames)))
    check("`plan_rounds` 只走了 1（撞预算那条路会走到 4）",
          max((u.get("plan_rounds") or 0) for m, d in frames if m == "updates"
              for u in [(d or {}).get("planner")] if u) == 1,
          str([(d or {}).get("planner", {}).get("plan_rounds")
               for m, d in frames if m == "updates" and (d or {}).get("planner")]))

    print("\n  负控：把判据换成恒真（老写法 `decided = \"execute\"`）⇒ 打转当场回来")
    frames2, sysctx2 = _run([_call("chat", reply="你好呀")] * 6,
                            patch={"plan_has_tools": lambda _p: True})
    check("**模型被问了不止一次**（这条反控证明上面那条不是恒绿）",
          len(sysctx2) > 1, str(len(sysctx2)))
    check("帧序里又出现 execute 了", "execute" in _steps(frames2), str(_steps(frames2)))


# ── ② 只有注记、没有工具的计划（导航到不存在的页面）也走同一条边 ─────────────
def test_note_only() -> None:
    print("\n② 零工具**有注记**的计划（导航到不存在的页面）同样直去 narrator")
    frames, sysctx = _run([_call("navigate", target="火星基地")] * 4,
                          msg="带我去火星基地")
    check("模型只被问了一次", len(sysctx) == 1, str(len(sysctx)))
    check("没有 execute", "execute" not in _steps(frames), str(_steps(frames)))
    last = next((d["planner"]["plan_obj"] for m, d in reversed(frames)
                 if m == "updates" and (d or {}).get("planner")), {})
    check("计划是零工具 + 带注记（判据的前提：它**确实**是零工具那一档）",
          not (last.get("tools") or []) and bool(str(last.get("note") or "")),
          f'{last.get("tools")} {str(last.get("note"))[:40]}')


# ── ③ 源码锁：四条出口写的是**同一条判据** ──────────────────────────────────
def test_source_lock() -> None:
    print("\n③ 源码锁：`_planner_decide` 的零工具出口**没有**硬写的 `execute`")
    src = (ROOT / "agent" / "react_arm.py").read_text(encoding="utf-8")
    body = src.split("def _planner_decide(", 1)[1].split("\ndef ", 1)[0]
    check("硬写的 `ctx[\"decided\"] = \"execute\"` **一处都没有**（老写法就是它）",
          'ctx["decided"] = "execute"\n' not in body,
          str(body.count('ctx["decided"] = "execute"\n')))
    check("四条出口都是 `plan_has_tools` 那个三元式（确认轮 / 快道 / 去重终局 / 出口）",
          body.count('ctx["decided"] = "execute" if plan_has_tools(_plan) else "model"') == 4,
          str(body.count('ctx["decided"] = "execute" if plan_has_tools(_plan) else "model"')))
    # 反控：判据本体在生产那一份里（本臂不许有第二份"有没有清单"的写法）。
    prod = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
    check("生产 `route_after_planner` 的判据仍是 `plan[\"tools\"]`（同源）",
          'return "execute" if plan["tools"] else "model"' in prod)


def main() -> int:
    test_chat_round()
    test_note_only()
    test_source_lock()

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
