"""narrator 的消息窗口（批 3）：纯函数判据 + 反向对照。

**为什么有这个套件**：接上持久化 saver、线程改成会话坐标之后，`state["messages"]`
是"这个会话开天辟地以来所有条"，而 `model_node` 那句
`with_tool_call_pairs(state["messages"])` **无截断** ⇒ narrator 的提示词随会话线性
膨胀（判决文档 §4.6；`graph.py` 的 `prompt_oversize` 埋点就在那儿）。同一时刻，
`server._build_messages` 每轮**注入**的 `history[-20:]` 变成重复——历史被注入 r 次。

批 3 的做法是**替代**而不是删除：图自己持有历史之后，把 `_build_messages` 一直在
悄悄提供的三样东西补回图里（**封顶** / **剥事实行** / **剪孤儿 user**），住在
`prompt_context.narrator_messages` 里，`model_node` 走 `graph._narrator_msgs` 调它。

**无 saver 时窗口是恒等**（`turn_start` 是 `0`/`-1`）⇒ §①② 是那条负控：判据要的是
**同一个对象**（`is`），不是"内容相同"——批 1 的 `test_turn_window.py` §① 同款，
理由见 `prompt_context.turn_window` 的 docstring。

判据分六组：
  ① 负控：闸门关着（没记边界 / 起点在表头 / `turns<=0`）⇒ **同一个对象**；
  ② 负控：臂 A 的真形状逐条 `is`（含 `graph_input` 这条真入口）；
  ③ 正控：脏清单（事实行 + 孤儿 user + 超 K 轮 + 工具帧）**必须真变**；
  ④ `_prev_user_msg` 的三形态：对臂 A **逐字节等于旧实现**，对臂 C 取到主人上一句；
  ⑤ 反向对照：三种改坏（`k<0` / 剪枝范围扩到全表 / `turns=0`）都必须被抓住；
  ⑥ 幂等：剥事实行对**已被剥过**的历史是 no-op（注入历史走的就是这条）。
"""


import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根


sys.path.insert(0, str(ROOT))


from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from agent.factblock import FACT_MARK, strip_fact_lines  # noqa: E402
from agent.graph import _narrator_msgs, graph_input  # noqa: E402
from agent.prompt_context import (  # noqa: E402
    _NARRATOR_TURNS,
    _is_system_block,
    _msg_text,
    _prev_user_msg,
    _recent_turn_start,
    narrator_messages,
    prune_orphan_users,
    strip_fact_messages,
)

FAILS: list[str] = []
check = collector(FAILS)


def sysblk(n: int) -> HumanMessage:
    """服务端每轮注入的那条上下文块（形状照 `server._build_messages` 的 `ctx`）。"""
    return HumanMessage(content=f"[System: user_id=7; page=/p{n}; current_time=T{n}]")


def q(n: int) -> HumanMessage:
    return HumanMessage(content=f"[当前问题]: Q{n}")


def a(n: int) -> AIMessage:
    return AIMessage(content=f"A{n}")


def tm(i: int) -> ToolMessage:
    return ToolMessage(content=f"结果{i}", tool_call_id=f"execute_{i}", name="search_notes")


def arm_a() -> list:
    """臂 A 的真形状：`[System 块] + history[-20:]（只有 user/assistant 文本） + 当前问题`。

    逐字照 `server._build_messages` 的组装（`server.py:692` 的 ctx 块在表头、
    `:711` 的 `req.history[-20:]` 在中间、`:752` 的 `[当前问题]:` 在表尾）。
    """
    return [sysblk(1), HumanMessage(content="Q1"), AIMessage(content="A1"),
            HumanMessage(content="Q2"), AIMessage(content="A2"), q(3)]


def arm_c(turns: int = 3, frames: bool = True) -> list:
    """臂 C 的真形状（线程复用）：每轮 = `[Sys, Q, 工具帧…, AI]`，最后是**本轮**的 `[Sys, Q]`。"""
    msgs: list = []
    for i in range(1, turns):
        msgs += [sysblk(i), q(i)]
        if frames:
            msgs += [tm(i * 10), tm(i * 10 + 1)]
        msgs += [a(i)]
    msgs += [sysblk(turns), q(turns)]
    return msgs


def _old_prev_user_msg(messages: list) -> str:
    """**改前**的实现（20260927 那版，逐字抄自 `git show HEAD:agent/prompt_context.py`）。

    拿它当参照物，而不是拿"我记得它返回什么"当参照物——"对臂 A 零变化"这条判据
    只有这样才是可复核的。
    """
    seen = 0
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            seen += 1
            if seen == 2:
                return _msg_text(m)[-500:]
    return ""


def _variant_strict(messages, turn_start, turns=_NARRATOR_TURNS) -> list:
    """改坏 1：闸门条件 `k <= 0` → `k < 0`（第一轮也去切）。"""
    msgs = [] if messages is None else messages
    k = -1 if turn_start is None else turn_start
    if k < 0 or turns <= 0:
        return msgs
    start = _recent_turn_start(msgs, k, turns - 1)
    hist = [m for m in prune_orphan_users(strip_fact_messages(msgs[start:k]))
            if not isinstance(m, ToolMessage)]
    return hist + msgs[k:]


def test_narrator_window():
    print("[1] 负控：闸门关着 ⇒ 返回**同一个对象**（`is`，不是 `==`）")
    _A = arm_a()
    for desc, ts, turns in (("没记边界 turn_start=-1", -1, _NARRATOR_TURNS),
                            ("起点在表头 turn_start=0", 0, _NARRATOR_TURNS),
                            ("None", None, _NARRATOR_TURNS),
                            ("turns=0（窗口关掉的反向对照口）", 3, 0),
                            ("turns=-1", 3, -1)):
        check(f"{desc} ⇒ 恒等", narrator_messages(_A, ts, turns=turns) is _A)
    check("messages=None ⇒ 空表（不是 None）", narrator_messages(None, -1) == [])
    for desc, fill in (("空表", []), ("单条", [HumanMessage(content="在吗")])):
        check(f"装填量「{desc}」上也恒等", narrator_messages(fill, -1) is fill)
    print("[2] 负控：臂 A 的**真形状**逐条同一对象（不是「内容相同」）")
    _win_a = narrator_messages(_A, -1)
    check("逐条 `is`（6/6）", len(_win_a) == len(_A) and all(x is y for x, y in zip(_win_a, _A)))
    _st = graph_input(list(_A))
    check("graph_input 这条真入口：turn_start 初值 ⇒ 恒等",
          _st.get("turn_start") == -1 and _narrator_msgs(_st) is _st["messages"])
    check("`graph._narrator_msgs` 在无边界时也恒等（取值口那一层）",
          _narrator_msgs({"messages": _A, "turn_start": -1}) is _A)
    print("[3] 正控：脏清单**必须真变**")
    _C = arm_c(turns=3)
    _ts_c = next(i for i, m in enumerate(_C) if _is_system_block(m) and i > 6)
    check("fixture 里本轮的 Sys 块找得到", _is_system_block(_C[_ts_c]), f"下标 {_ts_c}")
    _win_c = narrator_messages(_C, _ts_c)
    check("真变了（不是同一个对象）", _win_c is not _C)
    check("窗口以 `[System:` 开头（切轮不切条数）", _is_system_block(_win_c[0]))
    check("本轮段原样：最后一条就是本轮的提问", _win_c[-1] is _C[-1])
    check("历史段的工具帧一条不剩",
          not any(isinstance(m, ToolMessage) for m in _win_c))
    check("历史段的文本保住了（丢的是帧，不是上下文）",
          sum(1 for m in _win_c if isinstance(m, AIMessage)) == 2)
    check("历史段每条 assistant 都在（丢帧不丢轮）",
          [m.content for m in _win_c if isinstance(m, AIMessage)] == ["A1", "A2"])
    _dirty = AIMessage(content=f"好的\n{FACT_MARK}页面已跳转：/about\n就这样")
    _C2 = [sysblk(1), q(1), _dirty, sysblk(2), q(2)]
    _w2 = narrator_messages(_C2, 3)
    check("历史段的「〔系统〕」事实行被剥掉",
          all(FACT_MARK not in (_msg_text(m) or "") for m in _w2))
    check("本轮段一个字没动（脏消息若在本轮，原样带进去）",
          narrator_messages([sysblk(1), q(1), _dirty], 0) == [sysblk(1), q(1), _dirty])
    _orphan = [sysblk(1), q(1), tm(0), a(1), sysblk(2), HumanMessage(content="Q2断流"),
               sysblk(3), q(3)]
    _w3 = narrator_messages(_orphan, 6)
    check("断流那条孤儿 user 被剪掉",
          not any((_msg_text(m) or "") == "Q2断流" for m in _w3))
    check("答过的那条留着（帧在后面也算答过）",
          any((_msg_text(m) or "") == "[当前问题]: Q1" for m in _w3))
    _long: list = []
    for i in range(1, 13):
        _long += [sysblk(i), q(i), a(i)]
    _long[-1] = q(12)
    _ts12 = 11 * 3
    _w12 = narrator_messages(_long, _ts12)
    check(f"12 轮 ⇒ 只留最近 {_NARRATOR_TURNS} 轮（30 条）", len(_w12) == 30, f"len={len(_w12)}")
    check("窗口第一轮是第 3 轮（切的是**轮**）", "page=/p3" in _w12[0].content)
    check("边界不够时退回第一个边界（不切掉任何东西）",
          narrator_messages(_long, 6, turns=_NARRATOR_TURNS) == _long)
    check("`_recent_turn_start` 取的是**边界**（一定是 Sys 块）",
          _is_system_block(_long[_recent_turn_start(_long, _ts12, _NARRATOR_TURNS - 1)]))
    print("[4] `_prev_user_msg` 三形态（臂 A 逐字节不变 / 臂 C 取到主人上一句）")
    _ARM_A_SHAPES = {
        "有历史（Sys 在表头）": _A,
        "只有本轮（[Sys, Q]）": [sysblk(1), q(1)],
        "历史里只有 user": [sysblk(1), HumanMessage(content="Q1"), q(2)],
        "没有 Sys（合成 fixture）": [HumanMessage(content="Q1"), q(2)],
        "空表": [],
        "单条": [q(1)],
    }
    for _desc, _msgs in _ARM_A_SHAPES.items():
        _new, _old = _prev_user_msg(_msgs), _old_prev_user_msg(_msgs)
        check(f"臂 A 形态「{_desc}」逐字节不变", _new
            == _old, f"new={_new[:40]!r} old={_old[:40]!r}")
    _C_form = [sysblk(1), q(1), tm(0), a(1), sysblk(2), q(2)]
    check("臂 C：取到的是**主人上一句**（Q1）",
          _prev_user_msg(_C_form) == "[当前问题]: Q1", repr(_prev_user_msg(_C_form)))
    check("臂 C：旧实现取到的是**本轮的 System 块**（这就是那个真缺陷）",
          _old_prev_user_msg(_C_form) == _msg_text(sysblk(2)),
          repr(_old_prev_user_msg(_C_form)))
    check("两条 System 块时同样取主人那句",
          _prev_user_msg([sysblk(1), q(1), sysblk(2), q(2)]) == "[当前问题]: Q1")
    print("[5] 反向对照：三种改坏都必须被抓住")
    check("改坏 1（`k < 0`）：第一轮上**不是**同一个对象 ⇒ §① 的判据抓得住",
          _variant_strict(_A, 0) is not _A
          and _variant_strict(_A, 0) == _A)  # 内容还一样，只有身份变了
    check("改坏 2（剪枝范围扩到**全表**）：本轮那条提问被剪掉 ⇒ §③ 的判据抓得住",
          _C[-1] not in prune_orphan_users(_C))
    check("改坏 3（`turns=0`）：窗口关掉 ⇒ 退回整表（膨胀必须能复现，见判决文档 §4.6）",
          narrator_messages(_long, _ts12, turns=0) is _long
          and len(narrator_messages(_long, _ts12, turns=0)) == 36)
    check("正向：真函数在同一个入参上**留住了**本轮提问",
          any(m is _C[-1] for m in narrator_messages(_C, _ts_c)))
    print("[6] 幂等：对**已被 `_build_messages` 剥过**的注入历史是 no-op")
    _once = AIMessage(content=strip_fact_lines(f"好的\n{FACT_MARK}页面已跳转：/about"))
    check("第一次剥不动它（同一个对象）", strip_fact_messages([_once])[0] is _once)
    check("`strip_fact_lines` 自身幂等",
          strip_fact_lines(_once.content) == _once.content)
    _clean_ai, _clean_hu = a(9), HumanMessage(content="x")
    check("没被动的条目返回**原对象**（assistant 无事实行 / user 一律）",
          strip_fact_messages([_clean_ai])[0] is _clean_ai
          and strip_fact_messages([_clean_hu])[0] is _clean_hu)
    _multi = AIMessage(content=[{"type": "text", "text": f"{FACT_MARK}x"}])
    check("多模态 content 数组一律不动（`_msg_text` 写回去是改内容）",
          strip_fact_messages([_multi])[0] is _multi)
    check("`prune_orphan_users` 不剪 System 块",
          sum(1 for m in prune_orphan_users(_C) if _is_system_block(m))
          == sum(1 for m in _C if _is_system_block(m)))
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 条判据未过：")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ 全部通过")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_narrator_window()
