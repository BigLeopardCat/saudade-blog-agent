"""台账终态：开着的行要么被回执结掉、要么被期限结掉（`agent/tasks.py::expire_transitions`）。

**为什么单独一套**（20261015）：这一步治的是"有一族账永远结不掉"——生产库直读实测
（窗口内 17 行 `running` **全部** `cursor=0`、声明的那一步工具一次都没执行过），
两副面孔（同意闸挡下 / 换另一件工具办掉了）**在代码里都没有任何一处会改 state**：
`advance_by_tools` 见游标推不动就 `return None` ⇒ 不发帧 ⇒ 那一行只能等 Rust 读侧的
72h 时效把它从**注入**里抹掉，而**行本身永远留在表里、状态永远写着"还开着"**。

钉住的契约（这里红 = 某条判据被改掉了，改动要同步改这里）：

  · **软期限**到点 ⇒ `input_required` + 催办问句；**催办本身就是状态转换** ⇒ 天然
    只问一次（不需要另立"问过没有"的标志位）；
  · **硬期限**到点 ⇒ `cancelled` + 清空问句；**已经在等主人回话的行（`input_required`）
    照样被硬期限收掉**——问过一遍没回音不等于可以永远挂着；
  · **同意闸挡下** ⇒ 当场 `input_required` + 同意闸问句，**不看年龄**（那是当轮已知的
    事实，不该等 12 小时）；
  · **先结账、后催账**：本轮已被回执结掉的行**不许**在同一轮被判过期；
  · **同值不产出**：目标 state + 问句与当前逐字相同 ⇒ 不产帧（`__TASK__` 每发一次
    Rust 就把 `attempts` 加一，发空帧等于刷计数）；
  · **认不出必须响亮**：`created_at` 看不懂 ⇒ 不转换 + 进 `undecidable`（不静默跳过）；
    但**本轮刚登记的行**（还没有 `created_at`）不算认不出——「刚登记」与「读不出来」
    是两件事，混起来会把那个信号冲成噪声；
  · **系统写的问句在游标推进后必须清掉**（`advance_by_tools`）：不清的话那一行会被
    一句过期问句永久挡住（`next_actionable` 判据②），剩下的步骤再没人续跑；
  · **跨语言**：`SOFT < HARD < TASK_READ_TTL_HOURS`，而后者从父仓 `src/routes/chat.rs`
    现读（跨年规则够用的论据、以及"转换必须落在读窗口内"这条不变量都吊在它上面；
    父仓读不到时按 `tests/_parent_repo.py` 的三分支处理）；
  · **帧形状**：转换帧与结算帧**逐字同形**（8 键里少 `idempotency_key` 那一格，
    不带身份两列），且 `steps` **不清空**（那是事后唯一的线索）。
"""


import json
import re
import sys
from datetime import datetime
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）


sys.path.insert(0, str(ROOT))


from agent import tasks as T  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


NOW = datetime(2026, 10, 15, 12, 0)


def _row(task_id="at_1", *, ago_min=None, created_at=None, state="running",
         cursor=0, steps=None, pending_question="", total=None):
    """一行台账。`ago_min` = 相对 `NOW` 的年龄（分钟）⇒ 渲染成行首那个 `MM-DD HH:MM`。"""
    if created_at is None:
        if ago_min is None:
            created_at = ""
        else:
            t = NOW.timestamp() - ago_min * 60
            created_at = datetime.fromtimestamp(t).strftime("%m-%d %H:%M")
    steps = steps if steps is not None else [{"label": "办一件事", "tool": "send_user_notice"}]
    return {"task_id": task_id, "goal": "把这件事办了", "steps": steps,
            "total_steps": len(steps) if total is None else total, "cursor": cursor,
            "state": state, "pending_question": pending_question,
            "created_at": created_at}


def _states(rows, **kw):
    tr, _ = T.expire_transitions(rows, now=NOW, **kw)
    return {t["task_id"]: (t["state"], t["pending_question"]) for t in tr}


def _test_soft_deadline_asks_once():
    print("\n[软期限] 到点转 input_required + 催办问句；**转过之后不再问第二遍**")
    check("11h59 ⇒ 不产出（差一分钟都不算到期）",
          _states([_row(ago_min=11 * 60 + 59)]) == {},
          str(_states([_row(ago_min=11 * 60 + 59)])))
    got = _states([_row(ago_min=12 * 60)])
    check("12h00 ⇒ input_required + 催办问句",
          got == {"at_1": ("input_required", T.TASK_STALE_ASK)}, str(got))
    # "只问一次"不是靠一个标志位，是靠**转换本身**：把产出物回灌成下一轮的输入
    # （这正是 Rust 落库 → 下一轮读回来的那一步），同一判据必须不再命中。
    again = _states([_row(ago_min=12 * 60 + 30, state="input_required",
                          pending_question=T.TASK_STALE_ASK)])
    check("★ 转过之后再跑一次 ⇒ 不产出第二次（催办即转换，不需要标志位）",
          again == {}, str(again))
    check("催办问句是**常量**（系统靠它认出「这句是我写的」，见 `TASK_SYSTEM_ASKS`）",
          T.TASK_STALE_ASK in T.TASK_SYSTEM_ASKS and T.TASK_CONSENT_ASK in T.TASK_SYSTEM_ASKS,
          str(sorted(T.TASK_SYSTEM_ASKS)))
    check("软硬期限是常量、且软 < 硬（写反了就没有「催办」这一档）",
          T.TASK_STALE_SOFT_HOURS < T.TASK_STALE_HARD_HOURS,
          f"{T.TASK_STALE_SOFT_HOURS} < {T.TASK_STALE_HARD_HOURS}")


def _test_hard_deadline_is_terminal():
    print("\n[硬期限] 到点 ⇒ cancelled + 清空问句；`input_required` 的行照样收掉")
    got = _states([_row(ago_min=48 * 60)])
    check("48h00 ⇒ cancelled、pending_question 清空（撤下的事不该还挂着问题）",
          got == {"at_1": ("cancelled", "")}, str(got))
    check("47h59 ⇒ 还没到（催办那一档，不是终态）",
          _states([_row(ago_min=47 * 60 + 59)]) ==
          {"at_1": ("input_required", T.TASK_STALE_ASK)},
          str(_states([_row(ago_min=47 * 60 + 59)])))
    got2 = _states([_row(ago_min=72 * 60, state="input_required",
                         pending_question=T.TASK_STALE_ASK)])
    check("★ 已经在等主人回话的行，硬期限照样收掉（问过一遍没回音 ≠ 可以永远挂着）",
          got2 == {"at_1": ("cancelled", "")}, str(got2))
    check("`cancelled` 是六态里**已有**的终态、且不在 `TASK_OPEN_STATES` 里"
          "（读侧单点：不进那个集合就自动不再注入）",
          "cancelled" not in T.TASK_OPEN_STATES, str(T.TASK_OPEN_STATES))


def _test_consent_block_is_immediate():
    print("\n[同意闸] 本轮被挡下的那一步 ⇒ 当场 input_required（**不看年龄**）")
    row = _row(ago_min=0, steps=[{"label": "建待办", "tool": "create_dashboard_todo"}])
    got = _states([row], consent_asks={"create_dashboard_todo": T.TASK_CONSENT_ASK})
    check("刚登记的行（年龄 0）也当场转，并带上同意闸问句",
          got == {"at_1": ("input_required", T.TASK_CONSENT_ASK)}, str(got))
    tr, _ = T.expire_transitions([row], now=NOW,
                                consent_asks={"create_dashboard_todo": T.TASK_CONSENT_ASK})
    check("`via=consent`（只进 trace：帧是 8 键契约，多一格要同步 Rust）",
          tr[0].get("via") == "consent", str(tr[0]))
    check("同意闸那一支只认**游标位那一步**的工具名——声明了两步、挡下的是第二步 ⇒ 不转",
          _states([_row(steps=[{"label": "a", "tool": "send_user_notice"},
                               {"label": "b", "tool": "create_dashboard_todo"}],
                        cursor=1)],
                  consent_asks={"create_dashboard_todo": T.TASK_CONSENT_ASK}) != {}
          and _states([_row(steps=[{"label": "a", "tool": "send_user_notice"},
                                   {"label": "b", "tool": "create_dashboard_todo"}],
                             cursor=0)],
                      consent_asks={"create_dashboard_todo": T.TASK_CONSENT_ASK}) == {},
          "游标 0 位是 send_user_notice ⇒ 不转")
    # 反控：同意闸问句**不许**是逐行拼出来的卡面问句。卡面那句结尾是「点「确定」我就
    # 去办」——它指着一张卡，而这一句要到下一轮才印给主人看，那时令牌早已过期。
    check("★ 反控：同意闸问句里**没有**指向按钮的话（'点「确定」'/'点确定'）",
          "确定」" not in T.TASK_CONSENT_ASK and "点确定" not in T.TASK_CONSENT_ASK,
          T.TASK_CONSENT_ASK)


def _test_settled_rows_are_not_expired():
    print("\n[顺序] 本轮已被回执结掉的行 ⇒ 不许在同一轮被判过期")
    old = _row(ago_min=72 * 60)                     # 早就过了硬期限
    check("不传 settled ⇒ 判过期（这一格不是恒不命中）",
          _states([old]) == {"at_1": ("cancelled", "")}, str(_states([old])))
    check("★ 传了 settled ⇒ 一个字都不产出（先结账再催账）",
          _states([old], settled=["at_1"]) == {}, str(_states([old], settled=["at_1"])))
    check("settled 只挡它自己那一行（别的行照旧判）",
          _states([old, _row("at_2", ago_min=72 * 60)], settled=["at_1"]) ==
          {"at_2": ("cancelled", "")},
          str(_states([old, _row("at_2", ago_min=72 * 60)], settled=["at_1"])))


def _test_no_op_transitions_are_suppressed():
    print("\n[同值不产出] 目标 state + 问句与当前逐字相同 ⇒ 不产帧")
    same = _row(ago_min=13 * 60, state="input_required",
                pending_question=T.TASK_STALE_ASK)
    check("状态与问句都已经是目标值 ⇒ 不产出（`__TASK__` 每发一次 attempts 加一）",
          _states([same]) == {}, str(_states([same])))
    # 反控：把问句改一个字（模型自己写的另一句话）⇒ 判据不是"非空就跳过"
    other = _row(ago_min=13 * 60, state="input_required", pending_question="降成哪个身份？")
    check("★ 反控：问句不同 ⇒ 仍不产出（`input_required` 这一档软期限本就不碰）",
          _states([other]) == {}, str(_states([other])))
    check("  而硬期限对它照样生效（清空那个模型自己写的问句）",
          _states([_row(ago_min=72 * 60, state="input_required",
                        pending_question="降成哪个身份？")]) ==
          {"at_1": ("cancelled", "")},
          str(_states([_row(ago_min=72 * 60, state="input_required",
                            pending_question="降成哪个身份？")])))


def _test_unreadable_timestamp_is_loud():
    print("\n[认不出] `created_at` 读不出来 ⇒ 不转换 + 进 undecidable（不静默）")
    tr, und = T.expire_transitions([_row(created_at="")], now=NOW)
    check("缺 created_at ⇒ 零转换 + 记进 undecidable",
          tr == [] and und == ["at_1"], f"{tr} / {und}")
    tr2, und2 = T.expire_transitions([_row(created_at="2026-10-15 12:00:00")], now=NOW)
    check("形状不对（完整日期）⇒ 同样认不出（正则两端排除项挡的就是它）",
          tr2 == [] and und2 == ["at_1"], f"{tr2} / {und2}")
    tr3, und3 = T.expire_transitions([_row(created_at="13-45 99:99")], now=NOW)
    check("月日越界 ⇒ 同样认不出（不猜、也不当成「刚刚」）",
          tr3 == [] and und3 == ["at_1"], f"{tr3} / {und3}")
    _, und4 = T.expire_transitions([_row(state="input_required",
                                        pending_question=T.TASK_STALE_ASK,
                                        created_at="")], now=NOW)
    check("已经在等主人回话的行**不进** undecidable（那一格是给「本该判却没判」用的）",
          und4 == [], str(und4))
    _, und5 = T.expire_transitions([_row(created_at="")], now=NOW, fresh_ids=["at_1"])
    check("★ 本轮刚登记的行（还没有 created_at）**不算**认不出——"
          "「刚登记」与「读不出来」是两件事，混起来会把那个信号冲成噪声",
          und5 == [], str(und5))
    tr6, _ = T.expire_transitions(
        [_row(created_at="", steps=[{"label": "建待办", "tool": "create_dashboard_todo"}])],
        now=NOW, consent_asks={"create_dashboard_todo": T.TASK_CONSENT_ASK},
        fresh_ids=["at_1"])
    check("  而同意闸那一支照样命中它（当场说「在等你点头」正是要的那一格）",
          [t["state"] for t in tr6] == ["input_required"], str(tr6))
    tr7, und7 = T.expire_transitions([_row(created_at=""), _row(created_at="")], now=NOW)
    check("同 id 两行只判一次（读回来那行 + 本轮重登记那行是常态）",
          tr7 == [] and und7 == ["at_1"], f"{tr7} / {und7}")


def _test_year_rollover():
    print("\n[跨年] `12-31 23:50` 在 `01-01 00:10` 读到 ⇒ 20 分钟，不是一年")
    check("跨年退一年（否则按当年解析 = 十一个月后的未来）",
          T.age_minutes("12-31 23:50", datetime(2027, 1, 1, 0, 10)) == 20,
          str(T.age_minutes("12-31 23:50", datetime(2027, 1, 1, 0, 10))))
    check("不跨年时**不退**（同样的月日、不同的现时）",
          T.age_minutes("12-31 23:50", datetime(2026, 12, 31, 23, 55)) == 5,
          str(T.age_minutes("12-31 23:50", datetime(2026, 12, 31, 23, 55))))
    # 6h 这条边界的**两侧**（规则只说"超前 6 小时以上退一年"，一句之差就是两个年份）。
    # 恰好 6h（不 > 6h）⇒ 不退 ⇒ 得到一个**负数**年龄（"六小时后"）。
    # 负数不是缺陷：它使 `age >= soft` 为假 ⇒ 不产出，正是要的（未来的行不该被催办）。
    check("恰好超前 6h ⇒ **不退**（边界是「> 6h」，不是「≥」）",
          T.age_minutes("01-01 06:30", datetime(2026, 1, 1, 0, 30)) == -360,
          str(T.age_minutes("01-01 06:30", datetime(2026, 1, 1, 0, 30))))
    check("超前 6h01m ⇒ 退一年（同一分钟内跨过边界）",
          T.age_minutes("01-01 06:31", datetime(2026, 1, 1, 0, 30)) > 60 * 24 * 300,
          str(T.age_minutes("01-01 06:31", datetime(2026, 1, 1, 0, 30))))
    _future = datetime(2026, 1, 1, 0, 30)
    check("未来时刻的年龄是负数 ⇒ 三条期限都不命中（不产出，不该被催办）",
          T.expire_transitions([_row(created_at="01-01 06:30")], now=_future)[0] == [],
          str(T.expire_transitions([_row(created_at="01-01 06:30")], now=_future)[0]))
    check("看不懂返回 None（调用方据此响亮，绝不退化成「刚刚」）",
          T.age_minutes("", NOW) is None and T.age_minutes("13-45 00:00", NOW) is None)
    # 与 `annotate_exec_ages` 同源：两处必须给同一个数，否则同一行时间在
    # planner 上下文与终态判据里是两个年龄（本仓"两份会漂的解析规则"那族的病）。
    import server  # noqa: E402
    check("★ 与 `server.annotate_exec_ages` 同源（同一份正则、同一个 helper）",
          server.TS_MMDD_HHMM_RE.pattern == T.TS_MMDD_HHMM_RE.pattern
          and server.age_minutes is T.age_minutes,
          "两处各写一份 ⇒ 同一行时间两个年龄，且没有任何东西会红")


def _test_system_ask_cleared_on_advance():
    print("\n[清问句] 游标一推进 ⇒ 系统写的问句必须清掉（否则那一行被永久挡住）")
    two = _row(steps=[{"label": "a", "tool": "send_user_notice"},
                      {"label": "b", "tool": "reset_user_quota"}],
               state="input_required", pending_question=T.TASK_CONSENT_ASK)
    adv = T.advance_by_tools(two, {"send_user_notice"})
    check("★ 推进到 running ⇒ pending_question 清空（不清就被 `next_actionable` 判据②"
          "永久跳过，剩下的步骤再没人续跑）",
          adv is not None and adv["cursor"] == 1 and adv["state"] == "running"
          and adv["pending_question"] == "", str(adv))
    check("  清掉之后 `next_actionable` 真的能续跑（判据在同一条链上）",
          T.next_actionable([dict(two, cursor=1, pending_question="")],
                            [{"tool": "send_user_notice"}]) is not None)
    check("  而不清的话它恒被跳过（这条反控钉住「为什么要清」）",
          T.next_actionable([dict(two, cursor=1,
                                  pending_question=T.TASK_CONSENT_ASK)], []) is None)
    model_asked = _row(steps=[{"label": "a", "tool": "send_user_notice"},
                              {"label": "b", "tool": "reset_user_quota"}],
                       state="input_required", pending_question="降成哪个身份？")
    adv2 = T.advance_by_tools(model_asked, {"send_user_notice"})
    check("★ 反控：**模型自己写的**问句一个字都不动（它问的可能是后面某一步的事）",
          adv2 is not None and adv2["pending_question"] == "降成哪个身份？", str(adv2))


def _test_transition_frames_match_settlement_shape():
    print("\n[帧] 转换帧与结算帧逐字同形（少 `idempotency_key` 那一格、不带身份）")
    rows = [_row("at_1", ago_min=72 * 60, steps=[{"label": "办一件事",
                                                  "tool": "send_user_notice",
                                                  "args": {"a": 1}}])]
    tr, _ = T.expire_transitions(rows, now=NOW)
    frames = T.transition_frames(rows, tr)
    check("恰一帧", len(frames) == 1, str(frames))
    f = frames[0]
    check("键集合与 `advance_by_tools` 的产出**逐字相同**（Rust 一套 upsert 认两种来源）",
          set(f) == {"task_id", "goal", "steps", "total_steps", "cursor",
                     "state", "pending_question"}, str(sorted(f)))
    check("不带身份两列（uid/会话由 Rust 从请求取，模型碰不到）",
          "user_id" not in f and "conversation_id" not in f and "uid" not in f)
    check("不带 `idempotency_key`（Rust 只在**新建行**时写它，已有行的 upsert 不读）",
          "idempotency_key" not in f)
    check("★ `steps` **原样带回**（它是事后唯一的线索；`normalize_drop` 给 `[]` 是"
          "「撤下」语义，这里不是撤下）",
          f["steps"] == rows[0]["steps"], str(f["steps"]))
    check("cursor / total_steps 也原样带回，不改写",
          f["cursor"] == 0 and f["total_steps"] == 1)
    check("`via` **不进帧**（只进 trace）", "via" not in f)
    check("JSON 可序列化（跨语言契约不会被序列化器吃掉）",
          json.loads(json.dumps(f, ensure_ascii=False))["task_id"] == "at_1")
    check("查不到对应行 ⇒ 跳过（拼出来会把那一行改写成一个空计划）",
          T.transition_frames([], tr) == [], str(T.transition_frames([], tr)))


def _test_deadline_invariant_mirrors_parent_repo():
    print("\n[跨语言] `SOFT < HARD < TASK_READ_TTL_HOURS`，后者从父仓 `chat.rs` 现读")
    import _parent_repo  # noqa: E402

    def invariant(soft, hard, ttl):
        """判据本体（抽出来是为了下面那条反控能直接喂坏值）。"""
        return 0 < soft < hard < ttl

    check("★ 转换必须落在读窗口**内**（落在窗口外 = 这段代码是死代码："
          "本仓「写了没人执行」那一族——R2 的 `--keep 3`、logrotate 的 `rotate 14`）",
          invariant(T.TASK_STALE_SOFT_HOURS, T.TASK_STALE_HARD_HOURS,
                    T.TASK_READ_TTL_HOURS),
          f"{T.TASK_STALE_SOFT_HOURS} < {T.TASK_STALE_HARD_HOURS} < {T.TASK_READ_TTL_HOURS}")
    check("★ 反控：窗口放宽到 100h（硬期限落到读窗口外）⇒ 判据当场红",
          not invariant(T.TASK_STALE_SOFT_HOURS, 100, T.TASK_READ_TTL_HOURS),
          "100 > 72")
    check("★ 反控：软硬写反 ⇒ 判据当场红", not invariant(48, 12, 72))

    src = _parent_repo.read(
        "src/routes/chat.rs",
        why="`TASK_READ_TTL_HOURS` 是跨语言契约：Rust 按它过滤**读给 agent 的行**，"
            "而 agent 侧的过期判据**只在这些行上跑**（Rust 不读回来的行永远到不了这里）。"
            "父仓把窗口改小到硬期限以内 ⇒ 这一段当场变成死代码，而**没有任何东西会红**")
    if src:
        m = re.search(r"const\s+TASK_READ_TTL_HOURS:\s*i64\s*=\s*(\d+)", src)
        check("父仓里真有这个常量（名字一改，这里的镜像就成了没人守的孤本）",
              m is not None, "src/routes/chat.rs")
        if m:
            check("两侧同值（agent 侧这一格只为那条不变量存在）",
                  int(m.group(1)) == T.TASK_READ_TTL_HOURS,
                  f"父仓 {m.group(1)} / agent {T.TASK_READ_TTL_HOURS}")
        m2 = re.search(r"const\s+TASK_OPEN_STATES:\s*\[&str;\s*\d+\]\s*=\s*\[([^\]]*)\]", src)
        check("父仓的未完结态集合里**没有** cancelled（终态不进注入，读侧单点）",
              m2 is not None and "cancelled" not in m2.group(1),
              m2.group(1).strip() if m2 else "没找到 TASK_OPEN_STATES")


def _test_producer_wiring():
    print("\n[接线] producer 里结算与收口都在、顺序是**先结算后收口**")
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    i_settle = src.find("advs = _task_advance_rows(")
    i_expire = src.find("_emit_task_frames(_task_expire_rows(")
    check("两处都在", i_settle > 0 and i_expire > 0, f"{i_settle} / {i_expire}")
    check("★ 结算在收口**之前**（反了就是「刚办完就被判过期」）",
          0 < i_settle < i_expire, f"{i_settle} < {i_expire}")
    check("结算过的 task_id 真的传给了收口（`settled` 不是摆设）",
          "settled = [str(a.get(\"task_id\") or \"\") for a in advs]" in src[i_settle:i_expire]
          and "settled, record), queue, loop)" in src[i_expire:i_expire + 200],
          "settled")
    check("两件事都住在 producer **外面**（`_run_agent_stream_to_queue` 已贴复杂度"
          "台账的行数上限，只许缩小）",
          "def _task_settle_and_expire(" in src)
    check("同意闸那一族在 execute update 里累计（`_consent_asks` 被调用）",
          "_consent_asks(ex_upd, consent_asks)" in src)
    check("两条路各自 try/except、失败只记一行（辅助事实绝不阻断对话）",
          src.count("任务结算失败（不影响本轮回复）") == 1
          and src.count("任务收口失败（不影响本轮回复）") == 1)
    check("`__TASK__` 帧只有两处出口：登记那条（收到即发）+ `_emit_task_frames`"
          "（结算与收口共用）——结算/收口各写一遍就是同一个形状漂两次",
          src.count('queue.put("__TASK__:" + json.dumps(') == 2,
          str(src.count('queue.put("__TASK__:" + json.dumps(')))


def _test_reverse_controls():
    print("\n[反控] 把真源头改坏 ⇒ 对应判据当场红（不是恒绿的装饰）")
    soft, hard = T.TASK_STALE_SOFT_HOURS, T.TASK_STALE_HARD_HOURS
    try:
        T.TASK_STALE_HARD_HOURS = 100          # 硬期限落到读窗口（72h）之外
        bad = (0 < T.TASK_STALE_SOFT_HOURS < T.TASK_STALE_HARD_HOURS
               < T.TASK_READ_TTL_HOURS)
        check("★ 硬期限改到 100h ⇒ 不变量判据当场红（那段代码会变成死代码）",
              not bad, f"{T.TASK_STALE_SOFT_HOURS} < {T.TASK_STALE_HARD_HOURS}"
                       f" < {T.TASK_READ_TTL_HOURS}")
    finally:
        T.TASK_STALE_HARD_HOURS = hard
    check("  还原之后判据回到绿（这一格不是「一次红就永远红」）",
          0 < T.TASK_STALE_SOFT_HOURS < T.TASK_STALE_HARD_HOURS < T.TASK_READ_TTL_HOURS)
    # 静默跳过那一支：把"看不懂"改成"当成刚刚"（本仓反复吃过的那口井），
    # `undecidable` 必须当场空掉——空了就说明那声"响亮"完全吊在 None 上。
    real = T.age_minutes
    try:
        T.age_minutes = lambda ts, now=None: (real(ts, now) or 0)
        _, und = T.expire_transitions([_row(created_at="")], now=NOW)
        check("★ 把解析失败静默成「刚刚」 ⇒ undecidable 当场空掉（那声响亮没了）",
              und == [], str(und))
    finally:
        T.age_minutes = real
    _, und_ok = T.expire_transitions([_row(created_at="")], now=NOW)
    check("  还原之后又喊得出来（`created_at` 读不出来的那一行没被漏掉）",
          und_ok == ["at_1"], str(und_ok))
    check("软期限那一格也参与（改了它 ⇒ 12h 边界那一条会红）",
          soft == 12 and hard == 48, f"{soft} / {hard}")


def test_task_expiry():
    for fn in (_test_soft_deadline_asks_once,
               _test_hard_deadline_is_terminal,
               _test_consent_block_is_immediate,
               _test_settled_rows_are_not_expired,
               _test_no_op_transitions_are_suppressed,
               _test_unreadable_timestamp_is_loud,
               _test_year_rollover,
               _test_system_ask_cleared_on_advance,
               _test_transition_frames_match_settlement_shape,
               _test_deadline_invariant_mirrors_parent_repo,
               _test_producer_wiring,
               _test_reverse_controls):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_task_expiry()
