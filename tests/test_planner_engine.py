# -*- coding: utf-8 -*-
"""planner 接口层档位（`settings.planner_engine`）单测：离线、秒级、零网络零 LLM。

**为什么单独一套**：`agent/native_plan.py` 的纯函数测试（`tests/test_native_plan.py`）
只验"给一个响应，映射对不对"。而这一批真正会出事的缝在**接线**上——`planner_node`
里那条分叉：档位读没读到、schema 有没有绑上、判不了时有没有落回既有兜底、影子里
那条多出来的 LLM 调用会不会反过来影响主路。**"能力有测试 ≠ 接线有测试"**是这个仓
反复吃过亏的一类洞（探针跑绿了、线上那条路没接上）。

钉住的契约（这里红 = 某条判据被改掉了，改动要同步改这里）：

  · 默认档 `text` 的行为**逐字节不变**：仍拿 400/30s/关思考的那只 LLM，提示词里
    仍是"两行纯文本"契约（这是"上线零行为变更"的全部依据）；
  · `native` 档：schema 来自 `visible_skills(role)`（与菜单同源）、预算取
    `settings.planner_native_*`、提示词换成"用工具调用表达决定"那一份；
  · **判不了就落回既有的文本解析**（不新增降级路径），并落 `native_fallback`；
  · `shadow` **不改行为**：主路仍是 text，只多一次 native 调用，且它**炸了也不许
    影响本轮**；
  · 认不出的档位值一律当 `text`（失败方向朝"与历史一致"）。

⚠️ 本文件**必须**能证明自己驱动到了 LLM 决策轮：快道（导航/文章/特效切换/屏幕显示）
会零 LLM 直接出计划，用例句子一旦命中快道，下面所有断言都会"绿得毫无意义"。
每个用例因此都断言了"假 LLM 确实被调用过"。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.principal import Principal  # noqa: E402
from agent.skills import visible_skills  # noqa: E402
from config import settings  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# 这句**不命中任何快道**（导航/文章/特效切换/屏幕显示都要更具体的形态），
# 保证每一轮都真的走到 LLM 决策——见文件头注的"绿得毫无意义"那条。
_MSG = "帮我把樱花打开"
_CFG = {"configurable": {"principal": Principal(uid=7, role="admin"),
                         "user_id": 7, "conversation_id": 42, "stop_event": None}}


class _FakeLLM:
    """记录构造参数与 `bind_tools` 入参；`invoke` 按脚本返回（脚本空则复用最后一个）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.kw: dict = {}
        self.bound: dict | None = None
        self.prompts: list[str] = []

    def bind_tools(self, tools, **kw):
        self.bound = {"tools": tools, **kw}
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if not self.replies:
            raise AssertionError("假 LLM 的脚本用完了（本轮调用了不止一次）")
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def _patch_llm(text_reply, native_reply=None):
    """按 max_tokens 区分两只 LLM：文本档 400、native 档取 settings 的值。

    用预算而不是调用序来区分，是因为**影子档会构造两只**，顺序不可依赖。
    """
    made: dict[str, _FakeLLM] = {}

    def _fake_get_llm(**kw):
        if kw.get("max_tokens") == 400:
            llm = _FakeLLM([text_reply])
            made["text"] = llm
        else:
            llm = _FakeLLM([native_reply] if native_reply is not None else [])
            made["native"] = llm
        llm.kw = kw
        return llm

    return _fake_get_llm, made


def _run(state_over: dict | None = None) -> dict:
    st = {"messages": [HumanMessage(content=_MSG)], "plan_rounds": 0,
          "executed": [], "tool_data": []}
    st.update(state_over or {})
    return G.planner_node(st, _CFG)


def _events(rec, name: str) -> list[dict]:
    return [e for e in rec.events if e.get("node") == "planner" and e.get("event") == name]


def _engine(val: str | None):
    """临时改档位（返回还原函数）。None = 还原成构造时的默认值。"""
    old = settings.planner_engine
    settings.planner_engine = val if val is not None else old
    return lambda: setattr(settings, "planner_engine", old)


_TEXT_REPLY = ('SKILL=effect\nPARAMS={"effect": "sakura", "action": "on"}\n'
               "TOOLS: toggle_effect\nNOTE: （无）\nREPLY: 已经打开樱花啦")


def _native_call(name="effect", args=None, calls=None):
    return AIMessage(content="", tool_calls=calls or [
        {"name": name, "args": args or {"effect": "sakura", "action": "on"},
         "id": "c1", "type": "tool_call"}])


# ── ① 默认档：行为逐字节不变 ────────────────────────────────────────────────
def test_text_engine_is_the_default_and_unchanged():
    print("\n[档位] 默认 text：预算/提示词/事件都还是历史那一套")
    rec = trace_mod.start_trace("t_text", 7, "th", {}, dir=tempfile.mkdtemp(), by_day=False)
    fake, made = _patch_llm(_TEXT_REPLY)
    restore = _engine("text")
    old_get, G.get_llm = G.get_llm, fake
    try:
        out = _run()
    finally:
        G.get_llm = old_get
        restore()
    check("确实走到了 LLM 决策轮（没被快道截走）", len(made.get("text", _FakeLLM([])).prompts) == 1)
    check("文本档预算仍是 400/30s/关思考",
          made["text"].kw.get("max_tokens") == 400
          and made["text"].kw.get("timeout") == 30
          and made["text"].kw.get("enable_thinking") is False, str(made["text"].kw))
    check("没绑 tools（文本档 BIND 一次都不该发生）", made["text"].bound is None)
    check("提示词里仍是「两行纯文本」契约",
          "SKILL: <技能名>" in made["text"].prompts[0]
          and "工具调用表达" not in made["text"].prompts[0])
    check("计划仍按文本契约解析出来", "SKILL=effect" in out["plan"], out["plan"].splitlines()[0])
    ev = _events(rec, "decision")
    check("decision 事件带 engine=text", len(ev) == 1 and ev[0].get("engine") == "text", str(ev))


# ── ② native 档：工具调用是主路 ─────────────────────────────────────────────
def test_native_engine_takes_the_tool_call_as_the_plan():
    print("\n[档位] native：schema 同源、预算独立、提示词换成工具契约")
    rec = trace_mod.start_trace("t_native", 7, "th", {}, dir=tempfile.mkdtemp(), by_day=False)
    fake, made = _patch_llm(_TEXT_REPLY, _native_call())
    restore = _engine("native")
    old_get, G.get_llm = G.get_llm, fake
    try:
        out = _run()
    finally:
        G.get_llm = old_get
        restore()
    check("native 那只 LLM 被调用了一次",
          len(made.get("native", _FakeLLM([])).prompts) == 1)
    check("native 预算取 settings（不是文本档的 400）",
          made["native"].kw.get("max_tokens") == settings.planner_native_max_tokens
          and made["native"].kw.get("timeout") == settings.planner_native_timeout
          and made["native"].kw.get("enable_thinking") == settings.planner_native_thinking,
          str({k: v for k, v in made["native"].kw.items() if k != "temperature"}))
    names = {t["function"]["name"] for t in (made["native"].bound or {}).get("tools") or []}
    check("绑的 schema 与 visible_skills(admin) 名字集合相等（不扩权是结构性的）",
          names == {s.name for s in visible_skills("admin")}, str(len(names)))
    check("tool_choice=auto + parallel_tool_calls=False",
          made["native"].bound.get("tool_choice") == "auto"
          and made["native"].bound.get("parallel_tool_calls") is False,
          str(made["native"].bound and {k: v for k, v in made["native"].bound.items()
                                        if k != "tools"}))
    check("提示词换成了工具契约、且不含旧契约行",
          "工具调用表达" in made["native"].prompts[0]
          and "SKILL: <技能名>" not in made["native"].prompts[0])
    check("计划就是那次工具调用（技能级参数）",
          "SKILL=effect" in out["plan"]
          and '"effect": "sakura"' in out["plan"], out["plan"].splitlines()[:2])
    check("decision 事件带 engine=native",
          [e for e in _events(rec, "decision") if e.get("engine") == "native"])
    nd = _events(rec, "native_decision")
    check("native_decision 事件记了函数名与 finish_reason",
          len(nd) == 1 and nd[0].get("calls") == "effect"
          and nd[0].get("finish") == "", str(nd))


def test_native_zero_call_is_chat_and_visible():
    print("\n[档位] native 零调用 = 闲聊轮（可分辨，不是静默落成 chat）")
    rec = trace_mod.start_trace("t_zero", 7, "th", {}, dir=tempfile.mkdtemp(), by_day=False)
    fake, made = _patch_llm(_TEXT_REPLY, AIMessage(content="你好呀～"))
    restore = _engine("native")
    old_get, G.get_llm = G.get_llm, fake
    try:
        out = _run()
    finally:
        G.get_llm = old_get
        restore()
    check("计划落到 chat", "SKILL=chat" in out["plan"], out["plan"].splitlines()[0])
    nd = _events(rec, "native_decision")
    check("native_decision 里 calls 为空串（**可分辨**：这不是一次技能调用）",
          len(nd) == 1 and nd[0].get("calls") == "" and nd[0].get("skill") == "chat", str(nd))
    check("没有落 native_fallback（没失败，是模型选择不调用）",
          not _events(rec, "native_fallback"))


def test_native_bad_response_falls_back_to_text_parsing():
    print("\n[档位] native 判不了 → 落回既有文本解析（不新增降级路径）")
    rec = trace_mod.start_trace("t_fb", 7, "th", {}, dir=tempfile.mkdtemp(), by_day=False)
    # 与探针里那条"思考链吃光额度"同形：arguments 断在半截 ⇒ invalid_tool_calls
    broken = AIMessage(content=_TEXT_REPLY, invalid_tool_calls=[
        {"name": "effect", "args": '{"effect": "sak', "id": "c", "error": "parse"}])
    fake, made = _patch_llm(_TEXT_REPLY, broken)
    restore = _engine("native")
    old_get, G.get_llm = G.get_llm, fake
    try:
        out = _run()
    finally:
        G.get_llm = old_get
        restore()
    check("那一版的原正文仍被读出来了（没白扔一次能用的决策）",
          "SKILL=effect" in out["plan"], out["plan"].splitlines()[0])
    fb = _events(rec, "native_fallback")
    check("落了 native_fallback（截断是静默失败，必须留痕）",
          len(fb) == 1 and fb[0].get("text_len", 0) > 0, str(fb))
    check("没有 native_decision（这一轮不是工具调用给的）",
          not _events(rec, "native_decision"))


def test_native_multi_call_takes_first_and_accounts():
    print("\n[档位] native 多调用：只取第一条 + 记账进 decision 事件")
    rec = trace_mod.start_trace("t_multi", 7, "th", {}, dir=tempfile.mkdtemp(), by_day=False)
    two = AIMessage(content="", tool_calls=[
        {"name": "effect", "args": {"effect": "sakura", "action": "on"},
         "id": "a", "type": "tool_call"},
        {"name": "darkmode", "args": {"mode": "on"}, "id": "b", "type": "tool_call"}])
    fake, made = _patch_llm(_TEXT_REPLY, two)
    restore = _engine("native")
    old_get, G.get_llm = G.get_llm, fake
    try:
        out = _run()
    finally:
        G.get_llm = old_get
        restore()
    check("取第一条（effect）", "SKILL=effect" in out["plan"], out["plan"].splitlines()[0])
    check("darkmode 没有被拼进同一份计划（一张确认卡只装同一个技能的动作）",
          "SKILL=darkmode" not in out["plan"] and "darkmode" not in out["plan"].split("\n")[0])
    dec = _events(rec, "decision")
    check("记账进了 decision 事件（native_multi_call:…）",
          len(dec) == 1 and "native_multi_call:effect|darkmode" in str(dec[0].get("native_note")),
          str(dec[0].get("native_note")))


# ── ③ 认不出的档位 ──────────────────────────────────────────────────────────
def test_unknown_engine_falls_back_to_text():
    print("\n[档位] 认不出的值 → text（失败方向朝「与历史一致」）")
    check("_PLANNER_ENGINES 恰为三档", G._PLANNER_ENGINES == ("text", "native", "shadow"),
          str(G._PLANNER_ENGINES))
    # 认不出的 → text；但**先剥空白再小写**（env 值常带尾随空格/换行，"NATIVE " 是
    # 配错格式而不是选错档位，退回 text 会把"我明明开了 native"变成查不出的疑问）
    for val, want in (("nativ", "text"), ("NATIVE ", "native"), (" Native", "native"),
                      ("", "text"), ("text2", "text"), ("None", "text")):
        restore = _engine(val)
        try:
            got = G._planner_engine()
        finally:
            restore()
        check(f"engine={val!r} → {want}", got == want, got)
    # 认不出的值**不炸**：照 text 走完整一轮
    fake, made = _patch_llm(_TEXT_REPLY)
    restore = _engine("nativ")
    old_get, G.get_llm = G.get_llm, fake
    try:
        out = _run()
    finally:
        G.get_llm = old_get
        restore()
    check("认不出的值照 text 跑完一轮、没有 native 那只 LLM",
          "SKILL=effect" in out["plan"] and "native" not in made, str(sorted(made)))


# ── ④ 影子档：只观测，不改行为 ──────────────────────────────────────────────
def test_shadow_keeps_text_behavior_and_compares():
    print("\n[档位] shadow：主路仍 text，多跑一遍 native 只比对")
    rec = trace_mod.start_trace("t_shadow", 7, "th", {}, dir=tempfile.mkdtemp(), by_day=False)
    # 影子故意给一个**不同**的技能，用来验 agree=False 真的会出现（都给 effect 的话
    # 这条断言恒真、等于没测）
    fake, made = _patch_llm(_TEXT_REPLY, _native_call("chat", {"reply": "你好呀"}))
    restore = _engine("shadow")
    old_get, G.get_llm = G.get_llm, fake
    try:
        out = _run()
    finally:
        G.get_llm = old_get
        restore()
    check("两只 LLM 都造了、都被调用过（主路 + 影子）",
          len(made["text"].prompts) == 1 and len(made["native"].prompts) == 1)
    check("**行为没变**：计划仍来自 text 那一版", "SKILL=effect" in out["plan"],
          out["plan"].splitlines()[0])
    check("影子用的是 native 契约提示词（否则比的是两个不同的提问）",
          "工具调用表达" in made["native"].prompts[0])
    sh = _events(rec, "shadow")
    check("shadow 事件记了两个技能与 agree=False",
          len(sh) == 1 and sh[0].get("skill_text") == "effect"
          and sh[0].get("skill_native") == "chat" and sh[0].get("agree") is False, str(sh))
    # `decision.engine` 记的是**本轮配的档位**（shadow），不是"判决来自哪一版"——
    # 1C 的报告要按这个字段切语料，所以它必须如实写 shadow；判决来自 text 这件事
    # 由上面那条"计划仍来自 text"钉住，两件事分开记，别混成一个字段。
    dec = _events(rec, "decision")
    check("decision 事件记 engine=shadow + skill 来自 text 那一版",
          len(dec) == 1 and dec[0].get("engine") == "shadow"
          and dec[0].get("skill") == "effect", str(dec))


def test_shadow_failure_never_breaks_the_round():
    print("\n[档位] shadow 自己炸了 → 本轮照常（观测设备不许影响主路）")
    rec = trace_mod.start_trace("t_shadowfail", 7, "th", {}, dir=tempfile.mkdtemp(),
                                by_day=False)

    class _Boom(_FakeLLM):
        def invoke(self, prompt):
            raise RuntimeError("boom: shadow llm down")

    fake, made = _patch_llm(_TEXT_REPLY, AIMessage(content=""))
    restore = _engine("shadow")
    old_get = G.get_llm

    def _get(**kw):
        llm = fake(**kw)
        if kw.get("max_tokens") == 400:
            return llm
        return _Boom([])                      # native 那只（影子）直接炸

    G.get_llm = _get
    try:
        out = _run()
    finally:
        G.get_llm = old_get
        restore()
    check("本轮计划照常产出", "SKILL=effect" in out["plan"], out["plan"].splitlines()[0])
    sh = _events(rec, "shadow")
    check("shadow 事件记了 error（不是静默吞掉）",
          len(sh) == 1 and "boom" in str(sh[0].get("error")), str(sh))


# ── ⑤ 提示词契约的两个版本 ─────────────────────────────────────────────────
def test_output_contract_variants():
    print("\n[提示词] 规则 7 两个版本各自成立、互不混入")
    t, n = G._PLANNER_OUTPUT_CONTRACT_TEXT, G._PLANNER_OUTPUT_CONTRACT_NATIVE
    check("文本档那份仍写着两行纯文本契约",
          "SKILL: <技能名>" in t and "PARAMS: <JSON>" in t and "不要任何其他文字" in t)
    check("native 那份不含文本契约行（两版混写就是让模型二选一）",
          "SKILL: <技能名>" not in n and "PARAMS: <JSON>" not in n)
    check("native 那份讲明「决策只以工具调用为准」", "工具调用表达" in n)
    check("native 那份保留 TODO 行（多步链声明仍走正文，`_parse_todo` 读它）",
          "TODO:" in n)
    check("两份都编号为规则 7（换档不该改规则编号）",
          t.startswith("7. ") and n.startswith("7. "))


if __name__ == "__main__":
    for fn in (test_text_engine_is_the_default_and_unchanged,
               test_native_engine_takes_the_tool_call_as_the_plan,
               test_native_zero_call_is_chat_and_visible,
               test_native_bad_response_falls_back_to_text_parsing,
               test_native_multi_call_takes_first_and_accounts,
               test_unknown_engine_falls_back_to_text,
               test_shadow_keeps_text_behavior_and_compares,
               test_shadow_failure_never_breaks_the_round,
               test_output_contract_variants):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
