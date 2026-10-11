# -*- coding: utf-8 -*-
"""跨轮台账＝循环的续跑判据（20261012，用户点名的现场：「多步骤任务完成情况依旧稀烂…
现在根治，架构上解决」）。

**病灶一句话**：循环的续跑判据读的是"这一轮那句话"（`_pending_intents`——对当轮那句话做
**具名别名**扫描，写意图它结构上看不见），而剩余工作住在跨轮台账（`agent_task` 表）里
——两本账不同源。于是"主人一句话两件写、第一件办完"的那一轮，台账明写着还剩一件，
路由却看见空清单、直去 narrator，而 narrator 只能如实写「降级还没做。**要现在继续给他
降成「杂鱼」吗？你说一声我就去办。**」——把系统自己已经登记、参数已备的剩余退回去让
主人重说一遍（现场 `logs/agent/traces/20261012/054330`；五天内五次，两次是主人自己
发现漏了：「两个你漏了」/「你是不是漏掉一个待办」）。

**本批的规则**：台账里还有可执行的下一步 ⇒ 这一轮不许收尾。台账从"提示词里的一段散文"
升成**循环的续跑变量**，判据只有一条纯函数（`tasks.next_actionable`），三处只是读它。

本套件锁四件事（前三条是判据，第四条是**接线锁**——本仓最忌的失效形状是"路由拿不到
config ⇒ 静默退化成老行为"）：

  ① `next_actionable` 的形状（五格）；
  ② **接线锁**：`route_after_execute` 在"config 里台账还剩一件"时去 **planner**；
  ③ `AGENT_TASK_STATE=0` ⇒ 本批新增判据逐字节不动（`_ledger_next` 恒 None）；
  ④ **不新增第九条纠偏通道**（planner 那处是**判据替换**，同一个单发锁存器）。

用法：.venv/bin/python tests/test_task_continuation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent.tasks as T  # noqa: E402
import agent.graph as G  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


class _Dial:
    """把 `AGENT_TASK_STATE` 拨到指定档位（退出时**必须**还原）。

    本仓纪律：开关关着的那一档要**逐字节退回**本批之前的行为，所以两档都要跑；
    而离线套件默认钉的是出厂档（`tests/run_all.py::_PINNED` 里 `AGENT_TASK_STATE=0`）
    ——临时拨开关只许在这一段里，别泄漏给后面的用例（那会让下一节读到"开着"的档）。
    """

    def __init__(self, on: bool):
        self.on = on

    def __enter__(self):
        self.old = G.settings.agent_task_state
        G.settings.agent_task_state = self.on
        return self

    def __exit__(self, *exc):
        G.settings.agent_task_state = self.old
        return False


def _row(task_id="at_1", goal="把 demo 的身份降级为杂鱼", steps=None,
         cursor=0, state="running", pending_question=""):
    """一行台账（形状与 `tasks.frame_payload` 逐键对齐）。"""
    steps = steps if steps is not None else [{"label": "set_account_role",
                                              "tool": "set_account_role"}]
    return {"task_id": task_id, "goal": goal, "steps": steps,
            "total_steps": len(steps), "cursor": cursor, "state": state,
            "pending_question": pending_question, "idempotency_key": "tk_x"}


def _cfg(rows):
    return {"configurable": {"open_tasks": rows}}


# ══════════════════════════════════════════════════════════════════════════
print("① `tasks.next_actionable`：台账里下一件现在就能动手的事（纯函数）")

check("无行 ⇒ None", T.next_actionable([], []) is None)
check("形状坏（不是 list）⇒ None（不阻断对话）",
      T.next_actionable("不是台账", []) is None)

_r = _row()
_got = T.next_actionable([_r], [])
check("有可执行的一步 ⇒ 返回 (行, 那一步)",
      bool(_got) and _got[0]["task_id"] == "at_1"
      and _got[1]["tool"] == "set_account_role")

# ② 格：pending_question 非空的行**跳过**——那行正等着主人回话，这一轮本就该停下问
check("pending_question 非空 ⇒ 跳过（那行在等主人回话）",
      T.next_actionable([_row(pending_question="要降成哪个身份？")], []) is None)

# ① 格：终态行没有"下一步"可言
check("终态行（succeeded）⇒ 跳过",
      T.next_actionable([_row(state="succeeded")], []) is None)
check("终态行（cancelled）⇒ 跳过",
      T.next_actionable([_row(state="cancelled")], []) is None)

# ③ 格：游标位那步没有机器可读的工具名 ⇒ 不知道下一步调什么
check("游标位那步没有 tool ⇒ 跳过（续跑无从谈起）",
      T.next_actionable([_row(steps=[{"label": "待定", "tool": ""}])], []) is None)

# 游标：复用结算函数（`advance_by_receipts`）的结论，不另写一套"这步做了没"
_two = _row(steps=[{"label": "发通知", "tool": "send_user_notice"},
                   {"label": "降级", "tool": "set_account_role"}])
_got2 = T.next_actionable([_two], [{"tool": "send_user_notice", "ts": 1.0}])
check("第一步本轮回执做过了 ⇒ 推的是**第二步**（不重复推）",
      bool(_got2) and _got2[1]["tool"] == "set_account_role")
check("  同一份回执没能推进它时，游标位那步照旧是可执行的那一件",
      (T.next_actionable([_two], [{"tool": "别的工具", "ts": 1.0}]) or ({}, {}))[1]
      .get("tool") == "send_user_notice")

_multi = [_row(task_id="at_a", pending_question="要降成哪个身份？"),
          _row(task_id="at_b", goal="把文章 16 的标签加上 Git")]
check("一行在等回话、另一行能动手 ⇒ 返回**能动手的那一行**",
      (T.next_actionable(_multi, []) or ({}, {}))[0].get("task_id") == "at_b")

# ══════════════════════════════════════════════════════════════════════════
print("\n② 接线锁：`route_after_execute` 在台账还剩一件时去 planner")

# 确认兑现轮：零 LLM 拼令牌、执行完第一件。台账里还剩第二件 ⇒ 不许直去 narrator。
_state = {"confirm_grant": True, "blocked": [], "receipts": [], "messages": []}
with _Dial(True):
    check("台账还剩一件 ⇒ planner（不是 model）",
          G.route_after_execute(_state, _cfg([_row()])) == "planner")
    check("台账空 ⇒ 老行为 model",
          G.route_after_execute(_state, _cfg([])) == "model")
    check("台账里只有在等回话的行 ⇒ model（本来就该停下来问）",
          G.route_after_execute(_state, _cfg([_row(pending_question="降成哪个？")]))
          == "model")
    check("config 里没有 open_tasks 这一格 ⇒ model（不因为读不到就改道）",
          G.route_after_execute(_state, {}) == "model")
    check("config 是 None ⇒ model（路由的手上真可能没有它）",
          G.route_after_execute(_state, None) == "model")
    check("非确认轮不受影响（那是原本就回 planner 的一支）",
          G.route_after_execute({"blocked": [], "messages": []}, _cfg([_row()]))
          == "planner")
    check("受阻轮不受影响（blocked 非空 ⇒ 不走台账那一支）",
          G.route_after_execute({"confirm_grant": True, "blocked": [{"tool": "x"}],
                                 "messages": []}, _cfg([_row()])) == "planner")

# ══════════════════════════════════════════════════════════════════════════
print("\n③ `AGENT_TASK_STATE=0` ⇒ 本批新增判据逐字节不动")

with _Dial(False):
    check("路由：台账还剩一件也仍是 model（关档 = 本批之前的行为）",
          G.route_after_execute(_state, _cfg([_row()])) == "model")
    check("`_ledger_next` 恒 None",
          G._ledger_next(_state, _cfg([_row()])) is None)
    check("`_ledger_has_next` 恒 False",
          G._ledger_has_next(_state, _cfg([_row()])) is False)
    check("gate 的账乙网恒不命中（哪怕是再露骨的征询句）",
          G._ledger_continuation_issue("要不要我现在就继续？你说一声我就去办。",
                                       {"task_frames": [{"task_id": "at_1"}]},
                                       _cfg([_row()])) is None)

# ══════════════════════════════════════════════════════════════════════════
print("\n④ gate 的账乙网（接线点 3）：征询式收尾 ⇒ 回 planner")

_IN = {"task_frames": [{"task_id": "at_1"}]}       # 本轮就在那件事的工作面上
_SOLICIT = ("至于降级身份那一步——这次只执行了发通知，降级还没做。"
            "**要现在继续给他降成「杂鱼」吗？你说一声我就去办。**")
with _Dial(True):
    _iss = G._ledger_continuation_issue(_SOLICIT, _IN, _cfg([_row()]))
    check("命中 ⇒ 返回 (issue, 兜底文案, 子句)",
          bool(_iss) and _iss[0] == "ledger_continuation_pending"
          and bool(_iss[1]) and bool(_iss[2]))
    check("  该族在 `_REPLAN_ISSUES` 里（否则打回时一步到兜底）",
          "ledger_continuation_pending" in G._REPLAN_ISSUES)
    check("  自备 `_REPLAN_ADVICE`（默认那份让 planner 去检索，是错的出路）",
          "ledger_continuation_pending" in G._REPLAN_ADVICE)
    check("  自备 `_REPLAN_WHY`（默认那句「这一轮一个工具都没有执行」在这里是假话）",
          "ledger_continuation_pending" in G._REPLAN_WHY)
    check("  建议里明写**不许**再问「要不要继续」",
          "不许" in " ".join(G._REPLAN_ADVICE["ledger_continuation_pending"])
          and "要不要我继续" in " ".join(G._REPLAN_ADVICE["ledger_continuation_pending"]))

    check("诚实叙述（没有把主动权退回去）⇒ 不命中",
          G._ledger_continuation_issue(
              "通知已经发出去了，接下来我接着把降级办掉。", _IN, _cfg([_row()])) is None)
    check("台账里没有可执行的剩余 ⇒ 不命中（词形这一层不单独成立）",
          G._ledger_continuation_issue(
              _SOLICIT, _IN, _cfg([_row(pending_question="降成哪个？")])) is None)
    check("不在工作面（无 confirm_grant、本轮无 task_frames）⇒ 不命中",
          G._ledger_continuation_issue(_SOLICIT, {}, _cfg([_row()])) is None)
    check("`gate_replan` 已为真 ⇒ 不命中（第二次进去不许把诚实的回复换成道歉）",
          G._ledger_continuation_issue(_SOLICIT, {**_IN, "gate_replan": True},
                                       _cfg([_row()])) is None)
    check("确认轮（`confirm_grant`）也在工作面里",
          bool(G._ledger_continuation_issue(
              _SOLICIT, {"confirm_grant": True}, _cfg([_row()]))))

# 三张表的口径锁（同族：家族必须三处都挂号）
check("`_REPLAN_ADVICE` 的键都是 `_REPLAN_ISSUES` 的成员",
      set(G._REPLAN_ADVICE) <= set(G._REPLAN_ISSUES))
check("`_REPLAN_WHY` 的键都是 `_REPLAN_ISSUES` 的成员",
      set(G._REPLAN_WHY) <= set(G._REPLAN_ISSUES))

# ══════════════════════════════════════════════════════════════════════════
print("\n⑤ 源码锁：本批**没有**新增第九条纠偏通道")

_SRC = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
_LATE = _SRC[_SRC.index("def _decide_late_correct("):]
_LATE = _LATE[:_LATE.index("\ndef ")]
_GUARD = 'if (not c.correction and c.has_frames and not c.plan_obj["tools"]'
# 这一格里的**并列通道数**（每一条以 `not c.correction` 起头的守卫 = 一条通道，
# 共用同一枚单发锁存器）：参数不齐 / 写形态·动作重复 / 收尾丢意图 / 菜单摘项后放弃 /
# 写命令零调用。**本批不许把这个数变大**——`收尾丢台账` 是"收尾丢意图"那条的
# **判据替换**（同一个守卫之内多了一个 `if _led:` 分支），不是第九条通道。
_GUARDS = [ln for ln in _LATE.splitlines()
           if ln.strip().startswith("if") and "not c.correction" in ln]
check("`_decide_late_correct` 里的并列纠偏通道仍是 5 条（本批不新增第九条）",
      len(_GUARDS) == 5, f"读到 {len(_GUARDS)} 条")

# 两条判据（当轮那句话 / 跨轮台账）住在同一个函数里、走同一个 `c.correction` 槽
# ——"并集"而不是"第九条通道"这句主张的**可执行形态**就在这里。
_HELP = _SRC[_SRC.index("def _wrapup_intent_correct("):]
_HELP = _HELP[:_HELP.index("\ndef ")]
check("planner 那处落在**同一个守卫**之内（收尾丢意图那一格，不是并列通道）",
      _LATE.count(_GUARD) == 1
      and _LATE.count("_wrapup_intent_correct(c)") == 1
      and _LATE.index(_GUARD) < _LATE.index("_wrapup_intent_correct(c)"))
check("  两条判据在同一个函数里（`收尾丢意图` 与 `收尾丢台账` 都走 `c.correction`）",
      'c.correction_kind = "收尾丢意图"' in _HELP
      and 'c.correction_kind = "收尾丢台账"' in _HELP
      and _HELP.count('return _Step("continue")') == 2)
check("  台账那一支读的是 `_ledger_next`（与路由、gate 同一句判据）",
      _HELP.count("_ledger_next(c.state, c.config)") == 1)
check("接台账用的是既有纠偏槽 `c.correction`（不新增预算、不与 MAX_PLAN_ROUNDS 叠乘）",
      _HELP.count("c.correction = _ledger_continue_nudge(") == 1
      and "for _attempt" not in _HELP)
check("接线点 3 的两个 PASS 出口都调了账乙网（少一处 = 那一族漏网）",
      _SRC.count("_ledger_continuation_issue(reply, state, config)") == 2)
check("`_ledger_continue_nudge` 只有一处定义",
      _SRC.count("def _ledger_continue_nudge(") == 1)
check("`graph.py` 里没有真的写下 `from __future__ import annotations`"
      "（会让条件边的 config 注入静默失效；头注第 53 行只是那条纪律的说明）",
      not any(ln.strip().startswith("from __future__ import annotations")
              for ln in _SRC.splitlines()))

# ══════════════════════════════════════════════════════════════════════════
print()
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print("✅ 全绿")
