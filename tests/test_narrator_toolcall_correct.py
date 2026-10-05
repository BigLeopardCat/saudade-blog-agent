# -*- coding: utf-8 -*-
"""零工具 narrator 回了 tool_calls ⇒ 带纠正重问一次（20261006）。

## 为什么这条测试存在

narrator 不 `bind_tools`，但这**只约束我们不传工具**，不约束服务商不把 `tool_calls`
放进响应——20261006 归档复扫把两副面孔都翻了出来（`../logs/agent/golden_traces`，
10222 份 trace 里 15 份 `reply_capture_skipped` **全部** `tool_calls>=1`，
**15 份全部**以 gate fallback 收场）：

  · **正文为空 + 一条 tool_call**（`20261006_045036/admin_write_denied_user.json`：
    `model.llm_empty_retry` → 同消息重试 → `reply_capture_skipped {tool_calls:1,
    content_len:0}` → `gate.fallback issue=empty_reply`）。同输入同采样 ⇒ 那次盲重试
    **必然复现**同一条，主人最终读到的是一句零内容道歉。
  · **只有开场白 + 一条 tool_call**（`20261006_040340/rag_git_svn.json`：
    `producer.narrator_tool_calls n=1 names=["search_notes"]`，正文只剩
    "让我帮你找找看～"）。这一份 gate 反而判 **PASS**——内容为零却被当成成功交付。

成因是上下文里的**仿写**：`agent/context.py::with_tool_call_pairs` 为满足 strict
服务商的配对要求，在消息序列里补了 `assistant(tool_calls=…)`（见该函数 docstring），
零工具的 narrator 照着历史的样子发起了工具调用。

## 处置与它**不能**碰的东西

加**一次带纠正的调用**（`_NARRATOR_NO_TOOLCALL_NUDGE`），不是把上面那次同消息重试改成
带提示的重试——那一条被 `tests/test_narrator_empty_retry.py` 的 ⑤ 逐字锁着
（"重试必须是同一次采样"，理由：判据要能拿复跑当对照）。两条并存：先按原语义盲重试
（**只在正文为空时**），再对"回了 tool_calls"这一次单独纠正。

## 锁住的六条

  ① **回 tool_calls ⇒ 纠正后正文胜出**（调用恰好两次，返回的是第二次那条）；
  ② **只有前言那一副面孔也走同一条路**（正文非空不该成为放行的理由）；
  ③ **纠正后仍不可用（又带 tool_calls 或仍空）⇒ 退回纠正前那份正文**：不抛、不再试
     第三次，fail-open 方向不变（兜底仍归 gate）；
  ④ **反向对照：正常回复一次都不多重问**——多问一次会把正常轮次再采样一遍，等于给
     它装一枚噪声源；
  ⑤ **形状：纠正那次追加的是一条 SystemMessage，且原消息一字不动**（与盲重试的
     "逐字同一份"相反，这正是两件事的区别）；
  ⑥ **记账**：`model.llm_toolcall_correct` 带的是**被否掉那次**的用量——
     `eval/token_cost_report.py` 按"事件里有没有 input 键"归集，不记就白丢一笔。

无网络 / 无 LLM：`agent.graph.get_llm` 换成脚本假 LLM，直接调**真的** `model_node`。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.principal import Principal  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


_MSG = "随便说点什么"          # 内容与本套件无关：假 LLM 全接管，不经过 planner
_PLAN = "SKILL=chat\nPARAMS={}\nTOOLS:\nNOTE: （无）\nREPLY: （无）"
_CFG = {"configurable": {"principal": Principal(uid=7, role="admin"),
                         "user_id": 7, "conversation_id": 42, "stop_event": None}}


def _ai(text: str, out: int = 0, inp: int = 1000, cache: int | None = None,
        calls: list | None = None) -> AIMessage:
    """带用量的假回复；`calls` 非 None 时带 `tool_calls`（零工具节点不该出现的那一形态）。"""
    um = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
    if cache is not None:
        um["input_token_details"] = {"cache_read": cache}
    return AIMessage(content=text, usage_metadata=um, tool_calls=list(calls or []))


def _tc(name: str = "search_notes") -> dict:
    return {"name": name, "args": {"query": "x"}, "id": "call_1", "type": "tool_call"}


class _Scripted:
    """按脚本逐条返回的假 LLM；脚本用完后重复最后一条（调用次数本身就是判据）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list = []
        self.kw: dict = {}

    def bind_tools(self, tools, **kw):  # pragma: no cover - narrator 零工具
        raise AssertionError("narrator 不该 bind_tools（零工具是结构约束）")

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]


def _run(tid: str, replies: list):
    rec = trace_mod.start_trace(tid, 7, "th", {}, dir=tempfile.mkdtemp(), by_day=False)
    fake = _Scripted(replies)

    def _fake_get_llm(**kw):
        fake.kw = kw
        return fake

    old, G.get_llm = G.get_llm, _fake_get_llm
    try:
        out = G.model_node({"messages": [HumanMessage(content=_MSG)],
                            "plan": _PLAN, "receipts": []}, _CFG)
    finally:
        G.get_llm = old
    return out, fake, rec


def _events(rec, node: str, name: str) -> list[dict]:
    return [e for e in rec.events if e.get("node") == node and e.get("event") == name]


# ── ① 空正文 + tool_calls ⇒ 纠正后正文胜出 ──────────────────────────────
# 脚本照 **现场那三次** 排：call1 空+tool_call ⇒ （既有语义）同消息盲重试 call2
# **仍是**空+tool_call（同输入同采样，trace 实证必然复现）⇒ 才轮到带纠正的 call3。
# 这条顺序本身就是判据：**盲重试的语义没被改动**（它照旧是逐字同一份消息），
# 纠正只是接在它后面。
def test_empty_toolcall_is_corrected():
    print("\n[纠正] 正文为空 + tool_calls ⇒ 盲重试之后带纠正再问一次")
    out, fake, rec = _run("t_tc_empty",
                          [_ai("", out=14, calls=[_tc()]),
                           _ai("", out=15, calls=[_tc()]),
                           _ai("查到了，站内有两篇喵～", out=104)])
    check("invoke 恰好三次", len(fake.prompts) == 3, str(len(fake.prompts)))
    check("前两次仍是**逐字同一份消息**（既有盲重试语义没被动过）",
          fake.prompts[0] == fake.prompts[1], "前两次不同")
    check("那次盲重试照旧留痕", len(_events(rec, "model", "llm_empty_retry")) == 1)
    got = out["messages"][0]
    check("返回的是纠正后那条（正文胜出）",
          got.content == "查到了，站内有两篇喵～", repr(got.content))
    check("返回的不再带 tool_calls（零工具节点该有的形状）",
          not getattr(got, "tool_calls", None), str(getattr(got, "tool_calls", None)))
    ev = _events(rec, "model", "llm_toolcall_correct")
    check("trace 留痕一条", len(ev) == 1, str(ev))
    check("留痕记的是它点了哪个工具",
          ev and ev[0].get("names") == ["search_notes"], str(ev))


# ── ② 只有前言那一副面孔（正文非空）也走同一条路 ────────────────────────
def test_preamble_only_toolcall_is_corrected():
    print("\n[纠正] 只有开场白 + tool_calls ⇒ 同样纠正（正文非空不是放行理由）")
    out, fake, _ = _run("t_tc_preamble",
                        [_ai("喵呜～让我帮你找找看～", out=50, calls=[_tc()]),
                         _ai("站内没有讲这个的文章喵。", out=88)])
    check("invoke 恰好两次", len(fake.prompts) == 2, str(len(fake.prompts)))
    check("返回的是纠正后那条（前言被换掉）",
          out["messages"][0].content == "站内没有讲这个的文章喵。",
          repr(out["messages"][0].content))


# ── ③ 纠正后仍不可用 ⇒ 退回纠正前那份正文（fail-open 不变）─────────────
def test_correct_then_fail_falls_back():
    print("\n[兜底] 纠正后又带 tool_calls ⇒ 不抛、不再试第三次")
    out, fake, rec = _run("t_tc_fail",
                          [_ai("喵呜～让我帮你找找看～", out=50, calls=[_tc()]),
                           _ai("", out=12, calls=[_tc("list_notes")])])
    check("恰好两次（没有第三次）", len(fake.prompts) == 2, str(len(fake.prompts)))
    check("退回的是纠正前那份正文（照旧交 gate，不在这里改判据）",
          out["messages"][0].content == "喵呜～让我帮你找找看～",
          repr(out["messages"][0].content))
    check("失败也留痕", len(_events(rec, "model", "llm_toolcall_correct_failed")) == 1)
    check("llm_done 照发（gate 读的是这条）",
          len(_events(rec, "model", "llm_done")) == 1)


# ── ④ 反向对照：正常回复一次都不多重问 ─────────────────────────────────
def test_normal_reply_not_corrected():
    print("\n[反向对照] 正常回复 ⇒ 一次都不多重问")
    out, fake, rec = _run("t_tc_ok", [_ai("好呀～", out=96)])
    check("invoke 恰好一次", len(fake.prompts) == 1, str(len(fake.prompts)))
    check("返回的还是那一条", out["messages"][0].content == "好呀～")
    check("没有纠正事件", _events(rec, "model", "llm_toolcall_correct") == [])


# ── ⑤ 形状：纠正那次 = 原消息 + 一条 SystemMessage，原消息一字不动 ──────
# 与盲重试的 ⑤（"逐字同一份"）**刻意相反**——这正是两件事的区别：盲重试是同一次采样的
# 重放，纠正是**换一次带指令的采样**。
def test_correction_appends_system_message():
    print("\n[形状] 纠正 = 同一份前缀 + 一条纠正 SystemMessage")
    _, fake, _ = _run("t_tc_shape",
                      [_ai("喵呜～先看看～", out=14, calls=[_tc()]), _ai("好了", out=104)])
    check("两次调用", len(fake.prompts) == 2, str(len(fake.prompts)))
    a, b = fake.prompts[0], fake.prompts[1]
    check("原消息一字不动（前缀逐字相同）", list(a) == list(b[:-1]),
          f"{len(a)} vs {len(b)}")
    check("只多了一条消息，且是 SystemMessage",
          len(b) == len(a) + 1 and isinstance(b[-1], SystemMessage),
          f"{type(b[-1]).__name__} / {len(a)}→{len(b)}")
    check("纠正文本写明了「不要输出任何工具调用」",
          "不要输出任何工具调用" in str(b[-1].content))


# ── ⑥ 记账：纠正那条事件带**被否掉那次**的用量 ──────────────────────────
def test_correction_traced_with_its_own_usage():
    print("\n[记账] 被否掉那次的 token 不凭空消失")
    _, _, rec = _run("t_tc_trace",
                     [_ai("喵呜～先看看～", out=14, inp=25200, cache=17000, calls=[_tc()]),
                      _ai("好了", out=104, inp=25400, cache=17200)])
    ev = _events(rec, "model", "llm_toolcall_correct")
    check("llm_toolcall_correct 恰好一条", len(ev) == 1, str(ev))
    check("带的是**被否掉那次**的用量",
          ev and ev[0].get("input") == 25200 and ev[0].get("output") == 14
          and ev[0].get("cache_read") == 17000, str(ev))
    done = _events(rec, "model", "llm_done")
    check("llm_done 记的是**第二次**那份",
          len(done) == 1 and done[0].get("input") == 25400
          and done[0].get("output") == 104, str(done))


if __name__ == "__main__":
    for fn in (test_empty_toolcall_is_corrected,
               test_preamble_only_toolcall_is_corrected,
               test_correct_then_fail_falls_back,
               test_normal_reply_not_corrected,
               test_correction_appends_system_message,
               test_correction_traced_with_its_own_usage):
        fn()
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 条判据未通过：")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ 全部通过")
