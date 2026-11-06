# -*- coding: utf-8 -*-
"""后台首页待办 / 日程的确定性层。"""

from __future__ import annotations

import datetime
import re

from agent.adminops_tags import (
    clip,
)

_DUE_REL_DAYS = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2,
                 "後天": 2, "大后天": 3, "大後天": 3}
_DUE_DAY_SUFFIX = r"[日号]?"
# 带年份：分隔符宽容（`2026-09-27` / `2026/9/27` / `2026年9月27日` / `2026年9月27号`）
_DUE_FULL_RE = re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*"
                          + _DUE_DAY_SUFFIX + r"$")
# 不带年份：`9月27日` / `9月27号` / `9-27`
_DUE_MONTH_DAY_RE = re.compile(r"^(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*"
                               + _DUE_DAY_SUFFIX + r"$")
DUE_DATE_FORMS_CN = "年-月-日 / 年/月/日 / X月X日（X月X号也算） / 今天·明天·后天"


def normalize_due_date(value, *, today=None) -> str | None:
    """排期日 → `YYYY-MM-DD`；认不出来 → None（**不猜**，调用方零写 + 追问）。

    认这几种写法（都是主人嘴里或 planner 手里真会出现的形态）：
      · `YYYY-MM-DD`（标准口径——planner 手里有 current_time，能算就该直接给这个）
      · `YYYY/M/D`、`YYYY年M月D日`（日位写「号」同样认：`2026年9月27号`）
      · 今天 / 今日 / 明天 / 明日 / 后天 / 大后天
      · `M月D日` / `M月D号` / `M-D`：**按当年**解释。这是条确定的规则、不是猜——无年份时
        分辨不出"去年的 9 月 27"和"今年的 9 月 27"，跨年必须写年份；真解释错了
        还有一道兜底：确认框那一行会把翻出来的日子写出来（「排期 9月27日」），
        主人点确定之前就看得到。
      · **裸的「27号」不认**：「按当月」与上面的「按当年」不是同一条规则，且
        「3号楼」这类东西不带「月」时和日期长得一样——要收它得另立一条规则，
        不在这条日位的同义字里顺手扩。

    `today` 只为测试注入（默认取本地今天，与全站 +08:00 钟面约定一致）。
    """
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not s:
        return None
    if s in _DUE_REL_DAYS:
        base = today or datetime.date.today()
        return (base + datetime.timedelta(days=_DUE_REL_DAYS[s])).isoformat()
    m = _DUE_FULL_RE.match(s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)),
                                 int(m.group(3))).isoformat()
        except ValueError:
            return None                      # 2026-02-30 这类：存在不了一天
    m = _DUE_MONTH_DAY_RE.match(s)
    if m:
        base = today or datetime.date.today()
        try:
            return datetime.date(base.year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    return None


def due_date_cn(iso) -> str:
    """`YYYY-MM-DD` → 「9月27日」；认不出的原样带引号吐回去（不假装它是已知日期）。"""
    s = str(iso or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if not m:
        return f"「{s}」" if s else "（没写日期）"
    return f"{int(m.group(2))}月{int(m.group(3))}日"


def _due_hint(iso: str, today) -> str:
    """排期相对今天的位置 → `（今天）` / `（已过期）` / 空串。

    与 `due_date_cn` 分开：那个只管"怎么念这个日子"，这个管"它相对今天是什么"。
    两个都不是猜——`iso` 是库里读出来的、`today` 是钟面，等宽 ISO 串直接比大小
    （`YYYY-MM-DD` 字典序 = 时间序，这也是 `normalize_due_date` 归一成这个形状的
    理由之一）。判不出来（形状不对）就**不标**，不硬套一个相对说法。
    """
    if len(iso) != 10 or iso[4] != "-" or iso[7] != "-":
        return ""
    now = today or datetime.date.today()
    if iso == now.isoformat():
        return "（今天）"
    return "（已过期）" if iso < now.isoformat() else ""


# 「这两类这一次**没读到**」的哨兵（见 `render_todo_pending`）。
#
# 为什么需要一个哨兵而不是用 `None`：`None` 要表达的其实是两种完全不同的事——
# ① **这一类信息本次不适用**（只渲染那份私人清单的调用方，比如单测与卡面预览）；
# ② **适用、但这一次没读到**（汇总接口挂了）。两者在下游**措辞相反**：①一个字都不印，
# ②必须明说"没读到、不确定"（**且不许顺口断言那两类存在**）。合并成一个值就会让 ① 那一侧凭空多出一句
# "没读到"（而它根本没去读），或者让 ② 静默成"没有待处理的事"——后者正是主人
# 最需要知道有活儿等着他的时候。哨兵让这两种形态在调用点**必须写出来**。
TODO_PENDING_UNREAD = object()


def render_todo_pending(pending) -> str:
    """渲染留言、评论和额度申请的独立待处理计数；未知、缺项与已知零值分别处理。"""
    if pending is None:
        return ""
    if pending is TODO_PENDING_UNREAD:
        return ("（后台汇总这一次没读到，**不确定**有没有待审留言、待审评论或额度重置"
                "申请——这是「没读到」，不是「没有」；要报给主人时如实说没读到）")
    if not isinstance(pending, dict):
        # 形态不对：**不猜**（当成"没提供"），同 `_todo_rows` 的取向。
        return ""
    review = _nonneg_int(pending.get("review"))
    comments = _nonneg_int(pending.get("comments"))
    quota = _nonneg_int(pending.get("quota"))
    parts = []
    if review:
        parts.append(f"留言待审 {review} 条")
    if comments:
        parts.append(f"评论待审 {comments} 条")
    if quota:
        parts.append(f"额度重置申请 {quota} 份")
    if not parts:
        return ""
    lead = {1: "另有一件", 2: "另有两件", 3: "另有三件"}[len(parts)]
    return f"{lead}等着你处理（不在上面这张私人待办里）：" + "、".join(parts) + "。"


def _nonneg_int(v) -> int:
    """`v` → 非负整数；不是整数/读不出 ⇒ 0（**只用于"印不印这一项"**）。

    不接受字符串数字：这一格的两端都是 Python 自己塞的 int（`tools.base` 从汇总里
    取 `pendingReview`/`pendingQuota`），出现字符串说明有人在编数而不是在读。
    """
    if isinstance(v, bool) or not isinstance(v, int):
        return 0
    return v if v > 0 else 0


def render_todo_list(rows, *, today=None, pending=None) -> str:
    """后台待办清单 → narrator 读的一行一条。

    **一行一条 + 字段用 ` | ` 分开**（同 tag 列表族的帧瘦身口径）：narrator 要能
    一眼看出"哪条、哪天、完成没有"，而把这三样揉成一句话会让它开始自己拆句子
    （拆错就把"已完成"读成"未完成"）。序号是**列表里的位次**——它只是给人读的，
    不是 id（这张列表没有稳定的行 id，见 src/routes/todos.rs 头注；任何按序号
    指认某一条的后续动作都做不到，所以 narrator 也**不该**用它指认）。

    `pending` 见 `render_todo_pending`（默认 `None` = 不提供这一类信息）：
    后台首页那两类"等着你处理"的计数（留言待审 / 额度申请），**空清单时也照印**
    ——"一条待办都没记"与"有两件等着你"是两件同时成立的事，不能因为前者为空
    就把后者吞掉（`list_dashboard_todos` 那一侧也正是这么分出口的）。
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    tail = render_todo_pending(pending)
    if not rows:
        head = "后台首页的待办列表是空的（一条都没有）。"
        return f"{head}\n{tail}" if tail else head
    undone = sum(1 for r in rows if not r.get("done"))
    lines = [f"后台首页待办共 {len(rows)} 条（未完成 {undone} 条）："]
    for i, r in enumerate(rows, 1):
        iso = str(r.get("date") or "").strip()
        when = f"排期 {due_date_cn(iso)}{_due_hint(iso, today)}" if iso else "未排期"
        lines.append(f"{i}. {clip(str(r.get('text') or ''), 60)} | {when} | "
                     f"{'已完成' if r.get('done') else '未完成'}")
    if tail:
        lines.append(tail)
    return "\n".join(lines)


def render_todo_added(text: str, date: str | None = None) -> str:
    """生成新增待办回执，逐字保留正文并显示真实编号和排期。"""
    when = f"排期 {due_date_cn(date)}" if date else "未排期"
    return (f"已在后台首页的待办里加了一条「{str(text or '').strip()}」（{when}）"
            f"（后台已复核：列表里读到了这一条）")


def render_todo_when(row) -> str:
    """一行待办的排期短语（`排期 9月28日` / `未排期`）。

    **只此一处**：列表帧、卡面、多条同名时的区分串走的是同一句话——系统对同一行
    的两种表述不一致时，主人在卡上认出的东西与他事后在列表里看到的对不上。
    """
    iso = str((row or {}).get("date") or "").strip()
    return f"排期 {due_date_cn(iso)}" if iso else "未排期"


def _todo_face_row(todos, text) -> tuple[dict | None, int, bool, int]:
    """渲染用的待办快照查询 → `(唯一命中行, 命中条数, 快照是否在手, 列表总条数)`。

    与 `tools.base._todo_text_hits` 同一判据（正文逐字相等），但**只用于渲染**：
    这里是"卡面上写不写得出具体是哪一条"，不是"能不能动"（那是工具侧独立判的
    一次，同 `_match_board` 那条注）。快照不在手（读不到列表）时不编、也不因此
    不弹窗——只印正文。总条数用来把"列表里没有这一条"与"你还没有任何待办"分开
    （要主人做的事不一样：一个是改正文，一个是先去记一条）。
    """
    if todos is None:
        return None, 0, False, 0
    want = str(text or "").strip()
    rows = [r for r in todos if isinstance(r, dict)]
    hits = [r for r in rows if str(r.get("text") or "").strip() == want] if want else []
    return (hits[0] if len(hits) == 1 else None), len(hits), True, len(rows)


def render_todo_done_action(text: str, todos=None) -> str:
    """`把待办「交房租」勾成完成（排期 9月28日，现在：未完成）`——卡面、问句、
    跨轮待办的目标**共用这一行**（同 `render_account_action` 的分工）。

    `todos` 三态（全表同一条纪律：读不到就少说，**不因此不弹窗**）：
      · 快照在手、恰好一条同名 → 印 `排期 …，现在：未完成/已完成`；
      · 快照在手、没有这条 → 印「（你后台首页的待办里没有这一条）」——主人点确定
        **之前**就该看到，而不是点完才被告知没做成；
      · 快照在手、多条同名 → 如实印「（有 N 条都叫这个，分不清是哪一条）」：
        这一条**点下去也办不成**（后端与工具侧都是歧义即零写），而"没有这一条"
        与"分不清是哪一条"要他做的事不一样（一个改正文、一个先去改个说法）；
      · 快照没有（读不到列表）→ 只印正文。
    """
    want = str(text or "").strip()
    act = f"把待办「{want}」勾成完成"
    row, n, have, total = _todo_face_row(todos, want)
    if not have:
        return act
    if row is None:
        if n > 1:
            return f"{act}（有 {n} 条待办都叫这个，分不清是哪一条）"
        if not total:
            return f"{act}（你后台首页的待办列表现在是空的）"
        return f"{act}（你后台首页的待办里没有这一条）"
    return (f"{act}（{render_todo_when(row)}，现在："
            f"{'已完成' if row.get('done') else '未完成'}）")


def render_todo_done(text: str, *, changed: bool = True) -> str:
    """根据真实结果生成完成待办回执，与确认卡使用同一事实来源。"""
    want = str(text or "").strip()
    if not changed:
        return (f"待办「{want}」**本来就是完成状态**，这次没有发生任何变更（没有重复勾）")
    return (f"已把待办「{want}」勾成完成"
            f"（后台已复核：列表里这一条现在就是完成状态）")


_TODO_CLEAR_WORD = "未排期"
# 工具侧**兜底**收的同义词（封闭小集、不做模糊匹配）：planner 没照契约写那个词、而是
# 按主人原话填了「清空」「取消」时，认它比回一句"认不出这个日子"对主人更有用——而漏收
# 一个的代价是"把那条的日子清掉"被答成日期格式错误。**认不出来的一律不是清空**：它们
# 会走"翻不出日期 ⇒ 零写 + 问清哪一天"那条既有的路。
_TODO_CLEAR_DATES = frozenset({_TODO_CLEAR_WORD, "无", "清空", "取消"})


def todo_due_phrase(date) -> str:
    """目标排期 → 「10月8日」/「未排期」（卡面、回执行、跨轮目标行**共用这一行**）。

    与 `render_todo_when` 分工：那个读的是**台账里一行**的现状（`date` 是库里的
    `YYYY-MM-DD` 或空），这个读的是**计划里要写进去的那个值**（可能是清空那个契约词、
    也可能是没翻过的脏值）。两者都必须落到同一句人话上——主人在卡上看到的"改成几号"
    与事后在列表里看到的"排期几号"是同一件事的两种说法。
    """
    s = str(date or "").strip()
    if not s or s in _TODO_CLEAR_DATES:
        return "未排期"
    return due_date_cn(s)


def render_todo_reschedule_action(text: str, date, todos=None) -> str:
    """`把待办「更新简历（国庆后）」的排期改成10月8日（现在：排期 9月28日）`——卡面、问句、
    跨轮待办的目标**共用这一行**（同 `render_todo_done_action` 的分工）。

    与勾完成那张卡同一套三态（快照在手且唯一命中 → 印现状；没有这一条 / 有多条同名 →
    如实说清；快照读不到 → 只印正文，**不因此不弹窗**），差在"现状"那一格印的是
    **当前排期**（`render_todo_when`）而不是完成态——主人核对的是"要改的是不是这一条、
    它现在排在几号"。现状那一格的写法与勾完成那张卡同形（`现在：…`）：两张卡印的是
    同一件事的两种问法，主人不必学两套读法。正文同样**一格都不截**（见
    `render_todo_done_action` 里那段）。
    """
    want = str(text or "").strip()
    act = f"把待办「{want}」的排期改成{todo_due_phrase(date)}"
    row, n, have, total = _todo_face_row(todos, want)
    if not have:
        return act
    if row is None:
        if n > 1:
            return f"{act}（有 {n} 条待办都叫这个，分不清是哪一条）"
        if not total:
            return f"{act}（你后台首页的待办列表现在是空的）"
        return f"{act}（你后台首页的待办里没有这一条）"
    return f"{act}（现在：{render_todo_when(row)}）"


def render_todo_rescheduled(text: str, date, *, changed: bool = True) -> str:
    """根据真实结果生成改排期待办回执，与确认卡使用同一事实来源。"""
    want = str(text or "").strip()
    when = todo_due_phrase(date)
    # 「未排期」不能套进「排在…」这个句式（「本来就排在未排期」不是中文）——清空那一路
    # 的说法与 `_reached_one` 的幂等臂共用同一句（两处印的是同一个事实，不许各写一套）。
    was = f"本来就排在{when}" if when != _TODO_CLEAR_WORD else "本来就没有排期"
    if not changed:
        return (f"待办「{want}」**{was}**，这次没有发生任何变更（没有重复改）")
    return (f"已把待办「{want}」的排期改成{when}"
            f"（后台已复核：列表里这一条现在的排期就是 {when}）")
