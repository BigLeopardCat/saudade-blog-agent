# -*- coding: utf-8 -*-
"""react 臂的**决策事件**（资产对齐 J，20261020）。

## 为什么这条测试存在

本臂此前**一条决策事件都不记**——生产 `_decide_llm` 那条 `planner.native_decision`
（工作流 / 轮次 / 调用清单 / finish / 四个解码旋钮）在 react 侧没有对应物。具名的代价：

20261020 查 `task_auto` 残差（graph 64 条 vs react 15 条）时，**两臂的规划轮数与每轮
意图条数都读不出来**，只能拿"两臂恰好都记的那几条事件"当代理，绕了一大圈才把
"这是读数差不是行为差"钉住。轮数那一半后来由 `planner.llm_done` 补上（327af3e），
**意图条数这一半只有这条事件能给**。

## 判的是"接线与形状"，不是"模型点什么"

四件事：
① 有调用那一轮：`workflow`/`calls`/`finish` 与模型真发的对得上，四个解码旋钮在；
② **零工具轮也记**（生产的 `native_decision` 覆盖全部规划轮 ⇒ 两臂的分母才是同一件事）；
③ 反控：`calls` 记的是**拆分伪函数之前**的整份清单——只发 `task_intents` 那一轮
   与"点了技能 + 顺带交清单"那一轮，名字都必须在；
④ `intents` 是**显式的一格**（`0` 也记）：生产靠 `task_auto` 的 `listed` 补这一格，
   而 `task_auto` 只在清单非空时才记 ⇒ "没填"在生产是缺席，与"这一格不存在"分不开。

用法：SAUDADE_IGNORE_ENV_FILE=1 IOT_ENABLED=1 \\
        .venv/bin/python tests/test_react_decision_trace.py
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
from config.settings import settings  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc + (f"  [{detail}]" if detail else ""))


ADMIN = Principal(uid=1, role="admin")
_CFG = {"configurable": {"principal": ADMIN, "conversation_id": 7}}
_MSG = "帮我把那篇讲架构的文章读一下，顺便看看留言板"

_UM = {"input_tokens": 100, "output_tokens": 5, "total_tokens": 105,
       "input_token_details": {"cache_read": 80}}


class _Scripted(GenericFakeChatModel):
    """按脚本吐回复的假模型。

    ⚠️ **最后一个 chunk 要带上 `usage_metadata` 与 `response_metadata`**：在**图里**
    调用时这条链走**分片合并**那条路，合并只认 chunk 上带的元数据——脚本消息上写的那
    一份**合完就没了**（`finish` 那一格会静默变成空串，而"空串"正是"端点没回"的取值
    ⇒ 判据会被架空而不报错）。理由与实测见 `tests/test_react_llm_usage.py` 同一段注。
    """

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
                tool_calls=list(getattr(msg, "tool_calls", None) or []) if last else [],
                usage_metadata=(getattr(msg, "usage_metadata", None) if last else None),
                response_metadata=(getattr(msg, "response_metadata", None) if last else {}))
            if run_manager:
                run_manager.on_llm_new_token(tok, chunk=ChatGenerationChunk(message=chunk))
            yield ChatGenerationChunk(message=chunk)


def _call(name: str, *, content: str = "", finish: str = "tool_calls", **args) -> AIMessage:
    """一条点名技能的脚本回复（`finish` 原样进 `response_metadata`）。"""
    return AIMessage(content=content,
                     tool_calls=[{"name": name, "args": args, "id": f"call_{name}",
                                  "type": "tool_call"}],
                     response_metadata={"finish_reason": finish},
                     usage_metadata=_UM)


def _plain(content: str, finish: str = "stop") -> AIMessage:
    """零工具轮：有正文、一个函数都不点。"""
    return AIMessage(content=content, response_metadata={"finish_reason": finish},
                     usage_metadata=_UM)


def _run(plan_script: list) -> list:
    """跑一轮 → trace 事件列表 `[(node, event, kwargs), …]`。

    记账只在 `record` 那一处收口 ⇒ 替掉 `R.record` 就能把这一轮**真正落了什么账**
    一条不落地收下来（比事后读 trace 文件更接近判据，后者还要过落盘）。
    """
    events: list = []
    _orig = (R._llm, R._real_tools, R._narrator_llm, R._system_prompt, R.record)
    try:
        R._llm = lambda: _Scripted(messages=iter(plan_script), ai_message_chunk=iter([]))
        R._real_tools = lambda *a, **k: {}
        R._narrator_llm = lambda: _Scripted(
            messages=iter([AIMessage(content="好的喵", usage_metadata=_UM)] * 3),
            ai_message_chunk=iter([]))
        R._system_prompt = lambda state, ctx: "你是测试用助手。"
        R.record = lambda *a, **k: events.append(
            (a[0] if a else "", a[1] if len(a) > 1 else "", k))
        list(R.build().stream({"messages": [HumanMessage(content=_MSG)]}, _CFG))
        return events
    finally:
        (R._llm, R._real_tools, R._narrator_llm, R._system_prompt, R.record) = _orig


def _decisions(events: list) -> list:
    return [e for e in events if e[0] == "planner" and e[1] == "native_decision"]


_INTENT = {"goal": "看看留言板", "workflow": "content_query"}


# ── ① 有调用那一轮：三格与模型真发的对得上 + 四个解码旋钮在 ────────────────────
def test_normal_round() -> None:
    print("\n① 有调用那一轮：`workflow`/`calls`/`finish` 对得上，解码旋钮在")
    ev = _decisions(_run([_call("chat", reply="好", intents=[_INTENT])] * 3))
    check("记了 `planner.native_decision`", len(ev) >= 1, str(len(ev)))
    if not ev:
        return
    k = ev[0][2]
    check("`workflow` 取第一条真调用的技能名", k.get("workflow") == "chat",
          str(k.get("workflow")))
    check("`calls` 是模型原样发出来的函数名", k.get("calls") == "chat", str(k.get("calls")))
    check("`finish` 透传端点的值（不是空串）", k.get("finish") == "tool_calls",
          str(k.get("finish")))
    check("`round` 在", isinstance(k.get("round"), int), str(k.get("round")))
    for key, want in (("provider", settings.llm_provider),
                      ("model", str(getattr(settings, "active_llm_model", "") or "")),
                      ("temp", settings.planner_temperature),
                      ("seed", settings.llm_seed),
                      ("thinking", bool(settings.planner_native_thinking))):
        check(f"解码旋钮 `{key}` 与 settings 同源", k.get(key) == want,
              f"{k.get(key)!r} != {want!r}")


# ── ② 零工具轮也记（两臂的分母才是同一件事） ─────────────────────────────────
def test_zero_tool_round() -> None:
    print("\n② 零工具轮**也记一条**（生产的 `native_decision` 覆盖全部规划轮）")
    ev = _decisions(_run([_plain("我这就去办喵")] * 3))
    check("零工具轮落了 `native_decision`", len(ev) >= 1, str(len(ev)))
    if not ev:
        return
    k = ev[0][2]
    check("`workflow` = chat（生产那一格同款）", k.get("workflow") == "chat",
          str(k.get("workflow")))
    check("`calls` 回空串，**不回技能名**（「点了 chat」与「一个都没点」必须可分辨）",
          k.get("calls") == "", str(k.get("calls")))
    check("`intents` = 0（显式的一格，不是缺席）", k.get("intents") == 0,
          str(k.get("intents")))
    check("`finish` 透传（这一格是「截断」的唯一机器判据）", k.get("finish") == "stop",
          str(k.get("finish")))


# ── ③ 反控：`calls` 记的是**拆分伪函数之前**的整份清单 ────────────────────────
def test_calls_are_raw() -> None:
    print("\n③ 反控：`calls` 含伪函数名（记的是 `raw_tool_calls`，不是摘完剩下的 `rest`）")
    both = [_call("chat", reply="好", intents=[_INTENT])]
    both[0].tool_calls.insert(0, {"name": "task_intents", "args": {"intents": [_INTENT]},
                                  "id": "c0", "type": "tool_call"})
    ev = _decisions(_run(both * 3))
    check("记了事件", len(ev) >= 1, str(len(ev)))
    if not ev:
        return
    k = ev[0][2]
    check("`calls` 同时含 `task_intents` 与 `chat`（照 `rest` 记会只剩后者）",
          k.get("calls") == "task_intents,chat", str(k.get("calls")))
    check("`workflow` 仍取**第一条真调用**（伪函数不算技能）", k.get("workflow") == "chat",
          str(k.get("workflow")))


# ── ④ `intents` 是显式的一格 ────────────────────────────────────────────────
def test_intents_count() -> None:
    print("\n④ `intents` 条数：模型填了几条就记几条，不填记 0")
    two = [_INTENT, {"goal": "看看有没有新评论", "workflow": "content_query"}]
    ev = _decisions(_run([_call("chat", reply="好", intents=two)] * 3))
    check("两条意图 ⇒ `intents` = 2", bool(ev) and ev[0][2].get("intents") == 2,
          str(ev[0][2].get("intents") if ev else None))
    ev2 = _decisions(_run([_call("chat", reply="好")] * 3))
    check("没填 ⇒ `intents` = 0（**缺席 ≠ 0** 的另一面：这里要的是显式的 0）",
          bool(ev2) and ev2[0][2].get("intents") == 0,
          str(ev2[0][2].get("intents") if ev2 else None))


def main() -> int:
    test_normal_round()
    test_zero_tool_round()
    test_calls_are_raw()
    test_intents_count()

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
