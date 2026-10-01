# -*- coding: utf-8 -*-
"""narrator 空内容重试一次（20261001）：`model_node` 对采样失败的兜底。

## 为什么这条测试存在

上游偶发回"有 completion token、正文却是空串"的响应：`usage.output` 十几到几十，
`content.strip()` 为空。它与提示词、帧数、轮次都无关——**同一条用例换个时间再跑就是
正常回复**。20261001 夜间 `nav_article_target` 是实证：首跑 `model.llm_done output=14`、
正文为空 ⇒ gate 判 `empty_reply` ⇒ 主人读到的是一句内容为零的道歉（`_FALLBACK_EMPTY`，
"我刚才好像卡住了，没能说出话来"），而复跑同一用例 `output=104`、正常通过——**那一轮的
工具帧、执行回执、动作事实块全都是好好的，被扔掉的只有措辞**。

定量的口子在两处（都逐条读过 trace）：20261001 夜间那一跑 133 个 narrator 轮里 3 条判成
`empty_reply`（2.3%，三条的 `output` 分别是 14 / 13 / 29，而 `input` 从 6235 到 33268
都有 ⇒ 不是"提示词太长"也不是"某类轮次"，就是采样本身）；同日生产语料 1167 份 trace /
1137 个 narrator 轮里 1 条（0.09%）。

修法是**重试一次**而不是放宽判据：这是**采样失败**不是判据错误（复跑就正常），
"再问一次模型"比"把这条判据放宽"更接近事实。仍为空 ⇒ 照旧走既有兜底，fail-open
方向不变（多花的只有一次调用）。

## 锁住的四条

  ① **空 ⇒ 重试，第二次的正文胜出**（调用恰好两次，返回的是第二次那条）；
  ② **不空 ⇒ 一次都不多重试**（反向对照。多调一次不只是白花钱——它会把一条本来正常的
     回复再采样一遍，等于给"正常轮次"也装上一枚噪声源）；
  ③ **两次都空 ⇒ 不抛、原样返回**（兜底仍归 gate，不许在这里改判据或把异常抛出去）；
  ④ **重试在 trace 里留痕，且账没记错**：`model.llm_empty_retry` 带**第一次**那份用量，
     `model.llm_done` 带**第二次**那份。这一条不是形式——`eval/token_cost_report.py` 与
     `eval/dial_matrix.py` 都按"事件里有没有 `input` 键"归集（**不看事件名**），所以两次
     调用各记一笔，重试的 token 不会从成本账上凭空消失。

## 顺手钉住的两条

  ⑤ **重试用的是同一份消息**（逐字同一次 `_msgs`）——不追加"请再答一次"、不换提示词。
     一旦重试变成"第二套提示词"，它就不是同一次采样了，判据也再没法拿复跑当对照。
  ⑥ **判空看 `.strip()`**：纯空白（空格/换行）算空——gate 判 `empty_reply` 用的是
     同一个表达式 `(content or "").strip()`，改窄了这一侧就会漏掉一类真实空回复。

无网络 / 无 LLM：`agent.graph.get_llm` 换成脚本假 LLM，直接调**真的** `model_node`。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

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


def _ai(text: str, out: int = 0, inp: int = 1000, cache: int | None = None) -> AIMessage:
    """带用量的假回复。`out` 与 `inp` 刻意分得开，好判"这条事件记的是哪一次调用"。"""
    um = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
    if cache is not None:
        um["input_token_details"] = {"cache_read": cache}
    return AIMessage(content=text, usage_metadata=um)


class _Scripted:
    """按脚本逐条返回的假 LLM；脚本用完后重复最后一条。

    用"重复最后一条"而不是用完就抛：**调用次数本身就是判据**（反向对照要的是"恰好
    一次"），用抛会把"多调了一次"报成异常，看不出是哪条判据红的。
    """

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
    """真调 `model_node`，返回 (返回的 state 片段, 假 LLM, 这次 trace 的 recorder)。"""
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


# ── ① 空 ⇒ 重试，第二次胜出 ──────────────────────────────────────────────
def test_empty_retries_once():
    print("\n[重试] 空内容 ⇒ 再问一次")
    out, fake, _ = _run("t_retry_empty", [_ai("", out=14), _ai("好了主人～", out=104)])
    check("invoke 恰好两次", len(fake.prompts) == 2, str(len(fake.prompts)))
    got = out["messages"][0]
    check("返回的是第二次那条（正文胜出）", got.content == "好了主人～", repr(got.content))
    check("返回的仍是 AIMessage（model_node 的契约不变）",
          isinstance(got, AIMessage), type(got).__name__)


# ── ② 反向对照：不空就一次都不多重试 ─────────────────────────────────────
def test_nonempty_does_not_retry():
    print("\n[反向对照] 正常回复 ⇒ 一次都不多重试")
    out, fake, rec = _run("t_retry_ok", [_ai("好呀～", out=96)])
    check("invoke 恰好一次", len(fake.prompts) == 1, str(len(fake.prompts)))
    check("返回的还是那一条", out["messages"][0].content == "好呀～")
    check("trace 里没有 llm_empty_retry 事件（没空过就不该留痕）",
          _events(rec, "model", "llm_empty_retry") == [],
          str(_events(rec, "model", "llm_empty_retry")))


# ── ③ 两次都空：不抛、原样返回（兜底仍归 gate）──────────────────────────
def test_both_empty_fail_open():
    print("\n[兜底] 两次都空 ⇒ 不抛、不再试第三次")
    out, fake, rec = _run("t_retry_both_empty", [_ai("", out=9), _ai("   ", out=9)])
    check("恰好两次（没有第三次重试）", len(fake.prompts) == 2, str(len(fake.prompts)))
    check("原样返回那条空内容（改判据的事不在这里做）",
          (out["messages"][0].content or "").strip() == "",
          repr(out["messages"][0].content))
    check("仍留痕（这一次重试也是真花掉的 token）",
          len(_events(rec, "model", "llm_empty_retry")) == 1)
    check("llm_done 照发（gate 那边读的是这条）",
          len(_events(rec, "model", "llm_done")) == 1)


# ── ⑥ 判空看 .strip()：纯空白算空，"0" 不算 ─────────────────────────────
def test_blankness_judge():
    print("\n[判据] 纯空白算空、有内容不算空")
    out, fake, _ = _run("t_retry_ws", [_ai("   \n  ", out=11), _ai("补上了", out=50)])
    check("纯空白 ⇒ 重试（与 gate 的 `(content or '').strip()` 同一判据）",
          len(fake.prompts) == 2 and out["messages"][0].content == "补上了",
          f"{len(fake.prompts)} 次 / {out['messages'][0].content!r}")
    out, fake, _ = _run("t_retry_zero", [_ai("0", out=3)])
    check("'0' 不是空 ⇒ 不重试", len(fake.prompts) == 1, str(len(fake.prompts)))


# ── ⑤ 重试用的是同一份消息 ──────────────────────────────────────────────
def test_retry_reuses_same_messages():
    print("\n[形状] 重试 = 同一次采样的重放")
    _, fake, _ = _run("t_retry_same", [_ai("", out=14), _ai("好了", out=104)])
    check("两次调用逐字同一份消息（不追加「请再答一次」、不换提示词）",
          len(fake.prompts) == 2 and fake.prompts[0] == fake.prompts[1],
          f"{len(fake.prompts)} 次")
    check("重试不改档（enable_thinking=False 照旧）",
          fake.kw.get("enable_thinking") is False, str(fake.kw))


# ── ④ 留痕与记账：两次调用各记一笔，谁也不吞谁的 ──────────────────────────
def test_retry_is_traced_with_its_own_usage():
    print("\n[记账] 重试的用量留在它自己那条事件上")
    _, fake, rec = _run("t_retry_trace",
                        [_ai("", out=14, inp=25200, cache=17000),
                         _ai("好了", out=104, inp=25400, cache=17200)])
    check("假 LLM 确实调了两次（否则下面的账没意义）", len(fake.prompts) == 2)
    tr = _events(rec, "model", "llm_empty_retry")
    check("model.llm_empty_retry 恰好一条", len(tr) == 1, str(tr))
    check("留痕带的是**第一次**那份用量（重试的 token 不凭空消失）",
          tr and tr[0].get("input") == 25200 and tr[0].get("output") == 14
          and tr[0].get("cache_read") == 17000, str(tr))
    done = _events(rec, "model", "llm_done")
    check("llm_done 记的是**第二次**那份（最终回复那一次调用）",
          len(done) == 1 and done[0].get("input") == 25400
          and done[0].get("output") == 104 and done[0].get("cache_read") == 17200,
          str(done))


if __name__ == "__main__":
    for fn in (test_empty_retries_once,
               test_nonempty_does_not_retry,
               test_both_empty_fail_open,
               test_blankness_judge,
               test_retry_reuses_same_messages,
               test_retry_is_traced_with_its_own_usage):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
