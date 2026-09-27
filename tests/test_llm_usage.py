# -*- coding: utf-8 -*-
"""LLM 用量记账（`agent/llm_usage.py` + 四个落点）单测：离线、秒级、零网络零 LLM。

**为什么单独一套**：这个仓反复吃过"能力有测试 ≠ **接线**有测试"的亏（探针跑绿、
线上那条路没接上）。用量提取本身是纯函数、好测；真正会出事的缝在**四个调用点有没有
把字段交出去**——planner（text/native 两档）、narrator、屏幕文案、复盘。任何一处
漏接，trace 里就少一格，而"前缀缓存到底有没有命中"这个判决恰恰依赖它。

钉住的契约（这里红 = 某条判据被改掉了，改动要同步改这里）：

  · 认两种形状：langchain 归一后的 `usage_metadata` 与网关原始 `token_usage`；
  · `cache_read` **缺席 = 量不到**，不是 0——端点没回这个字段时不许补 0
    （同 `eval/dial_matrix.py` 里 fallback 记 None 不记 0 的纪律）；
  · 取不到用量 → 空 dict，且调用方**照常跑完**（记账绝不许反噬主路）；
  · 四个落点各自的 `llm_done` 事件里真的带着 input/output/cache_read
    （planner / narrator / 屏幕文案 / 复盘；`moderator.py` 与 `summarizer.py` 两条
    侧任务**没有 trace 上下文**，本批不接——见 `agent/llm_usage.py` 末尾的缺口说明）。

⚠️ planner 那几个用例的句子必须**不命中快道**（导航/文章/特效切换/屏幕显示会零 LLM
直接出计划），否则假 LLM 一次都没被调用、断言"绿得毫无意义"——每个用例因此都断言了
假 LLM 确实被调用过。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.llm_usage import usage_fields  # noqa: E402
from agent.principal import Principal  # noqa: E402
from config import settings  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


_MSG = "帮我把樱花打开"          # 不命中任何快道（同 test_planner_engine 的那句）
_CFG = {"configurable": {"principal": Principal(uid=7, role="admin"),
                         "user_id": 7, "conversation_id": 42, "stop_event": None}}
# 端点实测的形状（20260927 探针：13 tok 的短提问也带 input_token_details）
_REAL = {"input_tokens": 25200, "output_tokens": 96, "total_tokens": 25296,
         "input_token_details": {"cache_read": 17588}, "output_token_details": {}}


class _Resp:
    """鸭子类型的响应：`usage_fields` 认的是**属性**不是类型（提取纯函数、永不抛）。

    用真 `AIMessage` 装不下几种要测的形状——它把 `usage_metadata` 校验成
    `UsageMetadata`（`total_tokens` 必填、值必须 int），而这些恰恰是真实网关
    可能回的脏形状（字符串数字、bool、缺字段）。
    """

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeLLM:
    """`invoke` 按脚本返回，并把构造参数记下来（预算断言用）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.kw: dict = {}
        self.prompts: list[str] = []
        self.bound: dict | None = None

    def bind_tools(self, tools, **kw):
        self.bound = {"tools": tools, **kw}
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if not self.replies:
            raise AssertionError("假 LLM 的脚本用完了（本轮调用了不止一次）")
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def _events(rec, node: str, name: str) -> list[dict]:
    return [e for e in rec.events if e.get("node") == node and e.get("event") == name]


def _new_trace(tid: str):
    return trace_mod.start_trace(tid, 7, "th", {}, dir=tempfile.mkdtemp(), by_day=False)


# ── ① 纯函数：两种形状、缺席不补 0、炸不了 ─────────────────────────────────
def test_shape_langchain_and_raw():
    print("\n[提取] 两种形状都认")
    got = usage_fields(AIMessage(content="x", usage_metadata=dict(_REAL)))
    check("langchain 形状：input/output/cache_read 三个数",
          got == {"input": 25200, "output": 96, "cache_read": 17588}, str(got))
    raw = AIMessage(content="x", response_metadata={"token_usage": {
        "prompt_tokens": 100, "completion_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 64}}})
    check("网关原始 token_usage 也认（老版本 langchain 只填这个）",
          usage_fields(raw) == {"input": 100, "output": 7, "cache_read": 64},
          str(usage_fields(raw)))
    # 字符串数字（有些网关回 "123"）要归一成 int，否则聚合时会做字符串相加
    check("字符串数字也归一成 int",
          usage_fields(_Resp(usage_metadata={
              "input_tokens": "12", "output_tokens": "3"})) == {"input": 12, "output": 3})


def test_cache_read_absent_is_not_zero():
    print("\n[提取] cache_read 缺席 = 量不到，**不补 0**")
    got = usage_fields(_Resp(usage_metadata={
        "input_tokens": 900, "output_tokens": 5}))
    check("端点没回缓存字段 → 键不出现（不是 cache_read=0）",
          got == {"input": 900, "output": 5} and "cache_read" not in got, str(got))
    got0 = usage_fields(_Resp(usage_metadata={
        "input_tokens": 900, "output_tokens": 5,
        "input_token_details": {"cache_read": 0}}))
    check("端点回了 0 → 键在，值为 0（「说了没命中」与「量不到」必须分得开）",
          got0.get("cache_read") == 0 and "cache_read" in got0, str(got0))


def test_no_usage_returns_empty_and_never_raises():
    print("\n[提取] 取不到就回空 dict，且绝不许抛")
    for resp, why in ((AIMessage(content="x"), "完全没有用量"),
                      (None, "响应是 None"),
                      (object(), "鸭子类型对象"),
                      (_Resp(usage_metadata={}), "空 dict"),
                      (_Resp(usage_metadata="nonsense"), "形状不对"),
                      (_Resp(usage_metadata={"input_tokens": "abc"}), "值不是数字")):
        check(f"{why} → {{}}", usage_fields(resp) == {}, str(usage_fields(resp)))
    # bool 是 int 的子类：True 当 token 数会让聚合静默算错，必须不认——而且**不写**
    # 这一格（写成 0 等于把"读不懂"记成"用了 0 个 token"）
    check("bool 不当作数字（整格不写，不是写 0）",
          usage_fields(_Resp(usage_metadata={
              "input_tokens": True, "output_tokens": 1})) == {"output": 1})


# ── ② 接线：planner（text / native 两档）──────────────────────────────────
def _patch_llm(reply):
    """按 max_tokens 区分两只 LLM（文本档 400 / native 档取 settings 的值）。

    用预算而不是调用序区分，是因为影子档会构造两只、顺序不可依赖。
    """
    made: dict[str, _FakeLLM] = {}

    def _fake_get_llm(**kw):
        llm = _FakeLLM([reply])
        llm.kw = kw
        made["text" if kw.get("max_tokens") == 400 else "native"] = llm
        return llm

    return _fake_get_llm, made


_TEXT_REPLY = ('SKILL=effect\nPARAMS={"effect": "sakura", "action": "on"}\n'
               "TOOLS: toggle_effect\nNOTE: （无）\nREPLY: 已经打开樱花啦")


def _run_planner(engine: str, reply: AIMessage):
    rec = _new_trace(f"t_usage_{engine}")
    fake, made = _patch_llm(reply)
    old_engine, old_get = settings.planner_engine, G.get_llm
    settings.planner_engine, G.get_llm = engine, fake
    try:
        G.planner_node({"messages": [HumanMessage(content=_MSG)], "plan_rounds": 0,
                        "executed": [], "tool_data": []}, _CFG)
    finally:
        settings.planner_engine, G.get_llm = old_engine, old_get
    return rec, made


def test_planner_records_usage_both_engines():
    print("\n[接线] planner：两档的 llm_done 都带用量")
    for engine, reply in (
            ("text", AIMessage(content=_TEXT_REPLY, usage_metadata=dict(_REAL))),
            ("native", AIMessage(content="", usage_metadata=dict(_REAL), tool_calls=[
                {"name": "effect", "args": {"effect": "sakura", "action": "on"},
                 "id": "c1", "type": "tool_call"}]))):
        rec, made = _run_planner(engine, reply)
        check(f"[{engine}] 假 LLM 确实被调用过（没被快道截走）",
              sum(len(m.prompts) for m in made.values()) == 1, str(sorted(made)))
        ev = _events(rec, "planner", "llm_done")
        check(f"[{engine}] llm_done 带 input/output/cache_read",
              len(ev) == 1 and ev[0].get("input") == 25200
              and ev[0].get("output") == 96 and ev[0].get("cache_read") == 17588,
              str(ev))
        check(f"[{engine}] 原有字段没被挤掉（duration_s/engine/frames_chars）",
              ev and ev[0].get("engine") == engine
              and isinstance(ev[0].get("duration_s"), float)
              and "frames_chars" in ev[0], str(sorted(ev[0])) if ev else "无事件")


# ── ③ 接线：narrator / 屏幕文案 / 复盘 ────────────────────────────────────
def _patch_one(reply):
    made: dict = {}

    def _fake_get_llm(**kw):
        llm = _FakeLLM([reply])
        llm.kw = kw
        made["llm"] = llm
        return llm

    return _fake_get_llm, made


def test_narrator_records_usage():
    print("\n[接线] narrator（model 节点）")
    rec = _new_trace("t_usage_narrator")
    fake, made = _patch_one(AIMessage(content="好呀～", usage_metadata=dict(_REAL)))
    old_get, G.get_llm = G.get_llm, fake
    try:
        G.model_node({"messages": [HumanMessage(content=_MSG)],
                      "plan": "SKILL=effect\nPARAMS={}\nTOOLS:\nNOTE: （无）\nREPLY: （无）",
                      "receipts": []}, _CFG)
    finally:
        G.get_llm = old_get
    check("narrator 的 LLM 被调用过", len(made["llm"].prompts) == 1)
    ev = _events(rec, "model", "llm_done")
    check("model.llm_done 带 input/output/cache_read",
          len(ev) == 1 and ev[0].get("input") == 25200
          and ev[0].get("cache_read") == 17588, str(ev))


def test_display_text_records_usage():
    print("\n[接线] 屏幕文案创作（execute 内）")
    rec = _new_trace("t_usage_display")
    fake, made = _patch_one(AIMessage(content="主人来看我啦喵", usage_metadata=dict(_REAL)))
    old_get, G.get_llm = G.get_llm, fake
    try:
        out = G._create_display_text("小猫咪在屏幕上写句话", "页面：首页")
    finally:
        G.get_llm = old_get
    check("文案照常产出（记账不许反噬主路）", out == "主人来看我啦喵", out)
    ev = _events(rec, "execute", "llm_done")
    check("这条调用**有** llm_done 了（此前只有成功后的 display_create）",
          len(ev) == 1 and ev[0].get("input") == 25200 and ev[0].get("output") == 96,
          str(ev))
    check("display_create 事件仍在（值照旧）",
          [e.get("text") for e in _events(rec, "execute", "display_create")]
          == ["主人来看我啦喵"])


def test_display_text_failure_path_still_quiet():
    print("\n[接线] 屏幕文案：创作失败 → 兜底文案，且不炸")
    class _Boom:
        def invoke(self, prompt):
            raise RuntimeError("boom")
    old_get, G.get_llm = G.get_llm, lambda **kw: _Boom()
    try:
        out = G._create_display_text("x", "y")
    finally:
        G.get_llm = old_get
    check("失败仍是兜底文案（行为不变）", out.startswith("主人来看我啦"), out)


def test_reflector_records_usage():
    print("\n[接线] reflector（复盘）")
    rec = _new_trace("t_usage_reflector")
    fake, made = _patch_one(AIMessage(content="ISSUE: 缺 id\nDECIDE: wrap_up",
                                      usage_metadata=dict(_REAL)))
    old_get, G.get_llm = G.get_llm, fake
    try:
        G.reflector_node({"messages": [HumanMessage(content=_MSG)], "plan": "SKILL=x",
                          "blocked": [{"spec": "get_article_detail({})",
                                       "reason": "empty_result", "result": "（空）"}],
                          "reflect_rounds": 0}, _CFG)
    finally:
        G.get_llm = old_get
    check("复盘 LLM 被调用过", len(made["llm"].prompts) == 1)
    ev = _events(rec, "reflector", "llm_done")
    check("reflector.llm_done 带 input/output/cache_read",
          len(ev) == 1 and ev[0].get("input") == 25200
          and ev[0].get("cache_read") == 17588, str(ev))
    check("verdict 事件仍在（decide 照旧解析出来）",
          [e.get("decide") for e in _events(rec, "reflector", "verdict")] == ["wrap_up"])


def test_no_usage_does_not_break_any_site():
    print("\n[接线] 端点不回用量 → 三条路照常跑完，trace 里只是少一格")
    rec, made = _run_planner("text", AIMessage(content=_TEXT_REPLY))
    ev = _events(rec, "planner", "llm_done")
    check("planner：没有用量也落 llm_done（duration_s 仍在）",
          len(ev) == 1 and isinstance(ev[0].get("duration_s"), float)
          and "input" not in ev[0], str(ev))


if __name__ == "__main__":
    for fn in (test_shape_langchain_and_raw,
               test_cache_read_absent_is_not_zero,
               test_no_usage_returns_empty_and_never_raises,
               test_planner_records_usage_both_engines,
               test_narrator_records_usage,
               test_display_text_records_usage,
               test_display_text_failure_path_still_quiet,
               test_reflector_records_usage,
               test_no_usage_does_not_break_any_site):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
