"""计划执行器（20261012 批 B）：兑现一张卡之后**零 LLM** 把下一步发出去。

**病灶**（用户点名的现场，五天内五次，形状一样）：主人一句话里三件写 ⇒ agent 识别对了、
弹卡①、主人点确定、执行① ⇒ 台账明写着还剩两件 ⇒ 路由把控制权交回 planner ⇒ **planner
请主人再说一遍**「要不要现在继续？」。这一格此前靠的是一条提示词纠偏（`_ledger_continue_nudge`
的 `resumed` 档："台账里这件事还没做完，本轮把它做掉"）——而模型已经两次没注意到它。
批 B 换掉的是**出口**：下一步是谁、参数是什么，早就落在台账里了（批 A 让步骤带上了
`workflow`/`args`）⇒ 拼装它是一次**查表**，不需要任何决策。

本套件锁六件事：

  ① 确认兑现轮的续跑：下一步已绑实参 ⇒ `_planner_preflight` **early** 返回一份计划，
     且**不吃 `plan_rounds`**（零 LLM 出口不许拿一次不存在的决策去抵 `MAX_PLAN_ROUNDS`）；
  ② **越位拒绝**：上一张卡的令牌**不许**覆盖这一张卡的 spec（令牌只授权它签名的那一件，
     新卡必须由 `_confirm_popup` 重新签）；
  ③ **不猜**：实参不齐 / 挂着 `$tool[N]` 引用 / 技能名认不出 ⇒ 退回 planner 补参；
  ④ **进度守卫**：同一行同一游标连着发两次而回执没动 ⇒ 停手（否则是个不出声的死循环）；
  ⑤ 单元推进：卡的最小单位＝`adminops.linked_units` 的单元（「先建再挂」两张卡会把
     第二张变成"点不动的格子"）；
  ⑥ **撤档（20261015）⇒ 这一支常开**：`AGENT_TASK_PLAN_EXEC` 已随任务状态那一族整族删除，
     判据只剩"台账里有没有现在能动手的一件"；本节反转成"不许把档位加回来"的锁
     （裸 `if resumed:` 守卫 + 单调用点 + 反向对照：台账在场但不是兑现轮 ⇒ 也不出卡）。
     **撤档前**这一节判的是"关档 ⇒ 这条路不存在"（`AGENT_TASK_PLAN_EXEC=0`，出厂档，
     `conftest._PINNED` 钉住它）——那个对照臂随档位一起没了。

用法：.venv/bin/python tests/test_plan_executor.py
"""


from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


import agent.graph as G  # noqa: E402
import agent.tasks as T  # noqa: E402
from utils import trace as trace_mod  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


ROLE = "superadmin"


_SRC = "给文章 46 加个标签 embeding"


_DECL = T.intents_to_declarations(
    [{"goal": _SRC, "workflow": "article_tags",
      "params": {"article_id": 46, "add": ["embeding"]}}],
    role=ROLE, sources=(_SRC,))


_FRAME = T.frame_payload(T.normalize_declaration(_DECL[0]), 346)


_STEPS = _FRAME["steps"]


_SRC2 = "在后台待办里加一条「身份变更复核」"


_DECL2 = T.intents_to_declarations(
    [{"goal": _SRC2, "workflow": "dashboard_todo_add", "params": {"text": "身份变更复核"}}],
    role=ROLE, sources=(_SRC2,))


_FRAME2 = T.frame_payload(T.normalize_declaration(_DECL2[0]), 347)


_STEPS2 = _FRAME2["steps"]


def _row(**over) -> dict:
    row = dict(_FRAME, state="running", cursor=0, pending_question="")
    row.update(over)
    return row


def _row2(**over) -> dict:
    row = dict(_FRAME2, state="running", cursor=0, pending_question="")
    row.update(over)
    return row


def _spec(s: dict) -> str:
    return f"{s['tool']}({json.dumps(s['args'], ensure_ascii=False)})"


def _cfg(rows: list) -> dict:
    return {"configurable": {"open_tasks": rows}}


def _state(**over) -> dict:
    """**确认兑现轮的续跑**那一格：令牌在场、`plan_rounds>0`、无受阻。"""
    st = {"messages": [], "plan_rounds": 1, "plan_obj": {}, "plan": "",
          "executed": [], "receipts": [], "blocked": [], "blocked_seen": [],
          "done": False, "tool_data": [], "task_frames": [],
          "plan_task_id": "", "plan_cursor": -1,
          "confirm_grant": {"workflow": "tag_create",
                            "specs": [{"tool": "create_tag", "args": {"title": "甲"}}]}}
    st.update(over)
    return st


def _run(state: dict, config) -> tuple[object, list]:
    """跑一次 `_planner_preflight`，回 `(pre, 本次的 planner 事件)`。"""
    tid = f"t_planexec_{len(_EVENTS)}"
    rec = trace_mod.start_trace(tid, 7, "th", {}, dir=tempfile.mkdtemp(), by_day=False)
    pre = G._planner_preflight(state, config)
    _EVENTS.append(tid)
    return pre, [e for e in rec.events if e.get("node") == "planner"]


_EVENTS: list[str] = []


def _ev(events: list, name: str) -> list[dict]:
    return [e for e in events if e.get("event") == name]


def _test_confirm_resume_emits_the_next_card_without_llm():
    print("\n① 确认兑现轮的续跑：下一步已绑实参 ⇒ 零 LLM 出下一张卡")
    pre, ev = _run(_state(), _cfg([_row()]))
    check("`_planner_preflight` 直接 early 返回（**没有**走到 LLM 决策轮）",
          pre.early is not None)
    if pre.early is None:
        return
    check("⭐ 计划里的调用串与台账那一步**逐字相同**（值一个字符都不改）",
          pre.early["plan_obj"]["tools"] == [_spec(s) for s in _STEPS],
          str(pre.early["plan_obj"]["tools"]))
    check("技能名判据是**闭集覆盖**：`create_tag` 由 `article_tags` 的 "
          "`extra_plan_tools` 声明，所以整个单元归它",
          pre.early["plan_obj"]["workflow"] == "article_tags")
    check("⭐ **不吃 `plan_rounds`**（零 LLM 出口不许拿一次不存在的决策去抵 MAX_PLAN_ROUNDS）",
          pre.early["plan_rounds"] == 1, str(pre.early["plan_rounds"]))
    check("进度守卫的两格写进了 state（同一轮的下一跳靠它）",
          pre.early["plan_task_id"] == _FRAME["task_id"]
          and pre.early["plan_cursor"] == 0)
    check("文本态与结构化态一起给（`plan_state` 的唯一入口，execute 读的是文本那条）",
          G.parse_plan(pre.early["plan"])["tools"] == pre.early["plan_obj"]["tools"])
    check("plan_obj 的键集与令牌那条通道**同款**（同一个 `_confirm_grant_plan` 契约）",
          set(pre.early["plan_obj"]) == set(G.plan_state(G._confirm_grant_plan(
              {"workflow": "tag_create", "specs": [{"tool": "create_tag",
                                                    "args": {"title": "甲"}}]}))["plan_obj"]))
    check("注记只写**事实与禁止**，不写机制（写给 narrator 的机制描述会变成它的词汇）",
          "弹卡" not in pre.early["plan_obj"]["note"]
          and "不得" in pre.early["plan_obj"]["note"])
    check("trace：`plan_step_bound`（找到已绑实参的下一步）＋ `plan_card_next`（决定走零 LLM）",
          len(_ev(ev, "plan_step_bound")) == 1 and len(_ev(ev, "plan_card_next")) == 1)
    check("  事件里带着是哪一行哪一步（事后能对回台账）",
          _ev(ev, "plan_card_next")[0].get("task_id") == _FRAME["task_id"]
          and _ev(ev, "plan_card_next")[0].get("cursor") == 0)


def _test_grant_does_not_cover_the_next_card():
    print("\n② 越位拒绝：上一张卡的令牌**不许**覆盖这一张卡的 spec")
    pre, _ = _run(_state(), _cfg([_row()]))
    specs = G.parse_plan(pre.early["plan"])["tools"]
    st = _state()
    check("对照：令牌签名体里那一件**在**覆盖范围里（否则下面几条是空跑）",
          G._grant_covers(st, 'create_tag({"title": "甲"})') is True
          and G._all_granted(st, ['create_tag({"title": "甲"})']) is True)
    check("⭐ 同工具、**换实参** ⇒ 不算覆盖（`_grant_covers` 比的是工具名 ∧ 参数逐字）",
          G._grant_covers(st, 'create_tag({"title": "embeding"})') is False)
    check("⭐ 这一批**整批都不在**签名体里 ⇒ `_all_granted` 为假 ⇒ `_confirm_popup` 会"
          "重新签一张卡（新卡新令牌，旧令牌一次都不作废给别的件）",
          G._all_granted(st, specs) is False)
    check("  写闸那条同样不免：`consent_missing` 读的是同一个判据",
          all(G._grant_covers(st, s) is False for s in specs))


def _test_unbound_next_step_goes_back_to_planner():
    print("\n③ 不猜：实参不齐 / `$tool[N]` 引用 / 技能认不出 ⇒ 退回 planner 补参")
    cases = [
        ("没有 `args` 这一格（批 A 的占位步）",
         [{k: v for k, v in _STEPS[0].items() if k != "args"}], "plan_placeholder_handback"),
        ("实参还挂着 `$tool[N]` 引用（值的来路是**上一轮的帧**，跨轮取不到）",
         [dict(_STEPS[0], args={"title": "$tool[0].title"})], "plan_placeholder_handback"),
        ("技能名认不出（批 A 之前登记的老行没有 `workflow`）",
         [dict(_STEPS[0], workflow="")], "plan_exec_workflow_mismatch"),
        ("技能与工具对不上（两步都声称是 `tag_create`，而那个技能管不到 `set_article_tags` "
         "⇒ 一个工具都不执行）",
         [dict(_STEPS[0], workflow="tag_create"), dict(_STEPS[1], workflow="tag_create")],
         "plan_exec_workflow_mismatch"),
    ]
    for name, steps, event in cases:
        pre, ev = _run(_state(), _cfg([_row(steps=steps, total_steps=len(steps))]))
        check(f"{name} ⇒ 不 early、交回今天的链路", pre.early is None)
        check(f"  留下一条如实的痕（`{event}`）", len(_ev(ev, event)) == 1)


def _test_progress_guard_stops_a_silent_loop():
    print("\n④ 进度守卫：同一行同一游标连着发两次而回执没动 ⇒ 停手")
    st = _state()
    pre1, _ = _run(st, _cfg([_row()]))
    check("第一跳：发得出（否则下面那条是空跑）", pre1.early is not None)
    carried = {k: pre1.early[k] for k in ("plan_task_id", "plan_cursor")}
    pre2, ev2 = _run(_state(**carried), _cfg([_row()]))
    check("⭐ 第二跳：同一行同一游标 ⇒ **不发**（回执没推进，重发毫无意义）",
          pre2.early is None)
    check("  trace 留痕 `plan_exec_stalled`（这一格必须是看得见的，不能静默空转）",
          len(_ev(ev2, "plan_exec_stalled")) == 1)
    # 回执落地 ⇒ 游标前进 ⇒ 放行（守卫拦的是"没进展"，不是"发过一次"）
    rcp = [{"tool": "create_tag", "args": {"title": "embeding"},
            "result": "已新建", "ts": 1.0}]
    row = _row()
    check("  回执落地后游标真的前进了（判据与结算同源：`tasks.next_cursor`）",
          T.next_cursor(row, rcp) == 1)
    pre3, _ = _run(_state(receipts=rcp, **carried), _cfg([row]))
    check("⭐ 游标前进 ⇒ 放行，且发的是**单元剩下的那一步**（不重发已执行的那一步）",
          pre3.early is not None
          and pre3.early["plan_obj"]["tools"] == [_spec(_STEPS[1])],
          str(pre3.early and pre3.early["plan_obj"]["tools"]))


def _test_unit_is_the_card_granularity():
    print("\n⑤ 单元推进：卡的最小单位＝`adminops.linked_units` 的单元")
    check("「先建再挂」是**一个**单元（两张卡的第二张注定点不动）",
          G._unit_at(_STEPS, 0) == _STEPS, str(G._unit_at(_STEPS, 0)))
    check("游标落在单元中间（前一半已执行）⇒ 只取剩下那几步",
          G._unit_at(_STEPS, 1) == [_STEPS[1]])
    check("游标越界 ⇒ 空表（调用方据此不出计划）", G._unit_at(_STEPS, 9) == [])
    # 与卡面**同一份**归组：`_confirm_popup` 的按钮编号就是这个函数算的。
    # 这一步是 20260929 批 F 那次的形状：按 spec 逐条编号会把「先建再挂」切成两个格子，
    # 主人点「只办第 2 件」就得到一条注定失败的 `set_article_tags`。
    multi = [{"label": "create_tag", "tool": "create_tag", "args": {"title": "甲"}},
             {"label": "set_article_tags", "tool": "set_article_tags",
              "args": {"article_id": 1, "add": ["甲"]}},
             {"label": "create_tag", "tool": "create_tag", "args": {"title": "乙"}}]
    check("归组不是我在这抄的一份（多步混杂时与 `linked_units` 逐字相同）",
          G._unit_at(multi, 0) == [multi[0], multi[1]]
          and G._unit_at(multi, 2) == [multi[2]])
    check("  配对靠**结构**（名字对得上），不是按位置硬切",
          G._unit_at([multi[2], multi[1], multi[0]], 1) == [multi[1]])


def _test_no_dial_keeps_the_executor_always_on():
    print("\n⑥ 撤档（20261015）⇒ 这一支**常开**，且不许再长出第二格档位判据")

    # 撤档前这一节判的是"关档 ⇒ 这条路不存在（逐字节退回本批之前）"。档位撤掉之后
    # 那个手臂**不存在**了，于是本节反转成一条**不许把档位加回来**的锁：判据只剩
    # "台账里有没有现在能动手的一件"，不再有第三格开关。
    pre, ev = _run(_state(), _cfg([_row()]))
    check("台账明写着还剩一件 ⇒ 零 LLM 出下一张卡（常开档的唯一形态）",
          pre.early is not None
          and bool(_ev(ev, "plan_card_next")) and bool(_ev(ev, "plan_step_bound")))
    check("  台账不在场 ⇒ 什么都不发生（常开不等于无判据）",
          _run(_state(), {"configurable": {}})[0].early is None)
    # 反向对照：台账里同样躺着那一行，但这一轮**不是确认兑现轮**（`plan_rounds==0`）
    # ⇒ 一个 `plan_card_next` 都不许有。守卫是"令牌刚兑现完"这个语义，不是"台账里有
    # 东西"——把这两件事分开，否则明天有人把守卫改成"有台账就发"时，本节的其它格
    # （都从同一个 `_state()` 派生）会一起跟着变绿。
    _pre0, ev0 = _run(_state(plan_rounds=0), _cfg([_row()]))
    check("  台账在场但不是兑现轮（rounds=0）⇒ 也不出这张卡",
          not _ev(ev0, "plan_card_next") and _pre0.early is not None)

    src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
    check("源码锁：调用点的守卫就是**裸 `if resumed:`**（多一格档位就要有人解释）",
          src.count("if resumed:\n        pe = _plan_exec_next_card(") == 1)
    check("源码锁：`_plan_exec_next_card` 只有一处调用点（另一处是定义）",
          src.count("_plan_exec_next_card(") == 2)


def _test_state_fields_are_declared_and_read():
    print("\n⑦ 结构锁：守卫的两格**必须显式声明**（未声明的键会被静默丢弃）")
    ann = G.AgentState.__annotations__
    check("`AgentState` 声明了 `plan_task_id` / `plan_cursor`",
          "plan_task_id" in ann and "plan_cursor" in ann)
    gi = G.graph_input([])
    check("`graph_input` 给中性初值（`\"\"` = 没驱动过任何一行；`-1` = 没发过计划）",
          gi.get("plan_task_id") == "" and gi.get("plan_cursor") == -1)
    src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
    check("守卫真的**读**它（声明了却没人读 = 同族的另一副面孔）",
          src.count('state.get("plan_cursor")') == 1
          and 'state.get("plan_task_id")' in src)
    check("`_plan_update` 的零 LLM 出口把两格一起写（守卫与「不吃预算」必须成对）",
          "advance=False" in src and "plan_cursor=pe.cursor" in src)


def _test_done_probe_is_observable():
    print("\n⑧ 到头了要有对偶事件（否则分不出「没生效」与「办完了」）")
    rcp = [{"tool": "create_tag", "args": {"title": "embeding"}, "result": "ok", "ts": 1.0},
           {"tool": "set_article_tags", "args": {"article_id": 46, "add": ["embeding"]},
            "result": "ok", "ts": 2.0}]
    row = _row()
    check("对照：这两条回执真的把这一行结算成 `succeeded`（否则下面那条是空跑）",
          T.settled_by_receipts(row, rcp) and T.next_actionable([row], rcp) is None)
    _tid = "t_planexec_done"
    rec = trace_mod.start_trace(_tid, 7, "th", {}, dir=tempfile.mkdtemp(), by_day=False)
    out = G._plan_exec_next_card(_state(receipts=rcp, plan_task_id=row["task_id"]),
                                 _cfg([row]))
    done = [e for e in rec.events if e.get("event") == "plan_done"]
    check("这一行到头 ⇒ 不出计划，只记 `plan_done`（带游标与终态）",
          out is None and len(done) == 1
          and done[0].get("state") == "succeeded" and done[0].get("cursor") == 2,
          str(done))
    rec2 = trace_mod.start_trace("t_planexec_done2", 7, "th", {},
                                 dir=tempfile.mkdtemp(), by_day=False)
    G._plan_exec_next_card(_state(receipts=rcp), _cfg([row]))
    check("  本请求没驱动过任何一行（`plan_task_id` 空）⇒ 不记（这事件不是背景噪声）",
          not [e for e in rec2.events if e.get("event") == "plan_done"])


def _test_next_cursor_is_the_single_cursor_authority():
    print("\n⑨ `next_cursor` 是游标的**唯一**算法（两处各推一份迟早分叉）")
    row = _row()
    check("没有回执 ⇒ 停在落库的游标上", T.next_cursor(row, []) == 0)
    check("回执只推进**连续**的前缀（不做跳跃：跳跃会让「进度 2/3」变成一句编出来的话）",
          T.next_cursor(row, [{"tool": "set_article_tags", "ts": 1.0}]) == 0
          and T.next_cursor(row, [{"tool": "create_tag", "ts": 1.0}]) == 1)
    check("`next_actionable` 的游标格与它同源（同一个纯函数，不是两次独立计算）",
          T.next_cursor(row, [{"tool": "create_tag", "ts": 1.0}]) == 1
          and T.next_actionable([row], [{"tool": "create_tag", "ts": 1.0}])[1]
          is row["steps"][1])
    src = (ROOT / "agent" / "tasks.py").read_text(encoding="utf-8")
    body = src[src.index("def next_actionable("):]
    body = body[:body.index("\ndef ")]
    # 判"调用形态"（带括号）而不是"文本里出现过这个名字"：它的 docstring 正当地
    # 提了一句结算函数的名字，那是解释不是实现。
    check("源码锁：`next_actionable` 直接问 `next_cursor`，自己不再算一遍游标",
          "cursor = next_cursor(row, receipts)" in body
          and "advance_by_receipts(" not in body)


def _test_the_row_just_settled_is_not_re_emitted():
    print("\n⑩ 台账里两行并排：刚兑现完的那一行**不再发**，第二张卡是**另一行**的")

    # 这一格的现场形状（`logs/agent/traces/20261012/054330`）：主人一句话两件写 ⇒ 弹卡①
    # ⇒ 点确定 ⇒ **同一个请求内**先执行①，执行完 `route_after_execute` 把控制权交回
    # planner（这就是 `resumed` 那一格）。关键是**①的回执已经在 `state["receipts"]` 里**
    # ——回执是请求内累计的（见 `AgentState.receipts` 的注），所以 `next_cursor` 已经把
    # 第一行推过去了。这一条锁的就是这个前提：没有它，执行器会把主人**刚刚点过确定**的
    # 那一件原样再发一张卡，而卡面断言在那种形状下**照样绿**（台账没推进 ⇒ `workflow_any`
    # 看到的那张卡还是同一件），主人白点一次。
    rcp = [{"tool": s["tool"], "args": s["args"], "result": "已办", "ts": 1.0}
           for s in _STEPS]
    row_a, row_b = _row(), _row2()
    check("对照：①的回执真的把第一行结算成 `succeeded`（否则下面那条是空跑）",
          T.settled_by_receipts(row_a, rcp))
    check("  两行是**不同的**任务（同一个 goal 会算出同一个 `task_id`）",
          row_a["task_id"] != row_b["task_id"])
    pre, ev = _run(_state(receipts=rcp), _cfg([row_a, row_b]))
    check("⭐ 发出的是**第二行**的单元，不是刚办完的第一行",
          pre.early is not None
          and pre.early["plan_obj"]["tools"] == [_spec(s) for s in _STEPS2],
          str(pre.early and pre.early["plan_obj"]["tools"]))
    check("  技能名跟着第二行走（`dashboard_todo_add`，不是第一行的）",
          pre.early is not None
          and pre.early["plan_obj"]["workflow"] == "dashboard_todo_add")
    check("  进度守卫记的是**第二行**的 id（下一跳拿它比对）",
          pre.early is not None and pre.early["plan_task_id"] == row_b["task_id"])
    check("  trace 也对得回第二行",
          [e.get("task_id") for e in _ev(ev, "plan_card_next")] == [row_b["task_id"]])
    check("  第二行的游标是 0（它一步没做，不是接着第一行往下数）",
          pre.early is not None and pre.early["plan_cursor"] == 0)
    # 反面对照：两行都还在（第一行没有回执）⇒ 发的**就是第一行**。没有这一条，
    # 「永远发第二行」也会绿——而那是另一副面孔的错（漏掉主人先说的那件）。
    pre2, _ = _run(_state(), _cfg([row_a, row_b]))
    check("对照：第一行还没办 ⇒ 发的就是第一行（顺序不是「写死第二行」）",
          pre2.early is not None
          and pre2.early["plan_obj"]["tools"] == [_spec(s) for s in _STEPS])


def test_plan_executor():
    for fn in (_test_confirm_resume_emits_the_next_card_without_llm,
               _test_grant_does_not_cover_the_next_card,
               _test_unbound_next_step_goes_back_to_planner,
               _test_progress_guard_stops_a_silent_loop,
               _test_unit_is_the_card_granularity,
               _test_no_dial_keeps_the_executor_always_on,
               _test_state_fields_are_declared_and_read,
               _test_done_probe_is_observable,
               _test_next_cursor_is_the_single_cursor_authority,
               _test_the_row_just_settled_is_not_re_emitted):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILED else f"失败 {len(FAILED)} 项 ❌: {FAILED}"))

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_plan_executor()
