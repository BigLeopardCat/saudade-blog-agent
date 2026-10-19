# -*- coding: utf-8 -*-
"""react 臂的 LLM 用量记账（资产对齐 L，20261019）。

## 为什么这条测试存在

用量只在**调用点**取得到（`usage_fields(resp)` 读的是响应对象本身），而本臂的调用点
是**另一处代码** ⇒ 生产那四处 `llm_done` 一行都盖不到这里。全量实测（两轮六跑交替
A/B，20261019）：

    graph 每跑记到约 508 次调用、1110 万输入 tok、前缀缓存命中 87.6–96.5%
    react 每跑只记到 **1** 次调用（那是 `execute` 里 `_create_display_text` 自己记的
    ——它是生产函数、记账在它内部）

而成本与**前缀缓存命中率**全靠这一格 ⇒ 两臂对照里"成本"那半边此前一直**读不出来**。
`eval/token_cost_report.py` 打印的是"1 次调用"，不是"没有成本"——缺席 ≠ 0，这条纪律
在 `agent/llm_usage.py` 自己的字段契约里就写着。

## 判的是"接线"，不是"数对不对"

数由 `tests/test_llm_usage.py` 管（`usage_fields` 的形状识别）。这里只判四件事：
① 决策那一路落了账、且 `engine` 与生产同一档（聚合表按行相加的前提）；
② 叙述那一路落的节点名与事件名与生产 `model_node` 同源，并带上 `prompt_chars`；
③ **反控**：响应不带用量 ⇒ 事件照落、但**没有**用量键（不许补 0）；
④ 两条补救路径（空内容重试 / 回了 tool_calls 的纠正）也各记一次——那两次是真花了
   钱的调用，漏记等于把这一族的成本少算一截。

用法：SAUDADE_IGNORE_ENV_FILE=1 IOT_ENABLED=1 \\
        .venv/bin/python tests/test_react_llm_usage.py
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
_MSG = "站内一共有几篇文章"

_IN, _CACHE = 1200, 900


def _um(inp: int = _IN, cache: int = _CACHE) -> dict:
    return {"input_tokens": inp, "output_tokens": 20, "total_tokens": inp + 20,
            "input_token_details": {"cache_read": cache}}


class _Scripted(GenericFakeChatModel):
    """按脚本吐回复的假模型；`_stream` 必须自己实现（基类会丢掉 tool_calls）。

    ⚠️ **最后一个 chunk 必须带上 `usage_metadata`**（20261019 实测）：在**图里**调用
    时这条链走的是**分片合并**那条路（`_generate` 的产物被拆成 chunk 再合回来），
    而合并只认 chunk 上带的用量——脚本消息上写的那一份**合完就没了**。症状很坑：
    记账接线明明是对的（`_record_llm` 在调用点、`usage_fields` 也认得出形状），
    读出来却是"这次没量到"。**图外**直接 `invoke` 走 `_generate`，看不出这个差别。
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
                usage_metadata=(getattr(msg, "usage_metadata", None) if last else None))
            if run_manager:
                run_manager.on_llm_new_token(tok, chunk=ChatGenerationChunk(message=chunk))
            yield ChatGenerationChunk(message=chunk)


def _call(name: str, *, no_usage: bool = False, **args) -> AIMessage:
    """一条点名技能的脚本回复。`no_usage=True` = **这一次端点没回用量**（反控用）。

    注意 `AIMessage` **不接受** `usage_metadata={}`（三个必填格校验不过）——"没量到"
    在这一层就是**整个键不传**，这正是 `usage_fields` 那条"缺席 ≠ 0"要保护的形状。
    """
    kw = {} if no_usage else {"usage_metadata": _um()}
    return AIMessage(content="", tool_calls=[
        {"name": name, "args": args, "id": f"call_{name}", "type": "tool_call"}], **kw)


def _run(plan_script: list, narr_script: list) -> list:
    """跑一轮 → trace 事件列表 `[(node, event, kwargs), …]`。

    记账只在 `record` 那一处收口，所以这里替掉 `R.record` 就能把**这一轮真正落了
    什么账**一条不落地收下来——比事后去读 trace 文件更接近判据（后者还要过落盘）。
    """
    events: list = []
    _orig = (R._llm, R._real_tools, R._narrator_llm, R._system_prompt, R.record)
    try:
        R._llm = lambda: _Scripted(messages=iter(plan_script), ai_message_chunk=iter([]))
        R._real_tools = lambda *a, **k: {}
        R._narrator_llm = lambda: _Scripted(messages=iter(narr_script),
                                            ai_message_chunk=iter([]))
        R._system_prompt = lambda state, ctx: "你是测试用助手。"
        R.record = lambda *a, **k: events.append((a[0] if a else "", a[1] if len(a) > 1 else "", k))
        list(R.build().stream({"messages": [HumanMessage(content=_MSG)]}, _CFG))
        return events
    finally:
        (R._llm, R._real_tools, R._narrator_llm, R._system_prompt, R.record) = _orig


def _llm_done(events: list, node: str) -> list:
    return [e for e in events if e[0] == node and e[1] == "llm_done"]


# ── ① 决策那一路：用量三格 + `engine=native`（与生产同一档） ─────────────────
def test_planner_accounting() -> None:
    print("\n① 决策调用落账：`input`/`output`/`cache_read` 三格，`engine=native`")
    ev = _llm_done(_run([_call("chat", reply="好几篇呢")] * 3,
                        [AIMessage(content="好几篇呢", usage_metadata=_um())] * 3),
                   "planner")
    check("决策那一路落了 `llm_done`", len(ev) >= 1, str(len(ev)))
    if not ev:
        return
    k = ev[0][2]
    check("用量三格都在（`usage_fields` 抠出来的就是这三格）",
          k.get("input") == _IN and k.get("output") == 20 and k.get("cache_read") == _CACHE,
          str({x: k.get(x) for x in ("input", "output", "cache_read")}))
    check("`engine=native`——与生产 `planner.llm_done` 同一档（聚合表按行相加的前提）",
          k.get("engine") == "native", str(k.get("engine")))
    check("`duration_s` 也在（生产那条事件一直带着它）", "duration_s" in k, str(sorted(k)))


# ── ② 叙述那一路：节点名/事件名与生产 `model_node` 同源 + `prompt_chars` ──────
def test_narrator_accounting() -> None:
    print("\n② 叙述调用落账：节点 `model`、带 `prompt_chars`")
    ev = _llm_done(_run([_call("chat", reply="好几篇呢")] * 3,
                        [AIMessage(content="站里一共有 20 篇喵", usage_metadata=_um())] * 3),
                   "model")
    check("叙述那一路落了 `llm_done`", len(ev) >= 1, str(len(ev)))
    if not ev:
        return
    k = ev[0][2]
    check("用量三格都在", k.get("input") == _IN and k.get("cache_read") == _CACHE, str(k))
    check("`prompt_chars` 在（生产 `model.llm_done` 用它校准字符/token 比）",
          isinstance(k.get("prompt_chars"), int) and k["prompt_chars"] > 0,
          str(k.get("prompt_chars")))


# ── ③ 反控：响应不带用量 ⇒ 事件照落，但**没有**用量键（不许补 0） ────────────
def test_missing_usage_is_not_zero() -> None:
    print("\n③ 反控：响应不带用量 ⇒ 事件照落、用量键**缺席**（缺席 ≠ 0）")
    ev = _llm_done(_run([_call("chat", no_usage=True, reply="好几篇呢")] * 3,
                        [AIMessage(content="好几篇呢")] * 3), "planner")
    check("事件照落（记账不依赖端点回没回用量）", len(ev) >= 1, str(len(ev)))
    if not ev:
        return
    k = ev[0][2]
    check("三个用量键**一个都不在**（补 0 会把『读不懂』记成『token 数是 0』）",
          not any(x in k for x in ("input", "output", "cache_read")), str(sorted(k)))
    check("而 `engine`/`duration_s` 照旧在（那两格与端点无关）",
          k.get("engine") == "native" and "duration_s" in k, str(sorted(k)))


# ── ④ 两条补救路径各记一次（那两次也是真花了钱的调用） ───────────────────────
def test_remedy_paths() -> None:
    print("\n④ 空内容重试 / tool_calls 纠正：两条补救路径各落一条用量事件")
    # 空内容 ⇒ 重试一次。第二次给正文。
    ev = _run([_call("chat", reply="好几篇呢")] * 3,
              [AIMessage(content="", usage_metadata=_um()),
               AIMessage(content="站里一共有 20 篇喵", usage_metadata=_um(77, 66))])
    retry = [e for e in ev if e[0] == "model" and e[1] == "llm_empty_retry"]
    # 次数不判"恰好一次"：这一轮会因闸门打回而重跑叙述，每次空内容都会重试一次
    # ——判的是"这条补救路径**记了账**"，不是"它只发生一次"。
    check("落了 `model.llm_empty_retry`（事件名与生产同源）", len(retry) >= 1, str(len(retry)))
    check("每一次重试都带用量（第二次调用同样是钱）",
          bool(retry) and all(e[2].get("input") == 77 for e in retry),
          str([e[2] for e in retry]))
    # 回了 tool_calls ⇒ 带纠正重问一次。
    ev2 = _run([_call("chat", reply="好几篇呢")] * 3,
               [AIMessage(content="开场白", tool_calls=[
                   {"name": "chat", "args": {}, "id": "c9", "type": "tool_call"}],
                   usage_metadata=_um()),
                AIMessage(content="站里一共有 20 篇喵", usage_metadata=_um(55, 44))])
    corr = [e for e in ev2 if e[0] == "model" and e[1] == "llm_toolcall_correct_failed"]
    check("落了 `model.llm_toolcall_correct_failed`", len(corr) >= 1, str(len(corr)))
    check("每一次纠正都带用量", bool(corr) and all(e[2].get("input") == 55 for e in corr),
          str([e[2] for e in corr]))


def main() -> int:
    test_planner_accounting()
    test_narrator_accounting()
    test_missing_usage_is_not_zero()
    test_remedy_paths()

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
