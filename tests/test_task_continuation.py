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
_GUARD = 'if not c.correction and c.has_frames and not c.plan_obj.get("dropped")'
_CALL = "_wrapup_intent_correct(c)"
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
      and _LATE.count(_CALL) == 1
      and _LATE.index(_GUARD) < _LATE.index(_CALL))
check("  同一守卫之内还接上了「清单会被去重剪空」那一档（判据与裁剪同源）",
      "_plan_trims_to_empty(c.plan_obj, c.state)" in _HELP
      and "not c.plan_obj[\"tools\"]" in _HELP)
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
print("\n⑥ 二次接线：清单**会被去重剪空**时也要判台账（现场 081550）")
# 第一版只接了三处，生产上又冒出新的一处缝：确认轮办完第一件 → 台账续跑把这一轮交回
# planner（接线点 1 生效，两次）→ 模型又挑了**刚办完**的那件 → 零改动 → 剪空 → 收尾
# → 回复「…主人点个头，我下一轮就把它发掉喵」。剪裁跑在决策循环**之外**
# （`_finalize_dedupe`），剪完直接 `_wrap_up_plan` 走人 ⇒ 上面那条并集纠偏结构上够不到它。

_DUP = 'set_account_role({"name": "demo", "role": "zako"})'
_NEW = 'send_user_notice({"uid": 3, "text": "hi"})'
_NOOP_STATE = {"noop_specs": [list(G._spec_signature("set_account_role",
                                                    {"name": "demo", "role": "zako"}))]}

check("预演：全是被去重剪掉的（工具自报零改动）⇒ True",
      G._plan_trims_to_empty({"tools": [_DUP]}, _NOOP_STATE) is True)
check("预演：空清单 ⇒ False（「剪空」以有东西可剪为前提）",
      G._plan_trims_to_empty({"tools": []}, _NOOP_STATE) is False)
check("预演：有一件新的 ⇒ False",
      G._plan_trims_to_empty({"tools": [_DUP, _NEW]}, _NOOP_STATE) is False)
# 同源锁：预演与**真剪裁**必须同一结论（判据不另写一份"这步是不是重复"）
_real = G._trim_noop_specs({"tools": [_DUP]}, _NOOP_STATE["noop_specs"])
check("预演与真剪裁同源（同一对纯函数、同一顺序）",
      bool(_real) and not _real[0]["tools"]
      and G._plan_trims_to_empty({"tools": [_DUP]}, _NOOP_STATE))
_PIN = {"tools": [_DUP]}
G._plan_trims_to_empty(_PIN, _NOOP_STATE)
check("预演不改入参（那两个裁剪都是纯函数——就地改会让真计划当场少一件）",
      _PIN["tools"] == [_DUP])


def _ctx(**kw):
    """最小 `_DecideCtx`（数据类字段全必填，逐格给中性值）。"""
    base = dict(state={}, config=None, task_sink=None, rounds=1, user_msg="", intent_msg="",
                role="owner", page_ctx="", has_frames=True, doc_anchors=(), ledger_frame="",
                principal=None, round_info="", frames_txt="", gate_note="", deny=set(),
                _base_llm=None, _task_state=True, llm=None, deny_pseudo=set(),
                correction="", correction_kind="", native_note="", plan_obj=None,
                raw="", decided=None, skill_name="", params={}, _tools_off=False,
                _img_turn=False)
    base.update(kw)
    return G._DecideCtx(**base)


_NOTICE_ROW = _row(task_id="at_notice", goal="给 demo 发一条站内通知",
                   steps=[{"label": "发通知", "tool": "send_user_notice"}])
_FACE = {"confirm_grant": True, "receipts": [], "messages": [], "executed": [],
         "noop_specs": _NOOP_STATE["noop_specs"]}
_PLAN = {"tools": [_DUP], "skill": "account_set_role", "params": {}, "dropped": []}

with _Dial(True):
    _c = _ctx(state=dict(_FACE), config=_cfg([_NOTICE_ROW]), plan_obj=dict(_PLAN))
    _step = G._decide_late_correct(_c)
check("剪空 + 台账还剩一件 ⇒ 纠偏重决策（不是收尾）",
      _step.action == "continue" and _c.correction_kind == "收尾丢台账")
check("  指令点名下一步的工具（只说「还有事没做」＝让它自己猜）",
      "send_user_notice" in _c.correction)
check("  开场句按「排过但被剪掉」写——**不许**说成「这一轮没有排任何工具」（那是假话）",
      "已经执行过了" in _c.correction and "没有排任何工具" not in _c.correction)
check("  仍然明写不许反问「要不要继续」", "不许" in _c.correction)

with _Dial(True):
    _c0 = _ctx(state=dict(_FACE), config=_cfg([]), plan_obj=dict(_PLAN))
    _step0 = G._decide_late_correct(_c0)
check("剪空但台账没有剩余 ⇒ 照老路（break，交给 `_finalize_dedupe` 收尾）",
      _step0.action == "break" and not _c0.correction)

with _Dial(False):
    _c_off = _ctx(state=dict(_FACE), config=_cfg([_NOTICE_ROW]), plan_obj=dict(_PLAN))
    _step_off = G._decide_late_correct(_c_off)
check("`AGENT_TASK_STATE=0` ⇒ 这一格逐字节退回老行为（break、不纠偏）",
      _step_off.action == "break" and not _c_off.correction)

# 剪空档**只走台账那一支**：当轮那句话的意图清单判的是"里面的动作一次都没被规划过"，
# 与剪空（规划过、做过了）互斥，硬套会把两件事混成一条纠偏。
_old_pi = G._pending_intents
G._pending_intents = lambda st: [{"label": "收藏", "key": "favorite"}]
try:
    with _Dial(True):
        _c1 = _ctx(state=dict(_FACE), config=_cfg([_NOTICE_ROW]), plan_obj=dict(_PLAN))
        _s1 = G._wrapup_intent_correct(_c1)
        # 反控：清单**不会**被剪空时，这一格一件都不许纠（它还有真事要做）
        _c2 = _ctx(state=dict(_FACE), config=_cfg([_NOTICE_ROW]),
                   plan_obj={"tools": [_NEW], "skill": "user_notice", "params": {},
                             "dropped": []})
        _s2 = G._wrapup_intent_correct(_c2)
finally:
    G._pending_intents = _old_pi
check("剪空档不读「当轮那句话」的清单（走的是台账那一支）",
      _s1 is not None and _c1.correction_kind == "收尾丢台账")
check("  反控：清单里有真的新活儿 ⇒ 这一格不纠（不许把正常的排计划拦下来）",
      _s2 is None and not _c2.correction)

# 最后一个洞：那条征询族原本只收「**你**点个头」，生产上换个主语就漏了。
_SOLICIT_081550 = ("**还差一步**：给 demo 发一条站内通知，告诉他「由于近期活跃度不佳，"
                   "现将他降级为杂鱼」。这条通知我这边还挂着没发出去——"
                   "主人点个头，我下一轮就把它发掉喵。")
check("② 现场措辞「主人点个头」现在命中征询族",
      bool(G._LEDGER_SOLICIT_RE.search(_SOLICIT_081550)))
check("  正当的建议仍在族外（推荐**另一件**事 ≠ 推诿已在册的剩余）",
      not G._LEDGER_SOLICIT_RE.search("我还可以顺便帮你把这篇的标签也加上"))
with _Dial(True):
    check("  端到端：这段话 + 台账还剩一件 ⇒ 这条网命中",
          bool(G._ledger_continuation_issue(_SOLICIT_081550, {"confirm_grant": True},
                                            _cfg([_NOTICE_ROW]))))

# 第三次标定（同日、另一条现场）：这条族第三次撞上**主语被省掉**。
_SOLICIT_195727 = ("待办那条已经加好了喵～后台复核过，列表里确实读到了「给用户niuniu重置额度」。\n\n"
                   "接下来你要办的三件事，我按顺序排着：\n\n1. **把 niuniu 的额度重置**\n"
                   "2. **审核待审的河灯留言**\n3. **发一条公告**\n\n先做第 1 件吗？说一声我就去。")
check("③ 现场措辞「说一声我就去」现在命中征询族（前两条要求代词在场）",
      bool(G._LEDGER_SOLICIT_RE.search(_SOLICIT_195727)))
check("  裸「说一声」仍在族外（「我会说一声的」不是把主动权退回去）",
      not G._LEDGER_SOLICIT_RE.search("改完我会说一声的，你放心"))
with _Dial(True):
    check("  端到端：这段话 + 台账还剩一件 ⇒ 这条网命中（现场那三个续跑轮就是漏在它上）",
          bool(G._ledger_continuation_issue(_SOLICIT_195727, {"confirm_grant": True},
                                            _cfg([_NOTICE_ROW]))))

# ══════════════════════════════════════════════════════════════════════════
print("\n⑦ 确认兑现轮的续跑纠偏：判据改读**两本账的并集**（现场 195727）")
# 现场（`logs/agent/traces/20261012/195727`，会话 344）：一句话四件写 ⇒ 第一件弹卡 ⇒
# 主人点「确定」⇒ 执行完 ⇒ 台账还剩三件 ⇒ `route_after_execute` 如实交回 planner（三次）
# ⇒ 而这一格的判据只有 `_pending_intents`（**写意图它结构上看不见**）⇒ **一条纠偏都不给**
# （那三轮各打了一行"判据漂移？"的 WARNING）⇒ 模型把前端合成的**确认回显**当成主人的话来
# 读，回显里写的那条待办**又加了两遍**（同轮三份逐字相同的回执）。取并集之后它拿到的是
# 「回显不是主人的话 + 台账里还剩这一件、下一步调什么」。

_CARD_195727 = ("确认执行：要在后台首页的待办里加一条「给用户niuniu重置额度」，排期 未排期"
                "（那是你自己那份列表，加完随时能改删）吗？点「确定」我就去办。")
_OWNER_195727 = ("猫咪加条待办给用户niuniu重置额度，然后去把他额度重置，"
                 "然后把待审的留言你审核一下是否通过，然后再发一条公告，"
                 "以你的口吻随便说点声明")
_OWNER_ACTION = "把樱花特效打开吧"      # 反控用：这一句 `_scan_action_intents` **扫得到**
_TODO_SPEC = 'create_dashboard_todo({"text": "给用户niuniu重置额度"})'
_GRANT_195727 = {"token": "tok", "skill": "dashboard_todo_add",
                 "specs": [{"tool": "create_dashboard_todo",
                            "args": {"text": "给用户niuniu重置额度"}}]}


def _pre(resumed=True, owner=_OWNER_195727):
    from langchain_core.messages import HumanMessage
    return G._Preflight(
        rounds=1, resumed=resumed, user_msg=_CARD_195727,
        intent_msg=G._intent_src({"messages": [HumanMessage(content=owner),
                                               HumanMessage(content=_CARD_195727)],
                                  "confirm_grant": _GRANT_195727}),
        principal=None, role="owner", page_ctx="", has_frames=True, ledger_frame=""), owner


def _setup_state(owner=_OWNER_195727):
    from langchain_core.messages import HumanMessage
    return {"confirm_grant": _GRANT_195727, "executed": [_TODO_SPEC],
            "receipts": [{"skill": "dashboard_todo_add", "tool": "create_dashboard_todo",
                          "args": {"text": "给用户niuniu重置额度"}, "result": "已加", "ts": 1.0}],
            "blocked": [], "plan": "SKILL=x\nPARAMS={}\nTOOLS:\nNOTE: （无）\nREPLY: （无）",
            "plan_rounds": 1,
            "messages": [HumanMessage(content=owner), HumanMessage(content=_CARD_195727)]}


_pre_obj, _owner = _pre()


class _NoLLM:
    """装配段里那两处**与判据无关**的接线（建客户端 / 绑 native）换成桩：离线套件没有
    密钥，`get_llm` 会当场抛 `OpenAIError`（CI 里也一样）。本格判的是纠偏文本怎么拼，
    一个模型都不许被真的叫起来。"""

    def __enter__(self):
        self._g, self._b = G.get_llm, G.bind_native
        G.get_llm = lambda **kw: object()
        G.bind_native = lambda llm, *a, **kw: llm
        return self

    def __exit__(self, *exc):
        G.get_llm, G.bind_native = self._g, self._b
        return False


_QROW = _row(task_id="at_q", goal="去把niuniu额度重置",
             steps=[{"label": "重置额度", "tool": "reset_user_quota"}])
with _Dial(True), _NoLLM():
    _c = G._planner_setup(_setup_state(_owner), _cfg([_QROW]), _pre_obj, None)
check("意图清单为空 ⇒ 读台账（不是一条纠偏都不给）",
      _c.correction_kind == "确认轮台账剩余" and bool(_c.correction))
check("  点名台账那一行的目标与下一步工具",
      "去把niuniu额度重置" in _c.correction and "reset_user_quota" in _c.correction)
check("  点破消息来源：**不许**照着回显再排一遍那件事（现场重复三遍的成因）",
      "确认回显" in _c.correction and "**不要**照着回显" in _c.correction)
check("  仍然明写不许反问「要不要继续」", "不许" in _c.correction)

# 反控：主人那句话里的动作**扫得出来**时，走原来的那一支（文本逐字不变）
_pre2, _owner2 = _pre(owner=_OWNER_ACTION)
with _Dial(True), _NoLLM():
    _c2 = G._planner_setup(_setup_state(_owner2), _cfg([_QROW]), _pre2, None)
check("反控：当轮那句话扫得出意图 ⇒ 沿用原来那条（这一格没被替换掉）",
      _c2.correction_kind == "确认轮剩余意图" and "确认回显" not in (_c2.correction or ""))

with _Dial(False), _NoLLM():
    _c3 = G._planner_setup(_setup_state(_owner), _cfg([_QROW]), _pre_obj, None)
check("`AGENT_TASK_STATE=0` ⇒ 这一格逐字节退回老行为（无纠偏）",
      not _c3.correction and not _c3.correction_kind)

# ══════════════════════════════════════════════════════════════════════════
print()
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print("✅ 全绿")
