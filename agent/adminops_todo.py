# -*- coding: utf-8 -*-
"""后台首页待办 / 日程的确定性层。

批 3a 从 `agent/adminops.py` 按域拆出（原文逐字节保留）。日期在这一层翻：
主人嘴里的时间词翻不出来就返回 `None` 让调用方零写 + 追问，绝不挑默认日期顶上。"""

from __future__ import annotations

import datetime
import re

from agent.adminops_tags import (
    clip,
)

# ── 后台首页待办 / 日程（20260926）──────────────────────────────────────
# 这张列表的存储口径（线上 `{text, done, date}`、`date` = `YYYY-MM-DD` 或 null）
# 见 src/routes/todos.rs 与 scripts/migration/dashboard_todo_20260924.sql：**前端
# 拥有那份列表的顺序与身份**（拖拽换位、空行回收、行文本就地编辑），服务端只存
# "此刻的这份列表"。agent 手里没有那份列表，所以它只有**追加一条**这一条通道
# （POST /api/protected/todos/item）——整份覆盖会抹掉主人刚做的改动。
#
# 日期为什么在这一层翻：主人嘴里说的时间词（"明天""后天"）是最常见的形态，而
# 日期一旦错一天，这条日程会静静地躺在错误的格子里（后台首页的日历就是按它画点
# 的）。翻不出来就**返回 None 让调用方零写 + 追问**，绝不挑一个默认日期顶上。
_DUE_REL_DAYS = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2,
                 "後天": 2, "大后天": 3, "大後天": 3}
# 末尾那个 `[日号]?` 里的「号」是 20261008 补的：**「日」与「号」在这句人话里同义**，
# 而「X月X号」才是主人嘴里更常见的形态——`workflow_defs/` 的能力文案自己举的例句就是
# 「把〈…〉挪到 10 月 8 号」，旧表却不认它 ⇒ 主人明明说了日子，助手反问「你说是哪一天」。
# 只在这两个形态的**日位**加一个字：裸的「8号」仍不认（那要另立一条"按当月"的规则，
# 与「不带年份按当年」不是同一件事，别顺手扩）。
_DUE_DAY_SUFFIX = r"[日号]?"
# 带年份：分隔符宽容（`2026-09-27` / `2026/9/27` / `2026年9月27日` / `2026年9月27号`）
_DUE_FULL_RE = re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*"
                          + _DUE_DAY_SUFFIX + r"$")
# 不带年份：`9月27日` / `9月27号` / `9-27`
_DUE_MONTH_DAY_RE = re.compile(r"^(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*"
                               + _DUE_DAY_SUFFIX + r"$")
# 「认哪些写法」这句人话**只写一次**（四处报错文案共用）：它此前手抄在
# `workflow_defs/`×2 与 `tools/base.py`×2，四处已经开始各漂各的——改一处漏一处，就是
# "系统告诉模型的面"与"真正认的面"不是同一张（同族病：`_ITEM_POSITION_FORMS`）。
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
    """待处理的那几类（留言待审 / 评论待审 / 额度重置申请）→ **一行**；`None` ⇒ 空串。

    **为什么这几类要在这一帧里**（20260929 用户要求「让 agent 能感知到待审核留言和
    对话额度批准、问待办日程它能说出来」；20261019 补**评论**——它在后台首页是**第三
    行**、与留言并排，所以 agent 少读它一次就少一件主人眼里"等着我处理的事"）：后台首页
    把它们与私人待办**并排**显示，主人眼里那就是"我日程上等着我处理的事"；但它们**不是**
    `dashboard_todo` 表的行（不落库、不进 todos，见父仓
    `frontend/src/pages/Dashboard/Home/index.tsx` 那几行的注释——父仓的路径）。它们真正的来源是同一份
    红点汇总（`/notifications/summary` 的 `pendingReview` / `pendingComments` /
    `pendingQuota`）——所以这一帧读的是**红点那个源**，不另造一份计数
    （"红点与列表是两套数据源"那件事的教训：能同源就同源）。

    ⚠️ **留言与评论是两行、不是一行**（20261019）：两张表（`talk` / `note_comment`）、
    两个后台页签（`?tab=review` / `?tab=comment`）、两个字段名。合并成一个数就会让
    narrator 把"评论还有 3 条"说成"留言还有 3 条"——而主人点进去看到的是另一个页面。

    四态，与 `_quota_pair` 同一条纪律——**读不到 ≠ 0**：
      · `pending is None` ⇒ 空串（这一类信息本次不适用，调用方没提供）；
      · `pending is TODO_PENDING_UNREAD` ⇒ 明说"没读到、不确定"，
        **绝不**留空（留空会被读成"没有待处理的事"）；
      · 各类都是 0 ⇒ 空串（这时"没有"就是事实，不必占一行）；
      · 有一类 > 0 ⇒ 只印 > 0 的那几类（印"留言待审 0 条"是噪声，且会让 narrator
        开始念零）。
    """
    if pending is None:
        return ""
    if pending is TODO_PENDING_UNREAD:
        # ⚠️ **不许断言那两类存在**（20261019 修）。旧版写的是「另有两类等你处理的待办
        # ——留言待审与额度重置申请——这一次没读到，不确定有几条」：它一边说没读到、
        # 一边**把"确实有这两类在等你"说死了**。主人在额度申请为 0 的时候反复读到
        # 「有两类待办」，正是从这句来的（生产 trace 20261018 那几轮）。
        # 没读到就只陈述"没读到"这一件事，存在性留给真读到的那一次说。
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
    # 件数**按实际印出来的那几类数**，不写死"两件"（20261019 修）：旧版恒印「另有两件」，
    # 而额度那一类为 0 时下面只列出一条 ⇒ 模型照着念成「另有两件…留言待审 1 条」，
    # 主人读到的是"还有两件事"却只看见一件（生产 trace 20261018T232550 就是这个形状）。
    # 中文量词按**类数**取（一类/两类/三类），仍然不写死——20261019 加评论族之后
    # 三类的组合真的会出现，所以这里是一张表而不是三元表达式。
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
    """追加成功的回执行。**正文一字不改地进回执**（20260926 修，见本节上方那段
    "正文是唯一的指认方式"）——回执行是 narrator 唯一的取值来源，它截到 60 字，
    narrator 就**只能**照着那个截断版念给主人（真实现场：trace `20260926T094843`，
    主人要记的那条 150 字，回执给的是「…/20260926/2…」、回复里也是「…2…」，
    主人问"为什么和我要的内容不一样、显示截断了"）。`_TODO_TEXT_LIMIT` = 200 字，
    回执行整行 ~240 字，没有撑爆谁的问题；要截也必须像 `render_notice_status`
    那样**标注节选**，不能截了还读成完整的。
    **说清加的是什么、排在哪天**，并点明它在哪（后台首页的待办卡）——这条是主人
    自己那份列表，不是对外可见的东西，回执行不必吓唬他，但必须让他知道去哪儿看。"""
    when = f"排期 {due_date_cn(date)}" if date else "未排期"
    return (f"已在后台首页的待办里加了一条「{str(text or '').strip()}」（{when}）"
            f"（后台已复核：列表里读到了这一条）")


# ── 待办"勾完成"（20260926 第十轮）────────────────────────────────────────
# 这一件的卡面有**一条硬要求**（不是文风问题，同账号族那三条）：`text` 一字不改地
# 进卡面。主人点「确定」之前唯一能核对的就是"要勾的是不是这一条"，而这张列表没有
# 行号、没有标题，正文就是他认得的全部；把它润色/截短等于让他盲签（"交房租"被写成
# "交房"仍点得下去）。
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
    # **一格都不截**（20260926 修）：上面那三行注释写的就是这条要求，此前实现的却是
    # `clip(want, 60)`——注释与代码互相打脸了一个月，而"卡面印的是截断版"这件事在
    # 长正文上是**真的会让人盲签**（主人核对的是「…」前面那 60 字，系统勾的是整条）。
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
    """勾完成成功后的回执行（工具侧用；与卡面同源同事实）。

    `changed=False` = 写前它就是完成态（后端那次是真 no-op，走的是幂等分支）——
    **必须与"刚勾的"分开说**（同 `account_change_phrase`）：主人这一下什么都没
    发生，短路成"已勾成完成"会让回执读成一个动作。

    正文同样**不截**（与卡面同源同事实，20260926）：回执行是下一轮 narrator 的
    取值来源，截了它 narrator 就只能念截断版（`render_todo_added` 里那次真实现场
    的同一个道理）。
    """
    want = str(text or "").strip()
    if not changed:
        return (f"待办「{want}」**本来就是完成状态**，这次没有发生任何变更（没有重复勾）")
    return (f"已把待办「{want}」勾成完成"
            f"（后台已复核：列表里这一条现在就是完成状态）")


# ── 待办"改排期"（20260929 批 G）──────────────────────────────────────────
# 「清空排期」这条通道上的**契约写法**：改排期与新增不一样——新增那次"没给 date"就是
# 没排期，而这里**必须**分出三种处境：给了日子 / 说了不要排期 / 压根没说（第三种要
# 回头问主人，见 `workflows._expand_todo_reschedule_workflow`）。所以"清掉"要有一个**说得出
# 口**的写法，就是这一个词；工具侧另收几个明显同义的词兜底（封闭小集，见
# `tools.base._TODO_CLEAR_DATES`），但契约只有这一个。
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
    """改排期成功后的回执行（工具侧用；与卡面同源同事实）。

    `changed=False` = 写前它就排在那一天（后端那次是真 no-op，走的是幂等分支）——
    **必须与"刚改的"分开说**（同 `render_todo_done`）：主人这一下什么都没发生，短路成
    "已改成…"会让回执读成一个动作，而下一轮 narrator 就照着它讲。

    正文**不截**（与卡面同源同事实，同 `render_todo_done` 的论证）。
    """
    want = str(text or "").strip()
    when = todo_due_phrase(date)
    # 「未排期」不能套进「排在…」这个句式（「本来就排在未排期」不是中文）——清空那一路
    # 的说法与 `_reached_one` 的幂等臂共用同一句（两处印的是同一个事实，不许各写一套）。
    was = f"本来就排在{when}" if when != _TODO_CLEAR_WORD else "本来就没有排期"
    if not changed:
        return (f"待办「{want}」**{was}**，这次没有发生任何变更（没有重复改）")
    return (f"已把待办「{want}」的排期改成{when}"
            f"（后台已复核：列表里这一条现在的排期就是 {when}）")
