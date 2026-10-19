# -*- coding: utf-8 -*-
"""`plan_obj["params"]` 那一格（资产对齐 K，20261019）：少了它整轮崩成空回复。

## 为什么这条测试存在

生产把 `plan_obj["params"]` 当**恒在场**的一格用：`_decide_instantiate` 在
`instantiate_plan` 之后紧接着补上它，`_finalize_dedupe` 的字面路径修正则**直接下标**它。
而 `instantiate_plan` 自己的产物里**没有**这个键（它返回 workflow/tools/note/reply/
chat/status 那一套）——生产那一路是**额外补的**，补的那一行才是承重的。

本臂的 `_plan_of` 少了那一行。后果不是"少一个字段"那么轻：`_finalize_dedupe` 那条
字面路径修正的前两项是「技能是 navigate」＋「主人原话里带 /路径」，**第三项才去读
参数**——所以只要命中前两项就抛 `KeyError`，异常从 planner 节点穿出去、整轮打成
`__ERROR__`、主人收到空回复。全量实测（20261019 三遍交替 A/B）：`nav_nonexistent`
react **3/3 空回复**（`error: 'params'`），graph 3/3 绿——而它在慢性红榜上**一次都没上过**，
不是方差。

生产 20260922 在收尾路径上踩过同一个坑，治法写在 `_decide_write_guard` 那处 refusal
为什么**必须 return 而不是 break**：break 到收尾路径必抛同一个 `KeyError`。

用法：SAUDADE_IGNORE_ENV_FILE=1 IOT_ENABLED=1 \\
        .venv/bin/python tests/test_react_plan_params.py
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
import agent.react_parity as P  # noqa: E402
from agent.principal import Principal  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc + (f"  [{detail}]" if detail else ""))


ADMIN = Principal(uid=1, role="admin")
_CFG = {"configurable": {"principal": ADMIN, "conversation_id": 7}}

# 这一句是那条用例的原话（`nav_nonexistent`）：**含字面路径**，正是修正那一支的入口。
_MSG = "帮我去 /iot 这个页面"


def _call(name: str, **args) -> dict:
    return {"name": name, "args": args, "id": f"call_{name}", "type": "tool_call"}


def _dedupe(plan_obj: dict) -> dict:
    """拿一份计划走生产 `_finalize_dedupe`（本臂的接线），返回定稿那一份。"""
    out, _ended = P.finalize_dedupe({}, ctx={"user_msg": _MSG}, plan_obj=plan_obj,
                                    rounds=0, has_frames=False,
                                    turn_messages=[HumanMessage(content=_MSG)])
    return out


# ── ① 单调用：产物带 `params`，且逐字就是那一条调用的参数 ────────────────────
def test_single_call() -> None:
    print("\n① `_plan_of` 单调用产物带 `params`（生产 `_decide_instantiate` 那一行）")
    plan = R._plan_of([_call("navigate", target="/iot")], "admin")
    check("那一格在场", "params" in plan, str(sorted(plan)))
    check("取值逐字＝那一条调用的参数（不是编的、不是空的）",
          plan.get("params") == {"target": "/iot"}, str(plan.get("params")))
    check("前两项也成立（判据的前提：技能是 navigate、原话带 /路径）",
          plan.get("workflow") == "navigate" and bool(re.search(r"/[A-Za-z0-9_\-./]+", _MSG)),
          f'{plan.get("workflow")}')


# ── ② 行为：拿它走 `_finalize_dedupe` ⇒ 不抛，修正那一支走通 ─────────────────
def test_dedupe_no_crash() -> None:
    print("\n② 拿 ① 的产物走 `_finalize_dedupe` ⇒ **不抛异常**，且如实收在零工具上")
    plan = R._plan_of([_call("navigate", target="/iot")], "admin")
    try:
        out = _dedupe(plan)
    except KeyError as e:
        check("不抛 KeyError", False, repr(e))
        return
    check("不抛 KeyError", True)
    check("定稿后仍是 navigate 且**零工具**（白名单外的路径由 `instantiate_plan` 预校验拦下）",
          out.get("workflow") == "navigate" and not (out.get("tools") or []),
          f'{out.get("workflow")} {out.get("tools")}')
    note = str(out.get("note") or "")
    check("注记说清了「没有这个页面」（narrator 照它如实说，不编）",
          "不存在" in note or "没有" in note, note[:80])
    check("那一格在定稿后仍带着真值（修正那一支要拿它比对字面路径）",
          out.get("params") == {"target": "/iot"}, str(out.get("params")))


# ── ③ 反控：删掉那一格 ⇒ 同一个调用当场抛 ────────────────────────────────────
def test_control() -> None:
    print("\n③ 反控：把 `params` 删掉 ⇒ ② 的那条断言当场红（这一格是承重的）")
    plan = R._plan_of([_call("navigate", target="/iot")], "admin")
    plan.pop("params")
    try:
        _dedupe(plan)
        check("抛 KeyError", False, "没抛")
    except KeyError as e:
        check("抛 KeyError（取值就是那一格的名字）", "params" in str(e), repr(e))
    # 反控的反面：把技能换成非 navigate ⇒ 同一个缺键**不**抛（说明入口是"技能+字面路径"
    # 那两项，不是"缺键就必炸"——判据的射程写清楚，免得日后照这个反控去改别处）
    plan2 = R._plan_of([_call("chat", reply="你好")], "admin")
    plan2.pop("params", None)
    try:
        _dedupe(plan2)
        check("非 navigate 的缺键不抛（入口是那两项，不是缺键本身）", True)
    except KeyError as e:
        check("非 navigate 的缺键不抛（入口是那两项，不是缺键本身）", False, repr(e))


# ── ④ 多调用：没有"这一份计划的参数"可言 ⇒ 空表，不拿第一条顶替 ──────────────
def test_multi_call() -> None:
    print("\n④ 多调用（本臂的容差，生产恒一条）⇒ `params` 是**空表**")
    plan = R._plan_of([_call("navigate", target="/"), _call("effect", effect="snow")],
                      "admin")
    check("`workflow` 是 `a|b` 形状（判据的前提）",
          plan.get("workflow") == "navigate|effect", str(plan.get("workflow")))
    check("`params` 是空表（**不**拿第一条的参数顶替——宁可「没有」显式，也不编一个像真的）",
          plan.get("params") == {}, str(plan.get("params")))


# ── ⑤ 端到端：脚本化模型吐 navigate(/iot) 跑一轮 ⇒ 整轮不崩 ─────────────────
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


def test_end_to_end() -> None:
    print("\n⑤ 端到端：假模型点名 navigate(/iot) 跑一轮 ⇒ **不抛异常**、零工具收场")
    script = [AIMessage(content="", tool_calls=[
        {"name": "navigate", "args": {"target": "/iot"}, "id": "c1", "type": "tool_call"}])]
    _orig = (R._llm, R._real_tools, R._narrator_llm, R._system_prompt)
    try:
        R._llm = lambda: _Scripted(messages=iter(script), ai_message_chunk=iter([]))
        R._real_tools = lambda *a, **k: {}
        R._narrator_llm = lambda: _Scripted(
            messages=iter([AIMessage(content="站内没有这个页面喵")] * 8),
            ai_message_chunk=iter([]))
        R._system_prompt = lambda state, ctx: "你是测试用助手。"
        try:
            frames = list(R.build().stream({"messages": [HumanMessage(content=_MSG)]}, _CFG))
            ok = True
        except Exception as e:  # noqa: BLE001 —— 崩了就是这条测试要抓的东西
            frames, ok = [], e
    finally:
        (R._llm, R._real_tools, R._narrator_llm, R._system_prompt) = _orig
    check("整轮跑完没抛（崩溃那一版在这里就断了）", ok is True, "" if ok is True else repr(ok))
    if ok is not True:
        return
    last = next((d["planner"]["plan_obj"] for m, d in reversed(frames)
                 if m == "updates" and (d or {}).get("planner")), {})
    check("定稿计划是 navigate + 零工具（没去执行那条不存在的路径）",
          last.get("workflow") == "navigate" and not (last.get("tools") or []),
          f'{last.get("workflow")} {last.get("tools")}')
    steps = [k for m, d in frames if m == "updates" for k in (d or {})]
    check("帧序里**没有 execute**（零工具的出口直去 narrator）",
          "execute" not in steps, str(steps))


def main() -> int:
    test_single_call()
    test_dedupe_no_crash()
    test_control()
    test_multi_call()
    test_end_to_end()

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
