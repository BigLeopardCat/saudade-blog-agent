# -*- coding: utf-8 -*-
"""剔空纠偏 + 零工具轮的确定性终局（资产对齐 E，20261019）。

## 为什么这条测试存在

`instantiate_plan` 早就把"点名了但一个都执行不了"的工具记在 `plan_obj["dropped"]` 上，
而 react 臂**没有一个地方读它**。形状是：模型点名的工具全被剔除 ⇒ 零工具 ⇒ 直接交给
narrator ⇒ 它手里一帧都没有、只能编 ⇒ gate 打回 ⇒ 主人看到一句降级回复**且这一轮就
结束了**（20260921 22:34 生产实证，用户原话："居然就直接结束而不是重新规划执行"）。

生产那侧治这一族的是两件，本文件钉的就是它们接上了没有：

  · **剔空纠偏**（`graph._drop_correction`）：把"这些工具在哪条通道上"写给它看、
    重新决策**一次**。位置在决策循环的尾巴上，与意图纠偏**共用**同一个"只纠一次"旗子。
  · **确定性终局**（`graph._finalize_unaccounted` 的三格）：纠偏之后仍然零工具 / 参数
    不齐 / 记账字段全空——三种形态都**不该交给 narrator 自己编**，换成带禁止句的收尾
    计划（"不许出现「看过/读过/查过/调用过工具」"）。

## 它同时钉住的一件事：纠偏文本进的是**提示词那一格**

`_drop_correction` 的正文是喂给**决策模型**的，落点必须与生产 `_planner_setup` 的
`correction=` 同路（SystemMessage 的 `{correction}` 槽）。此前那一段是清空在
`_prompt_of` **之前** ⇒ 那一格恒是缺省语，纠偏一个字节都没进过提示词，而"只纠一次"
照旧烧掉一轮（模型被问同一个问题两次）。所以下面判的是**提示词 kwargs 里的那一格**，
不是"有没有挂一条 HumanMessage"——后者曾经是另一条通道，已按生产收掉。

用法：SAUDADE_IGNORE_ENV_FILE=1 IOT_ENABLED=1 AGENT_FASTPATH=1 \\
        .venv/bin/python tests/test_react_drop_correction.py
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

import agent.react_arm as R  # noqa: E402
from agent.principal import Principal  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc + (f"  [{detail}]" if detail else ""))


ADMIN = Principal(uid=1, role="admin")

# 这句话是 22:34 那个现场的原话形状（管理员问某篇草稿的标签），也是这一族的入口。
_MSG = "小猫咪那篇文章都有什么标签呀"

# `_drop_correction` 的抬头（逐字取它第一句）——判"纠偏有没有进提示词"认的是它。
_HEAD = "你上一版决策点名的工具一个都没有执行"

# 这份参数让 `content_query` 的清单**整条被剔空**（`tools=[]`、`dropped=["rag_search"]`），
# 与 22:34 那个现场同形：意图对路、通道不对。
_DROPPED_ARGS = {"tools": ["rag_search"]}


class _Scripted(GenericFakeChatModel):
    """按脚本吐回复的假模型（与其余 react 套件同款）。

    `_stream` **必须自己实现**：基类只读 `content`、按空白切片，`tool_calls` 整个丢掉
    ⇒ 得到"模型一个工具都不调"的假现场（本文件要的恰恰是"点了名但被剔空"）。
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
SYS: list | None = None
EVENTS: list | None = None


def _drop_call(n: int) -> AIMessage:
    return AIMessage(content="让我查查", tool_calls=[
        {"name": "content_query", "args": dict(_DROPPED_ARGS),
         "id": f"call_{n}", "type": "tool_call"}])


def _run(script: list, *, patch_correction=None) -> tuple[list, list, list]:
    """跑一轮 → `(帧, 每次决策的提示词 ctx, trace 事件名)`。"""
    global SINK, SYS, EVENTS
    SINK, SYS, EVENTS = [], [], []
    _orig = (R._llm, R._real_tools, R._system_prompt, R._narrator_llm, R.record,
             R.drop_correction)
    _sink, _sys, _ev = SINK, SYS, EVENTS

    def _sysprompt(state, ctx):  # noqa: ANN001
        _sys.append(dict(ctx))
        return "你是测试用助手。"

    def _record(*a, **k):  # noqa: ANN002, ANN003
        _ev.append(a)

    try:
        R._llm = lambda: _Scripted(messages=iter(script), ai_message_chunk=iter([]))
        R._real_tools = lambda *a, **k: {}
        R._system_prompt = _sysprompt
        R._narrator_llm = lambda: _Scripted(
            messages=iter([AIMessage(content="嗯")] * 4), ai_message_chunk=iter([]))
        R.record = _record
        if patch_correction is not None:
            R.drop_correction = patch_correction
        frames = list(R.build().stream(
            {"messages": [HumanMessage(content=_MSG)]},
            {"configurable": {"principal": ADMIN}}))
        return frames, _sys, _ev
    finally:
        (R._llm, R._real_tools, R._system_prompt, R._narrator_llm, R.record,
         R.drop_correction) = _orig


def _plans(frames: list) -> list[dict]:
    return [(d or {}).get("planner") for m, d in frames
            if m == "updates" and (d or {}).get("planner")]


def _corr(ctx: dict) -> str:
    return str(ctx.get("correction") or "")


# ── ① 剔空 ⇒ 纠偏一次，正文进**提示词那一格** ────────────────────────────────
def test_correct_once() -> None:
    print("\n① 点名全被剔空 ⇒ 纠偏**一次**，正文进提示词那一格（生产 `correction=` 同款）")
    frames, sysctx, events = _run([_drop_call(i) for i in range(6)])
    check("trace 落 `planner.drop_correct`（载荷带 dropped——判据要读得出来剔的是谁）",
          [a for a in events if a[:2] == ("planner", "drop_correct")] == [
              ("planner", "drop_correct")], str([a for a in events if a[0] == "planner"]))
    check("决策模型被问了**两次**（纠偏一次 ⇒ 第二次决策；不是原地打转）",
          len(sysctx) == 2, str(len(sysctx)))
    check("**第一次**提示词里没有纠偏（那一次还没有东西可纠）",
          _HEAD not in _corr(sysctx[0]), _corr(sysctx[0])[:40])
    check("**第二次**提示词里带着剔空纠偏的正文（判的是提示词那一格，不是某条消息）",
          _HEAD in _corr(sysctx[1]), _corr(sysctx[1])[:60])
    check("纠偏正文逐条说清了工具归属（那是 `_drop_correction` 从技能注册表现读的）",
          "rag_search" in _corr(sysctx[1]), _corr(sysctx[1])[-120:])
    check("纠偏文本**不在对话窗口里**（生产也只在提示词那一格；两条通道＝两个产地）",
          all(sum(1 for m in msgs if isinstance(m, HumanMessage)) == 1 for msgs in SINK),
          str([sum(1 for m in msgs if isinstance(m, HumanMessage)) for msgs in SINK]))


# ── ② 纠偏之后仍剔空 ⇒ 确定性如实收尾（不再交给 narrator 编） ────────────────
def test_terminal() -> None:
    print("\n② 纠偏之后仍剔空 ⇒ 确定性收尾（`_finalize_unaccounted` 第一格），只纠一次")
    frames, _sysctx, events = _run([_drop_call(i) for i in range(6)])
    check("**只纠一次**（第二次仍剔空时不再纠偏——`ctx[\"corrected\"]` 那个旗子）",
          len([a for a in events if a[:2] == ("planner", "drop_correct")]) == 1,
          str([a for a in events if a[0] == "planner"]))
    plans = _plans(frames)
    last = plans[-1].get("plan_obj") or {}
    check("终局是一份**收尾计划**、零工具（生产 `_wrap_up_plan` 的产物）",
          last.get("workflow") == "chat" and not (last.get("tools") or []),
          f'{last.get("workflow")} {last.get("tools")}')
    note = str(last.get("note") or "")
    check("注记写明「本轮一个工具都没有执行」（主人读到的是「没做」而不是「失败」）",
          "一个工具都没有执行" in note, note[:70])
    check("注记里带**禁止句**（narrator 会照抄机制描述，所以纪律只能写成禁止句）",
          "不许" in note and "查过" in note, note[-90:])
    # 路由去向**不在计划对象里**（`decided` 住在本臂的 ctx 上，`_plan_update` 只写
    # plan_state 那七格）⇒ 判它得看帧序：收尾那一份计划之后**不许再有 execute**。
    # 判"去没去 execute"比判某个字段的值更接近事实：字段对了而路由没接，形状是
    # "收尾计划被当成清单执行一遍"。
    tail = [k for m, d in frames if m == "updates" for k in (d or {})]
    last_plan_at = max(i for i, (m, d) in enumerate(frames)
                       if m == "updates" and (d or {}).get("planner"))
    after = [k for m, d in frames[last_plan_at + 1:] if m == "updates" for k in (d or {})]
    check("收尾那一份计划**之后没有 execute**（narrator 拿着这段注记如实说，不是照单执行）",
          "execute" not in after, f"之后={after} 全部={tail}")


# ── ③ 反控：纠偏接空 ⇒ ① 的那条断言当场红 ───────────────────────────────────
def test_control() -> None:
    print("\n③ 反控：把 `drop_correction` 换成空串替身 ⇒ ①「纠偏进提示词」当场红")
    _frames, sysctx, events = _run([_drop_call(i) for i in range(6)],
                                   patch_correction=lambda *a, **k: "")
    check("第二次提示词里**没有**纠偏正文（证明 ① 读的是接进来的那个值）",
          _HEAD not in _corr(sysctx[1]), _corr(sysctx[1])[:40])
    check("而 `drop_correct` 事件照旧落（事件与正文是两个产地，这条区分二者）",
          len([a for a in events if a[:2] == ("planner", "drop_correct")]) == 1,
          str([a for a in events if a[0] == "planner"]))


# ── ④ 位置锁：纠偏在终局之前、终局在去重之前 ────────────────────────────────
def test_order() -> None:
    print("\n④ 位置锁（AST）：纠偏 → 终局 → 去重，顺序照生产")
    tree = ast.parse((ROOT / "agent" / "react_arm.py").read_text(encoding="utf-8"))
    # ⚠️ 锁的落点是**决策主体**（`_planner_decide`），不是 `planner_node`：20261019
    # （资产对齐 G）把节点拆成"薄壳 + 主体"（生产 `graph.planner_node` 同款）之后，
    # 这三处调用住在主体里。只认名字会让这条锁**当场变空**，而"锁变空"与"接线掉了"
    # 在这里看不出区别 ⇒ 两件一起判（主体在，且壳恰好调它一次）。
    def _calls_in(node: ast.AST, name: str) -> list[int]:
        return [n.lineno for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == name]

    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == "_planner_decide"), None)
    shell = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "planner_node"), None)
    check("决策主体 `_planner_decide` 找得到", fn is not None)
    check("薄壳 `planner_node` **恰好调它一次**（壳还在，锁就没变空）",
          shell is not None and len(_calls_in(shell, "_planner_decide")) == 1,
          "" if shell is None else str(_calls_in(shell, "_planner_decide")))
    if fn is None:
        return

    def _at(name: str) -> list[int]:
        return _calls_in(fn, name)

    # 纠偏那一格 20261019 起住在 `_drop_correct_stage` 里（`_planner_decide` 的分支数
    # 压在复杂度闸上）⇒ 主体里认的是**那次调用**，而判据本体仍要落在那个函数上
    # （只认调用点会让"函数里换成空实现"照样绿）。
    corr, term, dedupe = _at("_drop_correct_stage"), _at("unaccounted"), _at("finalize_dedupe")
    _dc = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "_drop_correct_stage"), None)
    check("纠偏正文由 `_drop_correct_stage` 里那一处产出（调用点后面站着真函数）",
          _dc is not None and bool(_calls_in(_dc, "drop_correction")),
          "" if _dc is None else str(_calls_in(_dc, "drop_correction")))
    check("三处都调了", bool(corr) and bool(term) and bool(dedupe),
          f"corr={corr} term={term} dedupe={dedupe}")
    check("**纠偏在终局之前**（先给它一次改的机会，改不动才收尾）",
          bool(corr) and bool(term) and max(corr) < min(term), f"{corr} {term}")
    check("**终局在去重之前**（生产：`_finalize_unaccounted` → `_finalize_dedupe`）",
          bool(term) and bool(dedupe) and max(term) < min(dedupe), f"{term} {dedupe}")
    check("终局那一支**返回的是收尾计划**（不是 `None`——`None` 等于这一格没接）",
          "if _unacct is not None" in ast.unparse(fn))


def main() -> int:
    test_correct_once()
    test_terminal()
    test_control()
    test_order()

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
