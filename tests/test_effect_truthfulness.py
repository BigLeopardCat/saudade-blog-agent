# -*- coding: utf-8 -*-
"""洞⑪ 第二半：零帧轮的**特效/夜间模式状态**声称 → 与实时上报核对（纯离线）。

与 `test_nav_truthfulness.py` 的第 ④ 组同族：那半核的是 `page=`（主人现在在哪一页），
这半核的是同一条系统上下文里的 `current_effects=` / `current_darkmode=`（浏览器实时
上报的开关状态）。两族**同一条判据形状**——不猜词形、拿真值核，一致即放行。

**为什么这一族必须双向锁**（与页面那半同一条理由）：洞① ⑤ 当年刻意把开合类动词
（"樱花特效已经开启啦"）排除在词形判据之外，因为存在**合法的幂等轮**——主人要开的
特效本来就开着，零帧轮叙述"已经开启啦"是真话。所以本文件的回归锁一半是反例面
（真值说没开、回复说开了 ⇒ 判假），另一半是正例面（**真值一致 ⇒ 同一句话必须放行**）。
只锁反例面的话，"把词形放得足够宽"也能让测试变绿，而那正是本族要离开的路。

上线前复扫（全量真实 trace，判据按 gate 自己的 `zero_frame` 分列）：1177 份 trace /
667 份有回复与页面上下文 / 其中 **167 个零帧轮 ⇒ 本判据 0 命中**（另 3 个零帧以下的
命中全是**有帧轮**，见下面第五组那条"有帧轮不跑本族"的锁——那不是漏，是必须）。

用法：.venv/bin/python tests/test_effect_truthfulness.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
from agent.graph import (  # noqa: E402
    _FALLBACK_EFFECT_NO_FRAME, _claim_issue, _effect_state_claim,
    _effect_state_claim_clause, _live_darkmode, _live_effects,
    gate_node, parse_plan, plan_encode,
)
from agent.principal import Principal  # noqa: E402
from agent.skills import instantiate_plan  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ── 页面上下文的四条夹具（真值各不相同；字段顺序照 `server.py::_build_messages`）──
NONE_ = ("user_id=1, page=/, title=Saudade Blog; current_effects=none; "
         "current_darkmode=off")
SAKURA = ("user_id=1, page=/, title=Saudade Blog; current_effects=sakura; "
          "current_darkmode=off")
DARK = ("user_id=1, page=/, title=Saudade Blog; current_effects=none; "
        "current_darkmode=on")


def _sys_msg(ctx=NONE_):
    """`_build_messages` 写进消息流的那条 `[System: …]`（`_page_ctx` 的取值处）。"""
    return HumanMessage(content=f"[System: {ctx}]")


def _chat_state(msgs, **kw):
    """零帧 chat 轮的 gate 输入（本族只跑在零帧族表里）。`gate_replan=True` = 重规划
    那次已经用掉 ⇒ 打回走确定性兜底，断言能直接读到兜底文本。"""
    st = {"plan": plan_encode(instantiate_plan("chat", {})), "messages": msgs,
          "done": False, "plan_rounds": 2, "gate_replan": True}
    st.update(kw)
    return st


def test_live_truth_parsing():
    """① 真值解析：`none`/空 = **真值**（一个都没开），缺席/脏值 = **没有真值**。"""
    print("[洞⑪b] page_ctx 的真值解析")
    check("`current_effects=none` ⇒ 空集（能判假）",
          _live_effects(NONE_) == set())
    check("逗号分隔的多特效 ⇒ 集合（幂等判定要集合语义，不是字符串相等）",
          _live_effects("current_effects=sakura,rain") == {"sakura", "rain"})
    check("字段缺席 ⇒ None（无从核对，整族不判）",
          _live_effects("user_id=1, page=/") is None
          and _live_darkmode("user_id=1, page=/") is None)
    check("值一个都不认识（前端报了不建模的东西/脏值）⇒ None，"
          "**不许**当成'这些都没开'（那会把'樱花打开啦'判成假）",
          _live_effects("current_effects=?") is None
          and _live_effects("current_effects=fog") is None)
    check("认识的那个留下、不认识的那个丢掉",
          _live_effects("current_effects=sakura,fog") == {"sakura"})
    check("夜间模式 on/off ⇒ True/False，取值不认识 ⇒ None",
          _live_darkmode(DARK) is True and _live_darkmode(NONE_) is False
          and _live_darkmode("current_darkmode=maybe") is None)
    check("取真值时**不会**被同一段里的别的字段截胡（effects 的值在 `;` 前收住）",
          _live_effects("current_effects=none; current_darkmode=on") == set())


def test_fabricated_state_claim_judged():
    """② 反例面：真值说没开、回复说"已经打开了" ⇒ 判假（洞① 当年放掉的那一格）。"""
    print("[洞⑪b] 编造的开合声称（与实时上报不符）→ 兜底")
    human = HumanMessage(content="我有点想看樱花")
    # 措辞刻意**不带施事前缀**（"帮你/给你"）也**不带"把/将"**：那两种形态归洞① 的
    # ①②③ 支管（族表按顺序判，第一族记名），本族接的是它们**结构性漏掉**的那一格
    # ——"樱花特效已经开启啦"正是洞① ⑤ 当年为幂等轮刻意放过的那句。
    reply = "主人，樱花特效已经开启啦～满屏都是花瓣哦！"
    o = gate_node(_chat_state([_sys_msg(NONE_), human, AIMessage(content=reply)]))
    check("零帧轮 + `current_effects=none` + '已经开启啦' → 兜底",
          o.get("done") is True and o.get("fallback_text") == _FALLBACK_EFFECT_NO_FRAME,
          str(o.get("fallback_text"))[:36])

    iss = _claim_issue(reply, "chat", parse_plan(_chat_state([])["plan"]), False,
                       page_ctx=NONE_)
    check("issue = effect_state_claim_without_cmd（分族记，能按族统计）",
          bool(iss) and iss[0] == "effect_state_claim_without_cmd",
          str(iss and iss[0]))
    check("被否掉的子句 = 那一句（进 trace）",
          bool(iss) and "樱花特效已经开启啦" in iss[2], str(iss and iss[2])[:60])

    o2 = gate_node(_chat_state([_sys_msg(DARK), human,
                                AIMessage(content="主人，夜间模式已经关掉啦")],
                               gate_replan=False))
    check("首次打回 → 交回 planner 重规划一次（不是直接道歉收尾）",
          o2.get("gate_replan") is True and o2.get("done") is False, str(o2.get("done")))
    _note = "".join(str(getattr(m, "content", "")) for m in (o2.get("messages") or []))
    check("  提示里点明'现在开着什么'是系统事实（带那两个字段名）",
          "系统事实" in _note and "current_effects" in _note, _note[:60])
    check("  提示里的禁止句在（不许说'已经打开了'）", "已经打开了" in _note)


def test_idempotent_and_truthful_pass():
    """③ 正例面：真值**一致** ⇒ 同一句话必须放行（幂等轮的真话）。"""
    print("[洞⑪b] 真值一致 / 提议 / 能力罗列 → 放行")
    human = HumanMessage(content="樱花开了吗")
    for ctx, text, why in (
        (SAKURA, "主人，樱花特效已经开启啦～", "状态本来就是目标值（幂等轮）"),
        (SAKURA, "樱花特效本来就是开着的哦，不用再开一次啦", "如实转述当前状态"),
        (NONE_, "主人，樱花特效现在是关着的，要开吗？", "真话说'关着'"),
        (DARK, "主人，夜间模式已经打开了呀", "夜间真值一致"),
    ):
        o = gate_node(_chat_state([_sys_msg(ctx), human, AIMessage(content=text)]))
        check(f"{why} ⇒ 放行：" + text[:22],
              o.get("done") is True and not o.get("fallback_text"), str(o)[:50])

    # 提议/条件/能力/疑问/引述（与页面那半共用一张豁免表）
    for text in ("我可以帮你打开樱花特效的喵～",
                 "要不要我把樱花特效打开呢？",
                 "如果主人想开夜间模式，说一声就行",
                 "把樱花特效打开的话，页面上就会飘花瓣啦",
                 "主人你刚才说「把樱花特效关掉」对吧？",
                 "樱花特效的开关在右上角的按钮里"):
        o = gate_node(_chat_state([_sys_msg(NONE_), human, AIMessage(content=text)]))
        check("不是声称 ⇒ 放行：" + text[:20],
              o.get("done") is True and not o.get("fallback_text"), str(o)[:50])


def test_no_truth_and_guards():
    """④ 没有真值 / 完成态缺席 / 追述 ⇒ 不判（宁漏勿误伤的三条护栏）。"""
    print("[洞⑪b] 护栏：没有真值 / 完成态 / 追述豁免")
    human = HumanMessage(content="帮我开个樱花")
    for ctx, why in ((None, "整条 [System:] 缺席"),
                     ("user_id=1, page=/", "两个字段都缺"),
                     ("user_id=1, page=/; current_darkmode=on", "只缺特效真值"),
                     ("user_id=1, page=/; current_effects=?", "特效值不认识")):
        msgs = [human, AIMessage(content="樱花特效已经打开啦")] if ctx is None else \
            [_sys_msg(ctx), human, AIMessage(content="樱花特效已经打开啦")]
        o = gate_node(_chat_state(msgs))
        check(f"{why} ⇒ 不判，放行",
              o.get("done") is True and not o.get("fallback_text"), str(o)[:50])
    o = gate_node(_chat_state([_sys_msg("user_id=1, page=/; current_darkmode=on"),
                               human, AIMessage(content="夜间模式已经关掉啦")]))
    check("只缺特效真值时不牵连夜间那族（两族各判各的真值）",
          o.get("done") is True and o.get("fallback_text") == _FALLBACK_EFFECT_NO_FRAME,
          str(o.get("fallback_text"))[:36])

    for text in ("主人，我把樱花特效打开？", "你可以把夜间模式关掉试试"):
        o = gate_node(_chat_state([_sys_msg(NONE_), human, AIMessage(content=text)]))
        check("完成态缺席（提议/指路，不是声称）⇒ 放行：" + text[:16],
              o.get("done") is True and not o.get("fallback_text"), str(o)[:50])
    o = gate_node(_chat_state(
        [_sys_msg(NONE_), human, AIMessage(content="樱花特效刚才已经打开了")],
        ledger={"executions": [{"detail": "特效「樱花」打开"}]}))
    check("回执在场 + 追述时间词 = 引台账（rule 6）⇒ 放行",
          o.get("done") is True and not o.get("fallback_text"), str(o)[:50])
    o = gate_node(_chat_state([_sys_msg(NONE_), human,
                               AIMessage(content="樱花特效刚才已经打开了")]))
    check("  同句**无回执**在场 ⇒ 空手套不豁免，仍判假",
          o.get("fallback_text") == _FALLBACK_EFFECT_NO_FRAME,
          str(o.get("fallback_text"))[:36])


def test_frame_round_not_in_scope():
    """⑤ 本族**只跑零帧轮**，且这是必须的——同一个形状在有帧轮是**真话**。

    复扫实测（20260927T021605 / 20260927T074831 两条真实 trace）：那一轮
    `toggle_effect(sakura, on)` 真的执行了、回执写着"特效 樱花(sakura) 已打开"，而
    `page_ctx` 的 `current_effects=` 仍是 **none**——因为它是**请求时**的值（命令在
    回复到达之后才被前端执行）。所以"有帧轮的同类声称"**不能**照这一族直接扩，
    否则每一次成功的开合都会被判假。本锁把这个边界钉住。
    """
    print("[洞⑪b] 有帧轮不入本族（判据在零帧族表里，有帧从结构上到不了）")
    human = HumanMessage(content="帮我开樱花")
    frame = G.ToolMessage(content="特效 樱花(sakura) 已打开", tool_call_id="t1")
    o = gate_node(_chat_state([_sys_msg(NONE_), human, frame,
                               AIMessage(content="主人，樱花特效已经打开啦～")],
                              receipts=[{"skill": "effect", "tool": "toggle_effect",
                                         "args": {"effect": "sakura", "action": "on"},
                                         "result": "特效 樱花(sakura) 已打开"}]))
    check("有工具帧那轮（回执在场）⇒ 本族不判，放行",
          o.get("done") is True and not o.get("fallback_text"), str(o)[:50])


def test_family_wiring():
    """⑥ 接线锁：族名进了重规划表、advice 有对应条目、两个谓词都认同一句。"""
    print("[洞⑪b] 接线（族表 / 重规划 / 谓词是同一个）")
    fams = {f.issue: f for f in G._zero_frame_families(parse_plan(
        plan_encode(instantiate_plan("chat", {}))), "chat")}
    check("族表里有 effect_state_claim_without_cmd",
          "effect_state_claim_without_cmd" in fams)
    fam = fams.get("effect_state_claim_without_cmd")
    check("  它的 needs 要 page_ctx（真值来源）与 exec_memory（追述豁免）",
          fam is not None and tuple(fam.needs) == ("page_ctx", "exec_memory"),
          str(fam and fam.needs))
    check("  兜底文案是特效那半的（不是页面那半的）",
          fam is not None and fam.fallback == _FALLBACK_EFFECT_NO_FRAME)
    check("族名在 `_REPLAN_ISSUES`（否则拦住之后直接道歉收尾，事情没人办）",
          "effect_state_claim_without_cmd" in G._REPLAN_ISSUES)
    check("  `_REPLAN_ADVICE` 有对应条目（提示词按族分叉）",
          "effect_state_claim_without_cmd" in G._REPLAN_ADVICE)
    check("布尔壳与子句壳判的是同一件（族表按 bool 调、trace 按子句记）",
          _effect_state_claim("樱花特效已经打开啦", NONE_) is True
          and bool(_effect_state_claim_clause("樱花特效已经打开啦", NONE_)) is True)


def main():
    for fn in (test_live_truth_parsing,
               test_fabricated_state_claim_judged,
               test_idempotent_and_truthful_pass,
               test_no_truth_and_guards,
               test_frame_round_not_in_scope,
               test_family_wiring):
        fn()
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项不通过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
