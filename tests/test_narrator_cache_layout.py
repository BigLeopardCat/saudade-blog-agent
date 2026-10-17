# -*- coding: utf-8 -*-
"""narrator 出站序列的**装配顺序**（20261016）：稳定头在前、每轮都变的那段在历史之后。

## 为什么这条测试存在

前缀缓存的命中率 = **最长公共前缀** / 输入 tok。narrator 此前的装配是

    [SystemMessage(全量提示词)] + [System 上下文块] + 20 条历史 + 当前问题 + 本轮工具帧

两处"每轮都变的东西站在最前面"：系统提示词从 `{plan}` 起每轮不同，那条
`[System: …]` 上下文块里 `current_time` **精确到分钟**。于是公共前缀在**第二条消息**
就断了——后面整整 20 条历史一条都进不了缓存。

生产实测（`logs/agent/traces`，20260927–20261016，486 次 narrator 调用）：

  · 稳定头 = `_EXECUTOR_HEAD` + `NARRATOR_DISCIPLINE` = 8304 字 ≈ 5033 tok；
  · 每次命中的**中位数恒为 5248 tok** ⇒ 命中的恰好只有那个头，历史全长在外；
  · 分桶：与同会话上一次 narrator 间隔 ≤5 分钟的 39 次调用占 narrator 输入 tok 的 61%，
    当前命中 48.7%（近 5 天口径）——那一桶就是这次改动的射程。

改法 = **只重排发给模型的那份副本**（`_narrator_wire`）：上下文块从首位移到末尾，
系统提示词拆成"稳定头"与"变化段"两半、变化段放到历史之后。
`state["messages"]` 一个字不改——planner / gate / `_page_ctx` / `strict_wire_issues` /
`_last_user_msg` 读的都是 state。

## 锁住的七条

  ① 首条是**稳定头**（人设 + 纪律），里面**没有**本轮计划文本；
  ② 末条是**变化段**，里面**有**本轮计划文本（两半合起来仍是原来那一整段）；
  ③ 那条 `[System: …]` 上下文块**不再在首位**，而是排在整段历史之后；
  ④ **判据本体**：连续两轮对话的**公共前缀字符数**。新装配覆盖整段历史，
     旧装配只覆盖稳定头——**反控**把上下文块放回首位的旧装配，断言当场塌掉
     （证明这条判据不恒绿，不是"随便怎么排都通过"）；
  ⑤ **fail-open**：首条不是那个形状（主人自己打了一句 `[System …` 开头的话）⇒ 原样不动；
  ⑥ **不改入参**：`state["messages"]` 调用前后逐条相同；
  ⑦ 出站序列仍过 `strict_wire_issues`（重排没有把 `tool_call_id` 的配对搞坏）。

无网络 / 无 LLM：`agent.graph.get_llm` 换成脚本假 LLM，直接调**真的** `model_node`，
判据看的是**它实际发出去的那份 `_msgs`**（`fake.prompts[0]`），不是源码文本。

用法：.venv/bin/python tests/test_narrator_cache_layout.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.context import strict_wire_issues  # noqa: E402
from agent.principal import Principal  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


_MSG = "这篇讲了什么"
_PLAN = "WORKFLOW=content_query\nPARAMS={}\nTOOLS:\nNOTE: （无）\nREPLY: （无）"
_PLAN2 = "WORKFLOW=chat\nPARAMS={}\nTOOLS:\nNOTE: （无）\nREPLY: （无）"
_CFG = {"configurable": {"principal": Principal(uid=7, role="admin"),
                         "user_id": 7, "conversation_id": 42, "stop_event": None}}

# 两轮对话共用的那段历史（逐字相同 = 真实情况下它来自 DB，写入后不再变）。
# 刻意铺到与生产同量级（生产 narrator 的历史中位 ~9000 字）：太短的话"公共前缀多覆盖
# 了历史"这条断言只值几十个字，反控臂的对比也就看不出来了。
_HIST = [
    HumanMessage(content="你好"),
    AIMessage(content="你好呀～我是泠月喵，这个博客的看板娘。有什么想问的尽管说，"
                      "站内的文章、留言、说说我都能帮你翻一翻。"),
    HumanMessage(content="你平时都看些什么"),
    AIMessage(content="我平时就守着这座河灯集呀。主人写的那些文章我基本都翻过，"
                      "最近在整理检索那一块的笔记，你要是想看我可以带你过去。"),
    HumanMessage(content="那帮我看看有没有讲检索的文章"),
    AIMessage(content="翻到了几篇，我先把标题给你：一篇讲 BM25 倒排，一篇讲混合检索"
                      "与倒数排名融合，还有一篇是长文分节的工程记录。"),
]


def _ctx(stamp: str) -> HumanMessage:
    """`server._build_messages` 造的那条系统上下文块。**每次都不一样**——`current_time`
    精确到分钟，这正是它站在首位时会把整段历史踢出缓存的原因。"""
    return HumanMessage(content=f"[System: user_id=7, page=/article/46, title=某篇, "
                                f"current_effects=none, current_darkmode=off, "
                                f"current_time=2026年10月16日 星期五 {stamp}]")


def _ai(text: str = "好呀～", inp: int = 1000, cache: int = 5000) -> AIMessage:
    return AIMessage(content=text, usage_metadata={
        "input_tokens": inp, "output_tokens": 10, "total_tokens": inp + 10,
        "input_token_details": {"cache_read": cache}})


class _Capture:
    """假 LLM：把**实际发出去的 `_msgs`** 记下来，这是本套件唯一的事实来源。"""

    def __init__(self):
        self.prompts: list = []

    def bind_tools(self, tools, **kw):  # pragma: no cover - narrator 零工具
        raise AssertionError("narrator 不该 bind_tools（零工具是结构约束）")

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return _ai()


def _run(messages: list, plan: str = _PLAN):
    """真调 `model_node`，返回它发出去的 `_msgs`（`state["messages"]` 原样传入）。"""
    rec = trace_mod.start_trace("t_cache_layout", 7, "th", {}, dir=tempfile.mkdtemp(),
                                by_day=False)
    fake = _Capture()
    old, G.get_llm = G.get_llm, (lambda **kw: fake)
    try:
        G.model_node({"messages": messages, "plan": plan, "receipts": []}, _CFG)
    finally:
        G.get_llm = old
    del rec
    return fake.prompts[0]


def _text(m) -> str:
    c = getattr(m, "content", "")
    return c if isinstance(c, str) else str(c)


def _common_prefix(a: list, b: list) -> int:
    """两个消息列表的**公共前缀字符数**——前缀缓存命中的就是这个量（按 token，同源同序）。

    逐条比文本；同一条内再取公共前缀。遇到第一条不同的消息就停。
    """
    n = 0
    for x, y in zip(a, b):
        tx, ty = _text(x), _text(y)
        if tx != ty:
            i = 0
            while i < min(len(tx), len(ty)) and tx[i] == ty[i]:
                i += 1
            return n + i
        n += len(tx)
    return n


def _static_segments(tpl: str) -> list:
    """模板里**不是占位符**的那些字面段（`string.Formatter` 的口径，与 `.format()` 同源）。

    ⚠️ `Formatter().parse` 出的是 `(literal_text, field_name, format_spec, conversion)`
    ——**字面段是第一个**。第一版把它写在第四个位置上（按 `lit` 取），于是永远收到空表，
    下面那条"逐段在场"就成了一条**恒绿的空断言**（本仓最贵的那类失效）。所以这里
    连"段数 > 0"一起断言。
    """
    import string
    return [lit for lit, _f, _s, _c in string.Formatter().parse(tpl) if lit]


def _full_prompt(plan: str) -> str:
    """**改动前**的整段系统提示词（旧装配用，反控臂）。"""
    return G._EXECUTOR_PROMPT.format(
        persona=G.BLOG_ASSISTANT_PROMPT, audience=G.audience_block("admin"),
        plan=plan, tool_frames="（本轮尚无工具执行）", exec_receipts="（无）",
        fact_block="（无）", page_ctx="（无）", sticker_guide=G.STICKER_GUIDE)


def _turn(hist: list, ctx_stamp: str, question: str) -> list:
    """一轮请求的 `state["messages"]`：`[上下文块] + 历史 + [当前问题]`。"""
    return [_ctx(ctx_stamp)] + list(hist) + [HumanMessage(content=question)]


def main() -> int:
    t1 = _turn(_HIST, "04:24", "这篇讲了什么")
    wire = _run(t1)

    print("① 首条 = 稳定头（人设 + 叙述纪律），不含本轮计划文本")
    head = _text(wire[0])
    check("首条是 SystemMessage", isinstance(wire[0], SystemMessage), type(wire[0]).__name__)
    check("头里有人设", G.BLOG_ASSISTANT_PROMPT[:40] in head)
    check("头里有叙述纪律", G.NARRATOR_DISCIPLINE in head, f"{len(G.NARRATOR_DISCIPLINE)} 字")
    check("头里**没有**本轮计划文本（`{plan}` 那一半已经被搬走）",
          "WORKFLOW=content_query" not in head)

    print("\n② 末条 = 变化段，本轮计划文本在这里")
    tail = _text(wire[-1])
    _state = {"messages": t1, "plan": _PLAN, "receipts": []}
    _real_plan = G._narrator_plan(_state, _CFG)
    check("末条是 SystemMessage", isinstance(wire[-1], SystemMessage), type(wire[-1]).__name__)
    check("变化段里有本轮计划文本", "WORKFLOW=content_query" in tail)
    check("变化段用的是 `_narrator_plan` 那一份（没换来源）",
          tail.startswith(_real_plan), _real_plan.splitlines()[0])
    # **拆分零改动**这条不能在"渲染后"比：`_full_prompt` 的其余槽位是占位值，真的那份
    # 渲染的是真帧/真页面上下文，比长度只会比出槽位差异。所以在**静态骨架**上比：
    # ① 头逐字节仍是原来那一整段的前缀（模板级判据，同 `test_react_narrator_assets.py`）；
    # ② 变化段的每一段静态文字**逐段在场、顺序不变**。
    check("头**逐字节**仍是原来那一整段的前缀（拆分的头一个字没动）",
          _full_prompt(_real_plan).startswith(head), f"头 {len(head)} 字")
    _segs = _static_segments(G._EXECUTOR_TAIL)
    _pos, _ok = 0, True
    for s in _segs:
        i = tail.find(s, _pos)
        if i < 0:
            _ok = False
            break
        _pos = i + len(s)
    check(f"变化段的 {len(_segs)} 段静态骨架逐段在场、顺序不变",
          _segs and _ok and _pos <= len(tail), f"最后一段收在 {_pos}/{len(tail)}")

    print("\n③ 那条 `[System: …]` 上下文块挪到历史之后")
    ctx_at = [i for i, m in enumerate(wire) if _text(m).lstrip().startswith("[System:")]
    check("上下文块**不在**首位", ctx_at and ctx_at[0] != 0, str(ctx_at))
    check("它排在整段历史之后",
          ctx_at == [len(wire) - 2] if ctx_at else False, f"位置 {ctx_at} / 共 {len(wire)} 条")
    check("历史顺序没被打乱",
          [_text(m) for m in wire[1:1 + len(_HIST)]] == [_text(m) for m in _HIST],
          str([_text(m)[:8] for m in wire[1:1 + len(_HIST)]]))

    print("\n④ 判据本体：连续两轮的**公共前缀字符数**")
    # 第二轮 = 第一轮的历史 + 第一轮的问答；上下文块里的分钟数也变了；**计划也变了**
    # （真实的连续两轮几乎不会是同一个技能）。
    hist2 = _HIST + [HumanMessage(content="这篇讲了什么"), AIMessage(content="好呀～")]
    t2 = _turn(hist2, "04:26", "那第二篇呢")
    new_a, new_b = _run(t1, _PLAN), _run(t2, _PLAN2)
    new_cp = _common_prefix(new_a, new_b)

    # 反控臂 = **改动前**的装配：全量提示词在最前（计划不同 ⇒ 它每轮都变）、
    # 上下文块紧随其后（分钟数不同 ⇒ 它每轮也变）。
    old_a = [SystemMessage(content=_full_prompt(_PLAN))] + t1
    old_b = [SystemMessage(content=_full_prompt(_PLAN2))] + t2
    old_cp = _common_prefix(old_a, old_b)

    hist_chars = sum(len(_text(m)) for m in _HIST)
    check("新装配的公共前缀覆盖**整段历史**（≥ 稳定头 + 历史）",
          new_cp >= len(head) + hist_chars,
          f"公共前缀 {new_cp} 字 / 头 {len(head)} + 历史 {hist_chars}")
    check("  反控：旧装配（上下文块在首位）的公共前缀**只有稳定头**",
          old_cp < len(head) + 100, f"公共前缀 {old_cp} 字")
    # 反控臂多算的那几个字是两份计划文本的公共开头（`WORKFLOW=`，6 字）——不是历史。
    check("  两者相差的就是这次改动挣回来的量（≈ 整段历史）",
          new_cp - old_cp >= hist_chars - 10, f"差 {new_cp - old_cp} 字（历史 {hist_chars} 字）")

    print("\n⑤ fail-open：首条不是那个形状 ⇒ 原样不动")
    plain = [HumanMessage(content="[System 缺个冒号] 这是主人自己打的字"),
             AIMessage(content="嗯嗯")]
    w = _run(plain)
    _w_body = [m for m in w if not isinstance(m, SystemMessage)]
    check("原样的那两条顺序与内容都不动",
          [_text(m) for m in _w_body] == [_text(m) for m in plain],
          str([_text(m)[:12] for m in _w_body]))
    check("  反控：同样两条，首条改成真 `[System: …]` 形状 ⇒ **当场被挪走**"
          "（证明第 ⑤ 条不是因为判据恒不动）",
          [i for i, m in enumerate(_run(
              [HumanMessage(content="[System: user_id=7, current_time=04:24]"),
               AIMessage(content="嗯嗯")]))
           if _text(m).lstrip().startswith("[System:")] == [2],
          "位置应为 2（头之后、尾之前）")

    print("\n⑥ 不改入参：`state['messages']` 调用前后逐条相同")
    before = [(type(m).__name__, _text(m)) for m in t1]
    _run(t1)
    after = [(type(m).__name__, _text(m)) for m in t1]
    check("入参一个字节没动", before == after)

    print("\n⑦ 出站序列仍过 `strict_wire_issues`")
    check("空列表 = 合规（重排没把 tool_call_id 的配对搞坏）",
          strict_wire_issues(_run(t1)) == [], str(strict_wire_issues(_run(t1))))

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
