# -*- coding: utf-8 -*-
"""排期日的**推导**层（20261020）：主人给的是相对说法时，由**系统**算一个具体日子。

为什么单起一套（而不是并进 `tests/test_todo_schedule.py`）：那一个是"待办这一族"
的端到端套件（工具 + 确认闸 + 图节点 + Rust 源码对账），已经 **2233 行**、在
`eval/module_baseline.json` 里记着只许缩小；而这一层是**纯函数**、一条 IO 都没有，
主题也独立（"主人怎么说一个相对日子"⇄"这条待办怎么落库"）。`run_all.py` 按磁盘
枚举 `tests/*.py`，新文件自动进套件，不必登记。

**与 `normalize_due_date` 的分工是这一族的地基，别把两者合并**：

  · `normalize_due_date` = **照抄**——主人报了一个日子（「9月27日」「明天」），
    系统只做格式归一，认不出就是 `None`（那张反例表在 `test_todo_schedule.py` ①）；
  · `derive_due_date` = **算**——主人给的是相对/指示说法（「两天之内」「下周三」），
    要拿钟面推。

`test_todo_schedule.py` ① 那张表里 `下周三 / 周五 / 九月底 → None` **一条都没动**：
它们仍然不该被**归一**认出来。被**推导**接走是另一条路，两件事在产物上必须分得开
——前者是"主人说了、照抄"，后者是"主人没给具体日子、系统替他排"，卡面与注记的
措辞（「系统替你排的」）就靠这个分。

三种出口（调用方按它区分出处）：

  1. 算得出（含闭集认得的）⇒ 一个 ISO 日子；
  2. 算不出、但**是**排期说法（「过几天」）⇒ **今天**（用户裁决：系统自己挑一个，
     卡面印出来让主人核对）；
  3. 算不出、且**不是**排期说法（往 date 槽里填了不相干的词）⇒ `None`，
     调用方零写 + 追问。

用法：.venv/bin/python tests/test_todo_derive.py
"""
import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from agent import adminops_confirm as _confirm  # noqa: E402
from agent import adminops_todo as _todo  # noqa: E402
from agent.workflow_plan import instantiate_plan  # noqa: E402

_gsrc = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# 固定钟面：相对日期的断言才可复现。**2026-09-26 是星期六**（下面周几那几条全靠它）。
TODAY = datetime.date(2026, 9, 26)


# ══════════════════════════════════════════════════════════════════
print("\n① 推导先跑一遍归一（它是排期日的**总解析器**，调用方不必自己判断走哪条路）")

check("★ 闭集认得的写法两个函数逐字节同结论",
      all(_todo.derive_due_date(x, today=TODAY) == _todo.normalize_due_date(x, today=TODAY)
          for x in ("2026-09-27", "2026/9/27", "9月27日", "今天", "明天", "后天")))
check("  归一认不出的（那张反例表）**归一层仍然不认**——推导不是把归一放宽",
      all(_todo.normalize_due_date(x, today=TODAY) is None
          for x in ("下周三", "周五", "九月底", "两天之内")))


# ══════════════════════════════════════════════════════════════════
print("\n② 相对天数族：N天之内 / N天内 / N天以内 / N天后 / 基底词+期限")

check("★ 「两天之内」= 今天 +2（本次事故原句里那个说法）",
      _todo.derive_due_date("两天之内", today=TODAY) == "2026-09-28",
      str(_todo.derive_due_date("两天之内", today=TODAY)))
check("  同一族的其它写法（N天内 / N天以内 / N天后 / 中文数字）",
      _todo.derive_due_date("2天内", today=TODAY) == "2026-09-28"
      and _todo.derive_due_date("三天以内", today=TODAY) == "2026-09-29"
      and _todo.derive_due_date("5天后", today=TODAY) == "2026-10-01",
      str([_todo.derive_due_date(x, today=TODAY)
           for x in ("2天内", "三天以内", "5天后")]))
check("  基底词 + 期限（「今天之内」不是明天，是今天）",
      _todo.derive_due_date("今天之内", today=TODAY) == "2026-09-26"
      and _todo.derive_due_date("后天以前", today=TODAY) == "2026-09-28")


# ══════════════════════════════════════════════════════════════════
print("\n③ 周几族：三个分支的语义**不一样**，别统一成「下一个周五」")

# TODAY = 2026-09-26 是星期六。
check("★ 周几三态各算各的（下下周五 / 下周五 / 周五）",
      _todo.derive_due_date("下下周五", today=TODAY) == "2026-10-09"
      and _todo.derive_due_date("下周五", today=TODAY) == "2026-10-02"
      and _todo.derive_due_date("周五", today=TODAY) == "2026-10-02",
      str([_todo.derive_due_date(x, today=TODAY)
           for x in ("下下周五", "下周五", "周五")]))
check("  已过去的那一天不落一个过去的日期（今天就是周六 ⇒ 「周六」是今天）",
      _todo.derive_due_date("周六", today=TODAY) == "2026-09-26"
      and _todo.derive_due_date("这周六", today=TODAY) == "2026-09-26")
check("  「下周」不带周几 = 下周一；别名族（星期/礼拜）同一条路",
      _todo.derive_due_date("下周", today=TODAY) == "2026-09-28"
      and _todo.derive_due_date("星期日", today=TODAY) == "2026-09-27"
      and _todo.derive_due_date("礼拜二", today=TODAY) == "2026-09-29",
      str([_todo.derive_due_date(x, today=TODAY)
           for x in ("下周", "星期日", "礼拜二")]))


# ══════════════════════════════════════════════════════════════════
print("\n④ 月/日号族：月底 / 某月某号 / 裸的日号（取**下一次**）")

check("★ 月底族（月底 / 这个月底 / 下个月底 / 九月底 各不相同）",
      _todo.derive_due_date("月底", today=TODAY) == "2026-09-30"
      and _todo.derive_due_date("这个月底", today=TODAY) == "2026-09-30"
      and _todo.derive_due_date("下个月底", today=TODAY) == "2026-10-31"
      and _todo.derive_due_date("九月底", today=TODAY) == "2026-09-30",
      str([_todo.derive_due_date(x, today=TODAY)
           for x in ("月底", "这个月底", "下个月底", "九月底")]))
check("★ 某月某号 / 下个月某号 / 下个月（不带号 = 1 号）",
      _todo.derive_due_date("10月8号", today=TODAY) == "2026-10-08"
      and _todo.derive_due_date("下个月3号", today=TODAY) == "2026-10-03"
      and _todo.derive_due_date("下个月", today=TODAY) == "2026-10-01",
      str([_todo.derive_due_date(x, today=TODAY)
           for x in ("10月8号", "下个月3号", "下个月")]))
check("★ 裸的日号取**下一次**：本月还没到就是本月，已经到了就是下个月",
      _todo.derive_due_date("27号", today=TODAY) == "2026-09-27"
      and _todo.derive_due_date("3号", today=TODAY) == "2026-10-03",
      str([_todo.derive_due_date(x, today=TODAY) for x in ("27号", "3号")]))


# ══════════════════════════════════════════════════════════════════
print("\n⑤ 负控与兜底：推导**只接排期说法**，别的一概不认")

# 这几条与 `test_todo_schedule.py` ① 那张表**是同一批反例**（`3号楼` / `5号窗口`）：
# 归一不认，推导也不许认。它们看着像日子、其实不是，推导接走就等于"主人没说要排期，
# 系统替他排了今天"。
check("★ 不是排期说法 ⇒ None（`3号楼` / `5号窗口` / `有空的时候`）",
      _todo.derive_due_date("3号楼", today=TODAY) is None
      and _todo.derive_due_date("5号窗口", today=TODAY) is None
      and _todo.derive_due_date("有空的时候", today=TODAY) is None
      and _todo.derive_due_date("abc", today=TODAY) is None,
      str([_todo.derive_due_date(x, today=TODAY)
           for x in ("3号楼", "5号窗口", "有空的时候", "abc")]))
# ⚠️ 这个 `None` 是**承重**的，不是顺手写的：`_derive_month` 里"匹配上了、但那天不存在"
# 也是 `None`（哨兵 `_DUE_NO_MATCH` 与它严格分开）。两者混成一个，`9月31日` 就会一路
# 落到"是排期说法但算不出 ⇒ 默认今天"那一格——主人写错的日子被悄悄改成今天，而卡面
# 上一切正常。下面这一条就是那条分界线的正控。
check("★ 像日子却不存在 ⇒ None（**不落默认今天**：写错的日子不许被悄悄改成今天）",
      _todo.derive_due_date("9月31日", today=TODAY) is None
      and _todo.derive_due_date("2026-02-30", today=TODAY) is None
      and _todo.derive_due_date("11月31日", today=TODAY) is None,
      str([_todo.derive_due_date(x, today=TODAY)
           for x in ("9月31日", "2026-02-30", "11月31日")]))
check("★ 是排期说法但算不出 ⇒ **今天**（单一策略旋钮：主人表达了排期意图，系统给个可改的落点）",
      _todo.derive_due_date("过几天", today=TODAY) == "2026-09-26"
      and _todo.derive_due_date("尽快", today=TODAY) == "2026-09-26"
      and _todo.derive_due_date("这两天", today=TODAY) == "2026-09-26",
      str([_todo.derive_due_date(x, today=TODAY)
           for x in ("过几天", "尽快", "这两天")]))
check("  空 / 非串 / 布尔同归一口径（不是排期说法就是 None，绝不猜）",
      _todo.derive_due_date("", today=TODAY) is None
      and _todo.derive_due_date(None, today=TODAY) is None
      and _todo.derive_due_date(True, today=TODAY) is None)


# ══════════════════════════════════════════════════════════════════
print("\n⑥ 接线：展开层把**派生过**这件事记进计划（`instantiate_plan` 是纯函数，本节零 IO）")

# 这一节回答的是"推导层接上了没有"，与 `test_todo_schedule.py` ② 那一节（展开层
# 怎么处理**主人说了的日子**）不重叠。派生值不只是一个日子，还带着**出处**：
# `derived_values` 非空 = 这个日子是系统替主人排的 ⇒ 卡面与注记都必须写明，
# 否则等于系统替主人改了字、还让他以为是自己说的。
_add = instantiate_plan("dashboard_todo_add",
                        {"text": "发布泠月喵正式版第一版", "date": "两天之内"})
check("⭐ 相对说法 ⇒ 展开成一条真调用（**不再是**零工具死局轮）",
      len(_add.get("tools") or []) == 1, str(_add.get("tools")))
check("  日子由系统算出来，进的是 `date` 那一格",
      '"date"' in str(_add.get("tools")) and _todo.derive_due_date("两天之内") in str(
          _add.get("tools")),
      str(_add.get("tools")))
check("★ 且**出处**被记下来（`derived_values` 是这一批新增的那条通道）",
      (_add.get("derived_values") or {}).get("date") == "derived",
      str(_add.get("derived_values")))
check("  注记写明这日子是**系统替你排的**（narrator 才不会把它说成主人说的）",
      "系统替你排的" in str(_add.get("note")), str(_add.get("note"))[:90])
check("  反控：主人**没提日子** ⇒ 留空（未排期），不许顺手派一个今天",
      (instantiate_plan("dashboard_todo_add", {"text": "随便一件事"}).get("derived_values")
       or {}) == {}
      and "date" not in str(instantiate_plan("dashboard_todo_add",
                                             {"text": "随便一件事"}).get("tools")),
      str(instantiate_plan("dashboard_todo_add", {"text": "随便一件事"}).get("tools")))
check("  反控：**不是排期说法** ⇒ 零工具 + 追问，且**没有**派生值可记",
      (instantiate_plan("dashboard_todo_add",
                        {"text": "随便一件事", "date": "3号楼"}).get("tools") or []) == [])

_res = instantiate_plan("dashboard_todo_reschedule", {"text": "发布", "date": "下周三"})
check("⭐ 改排期那一件同一套三态（相对说法 ⇒ 算一个 + 记出处）",
      len(_res.get("tools") or []) == 1
      and (_res.get("derived_values") or {}).get("date") == "derived"
      and "系统替你排的" in str(_res.get("note")),
      f"{_res.get('tools')} / {_res.get('derived_values')}")
check("  反控：清空排期那个契约词**不是**派生值（那是主人明说的，不是系统算的）",
      (instantiate_plan("dashboard_todo_reschedule",
                        {"text": "发布", "date": _todo._TODO_CLEAR_WORD})
       .get("derived_values") or {}) == {})
check("  反控：勾完成那一件没有日期槽 ⇒ 第三项恒空（同族同形，调用点按一个形状解包）",
      (instantiate_plan("dashboard_todo_done", {"text": "发布"}).get("derived_values")
       or {}) == {})


# ══════════════════════════════════════════════════════════════════
print("\n⑦ 卡面：主人点「确定」时看得见那个日子是**系统替他排的**")

# 派生值一路传到弹卡那三个渲染点（问句 / 跨轮 `target` / 卡面正文），三处必须
# **同一份出处**——否则卡面写着「系统替你排的」而台账那一行不写，主人事后对账会
# 以为那个日子是自己说的。三处都从 `graph._derived_values` 取（经 `plan_view`）。
_spec = [{"tool": "create_dashboard_todo",
          "args": {"text": "发布泠月喵正式版第一版", "date": "2026-10-22"}}]
_dv = {"date": "derived"}
check("★ 问句写明（卡面上主人核对的就是这一句）",
      "（系统替你排的）" in _confirm.render_confirm_question(_spec, derived=_dv),
      _confirm.render_confirm_question(_spec, derived=_dv)[:120])
check("★ 跨轮 `target`（Rust 台账那一行）同样写明",
      "（系统替你排的）" in _confirm.render_action_lines(_spec, derived=_dv),
      _confirm.render_action_lines(_spec, derived=_dv))
check("★ 卡面正文同样写明",
      "（系统替你排的）" in _confirm.render_confirm_text(_spec, derived=_dv))
check("  且台账那一行**不带 markdown 标记**（那一列的读端是 Rust 的表格）",
      "**" not in _confirm.render_action_lines(_spec, derived=_dv))
check("  反控：**不传** derived（旧调用点 / 主人自己说的日子）⇒ 逐字同旧行为",
      "系统替你排的" not in _confirm.render_confirm_question(_spec)
      and "系统替你排的" not in _confirm.render_action_lines(_spec)
      and "系统替你排的" not in _confirm.render_confirm_text(_spec))
check("  反控：主人**没说**日子（未排期）⇒ 不写「系统替你排的」（那是另一件事）",
      "未排期" in _confirm.render_action_lines(
          [{"tool": "create_dashboard_todo", "args": {"text": "发布"}}], derived=_dv)
      and "系统替你排的" not in _confirm.render_action_lines(
          [{"tool": "create_dashboard_todo", "args": {"text": "发布"}}], derived=_dv))
check("  接线：`_confirm_popup` 三处渲染点都从 `_derived_values(state)` 取（同一份出处）",
      _gsrc.count("_derived_values(state)") >= 3)


print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
