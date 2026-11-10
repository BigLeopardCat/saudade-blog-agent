"""Render bounded administrator reports and rosters.

Use backend aggregate values for totals. Sanitize user-controlled text before it
enters reports, including command prefixes interpreted by the browser.
"""

from __future__ import annotations

import re
from datetime import datetime

# Bound report text supplied to the model.
MAX_REPORT_CHARS = 2400

# Break command-shaped prefixes in untrusted report fields, including EFFECT variants.
_CMD_WORD_RE = re.compile(
    r"(?i)\b(?:[A-Za-z0-9_]*EFFECT|DARKMODE|NAVIGATE|AUTO_NAVIGATE|SUMMARY|\[?System\]?)"
    r"\s*[:：]")

# U+200B is not whitespace, so it breaks the prefix without matching a \s separator.
_ZWSP = "\u200b"


def sanitize_untrusted(text: str, limit: int = 40) -> str:
    """Collapse lines, bound length and break command prefixes; this is not HTML escaping."""
    s = re.sub(r"\s+", " ", text or "").strip()
    s = _CMD_WORD_RE.sub(
        lambda m: m.group(0).replace(":", _ZWSP + ":").replace("：", _ZWSP + "："), s)
    if limit > 0 and len(s) > limit:
        s = s[:limit] + "…"
    return s


def _cap(text: str) -> str:
    if len(text) <= MAX_REPORT_CHARS:
        return text
    return text[:MAX_REPORT_CHARS] + "\n…（报表过长，已截断）"


def _ts(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M")



# Moderation status: approved is the final state; ai_result is the recorded AI verdict.
# A later review can change approved without changing ai_result.

APPROVED_PENDING, APPROVED_OK, APPROVED_REJECT = 0, 1, 2

# A focused category allows more rows; counts remain independent of the displayed details.
MAX_LIST_DETAIL = 5
MAX_FOCUS_DETAIL = 20

# Keys match the moderation tool's focus parameter.
FOCUS_NAMES = {
    "ai_passed": "AI 直接通过",
    "ai_rejected": "AI 驳回",
    "pending": "需要人工复批",
}


def account_text(r: dict) -> str:
    """Render the publishing account from administrator-only identity fields.

    The public signature is user-entered display text, never evidence of account identity.
    Missing account fields remain unknown."""
    uid = r.get("userId")
    head = f"userId:{uid}" if uid is not None else "userId 读不到"
    nick = sanitize_untrusted(str(r.get("nickname") or ""), 20)
    uname = sanitize_untrusted(str(r.get("username") or ""), 20)
    if not nick and not uname:
        return f"账号（{head}）"
    if not nick or nick == uname:
        return f"账号（{head}／{uname or nick}）"
    if not uname:
        return f"账号（{head}／昵称 {nick}）"
    return f"账号（{head}／昵称 {nick}／用户名 {uname}）"


def _detail_line(r: dict, extra: str = "") -> str:
    """Render a bounded moderation row with a namespaced talk ID and account identity."""
    who = account_text(r)
    sign = sanitize_untrusted(r.get("author") or "", 16)
    if sign:
        who += f"，留名「{sign}」"
    body = sanitize_untrusted(r.get("content") or "", 30)
    at = short_time(r.get("createTime"))
    return f"  · talkId:{r.get('talkKey')} {at} {who}（{extra}）「{body}」"


def _ai_bucket(v) -> str:
    """Map a recorded AI verdict to its bucket; unknown values have no recognized verdict."""
    return v if v in ("pass", "reject", "flag") else "none"


def render_moderation_status(rows: list[dict], status: str | None = None,
                             now: datetime | None = None) -> str:
    """Render independent final-status and AI-verdict counts from protected board rows.

    Detail groups may overlap: pass+approved, AI reject, and pending final status.
    A focus expands one group's details without changing the other groups' counts.
    """
    rows = rows or []
    focus = status if status in FOCUS_NAMES else None
    lines = [f"河灯留言审核状况（{_ts(now)}）"]
    total = len(rows)
    pending = [r for r in rows if r.get("approved") == APPROVED_PENDING]
    passed = [r for r in rows if r.get("approved") == APPROVED_OK]
    rejected = [r for r in rows if r.get("approved") == APPROVED_REJECT]
    lines.append(f"- 总计 {total} 条：待审 {len(pending)}、已通过 {len(passed)}、"
                 f"已驳回 {len(rejected)}")

    ai = {"pass": 0, "reject": 0, "flag": 0, "none": 0}
    for r in rows:
        ai[_ai_bucket(r.get("ai_result"))] += 1
    lines.append(f"- AI 侧判定：通过 {ai['pass']}、驳回 {ai['reject']}、"
                 f"存疑转人工 {ai['flag']}、未走 AI {ai['none']}")

    # A pass verdict with pending final status belongs only in the pending group.
    ai_passed = [r for r in rows if r.get("ai_result") == "pass"
                 and r.get("approved") == APPROVED_OK]
    ai_rejected = [r for r in rows if r.get("ai_result") == "reject"]
    keep_rej = [r for r in ai_rejected if r.get("approved") == APPROVED_REJECT]
    open_rej = [r for r in ai_rejected if r.get("approved") == APPROVED_OK]
    wait_rej = [r for r in ai_rejected if r.get("approved") == APPROVED_PENDING]
    pend_flag = [r for r in pending if r.get("ai_result") == "flag"]
    pend_pass = [r for r in pending if r.get("ai_result") == "pass"]
    pend_none = [r for r in pending if _ai_bucket(r.get("ai_result")) == "none"]

    lines.append("- 三份名单口径不同、**可以重叠**（一条 AI 驳回又还等人复批的留言会同时"
                 "出现在②③）——不要把三个数相加当总数")
    # Explain account identity once instead of repeating the legend in every row.
    lines.append("- 明细里「账号（…）」是**真实发表账号**（发留言必须登录，留空留名的也溯得到）；"
                 "「留名「…」」是留言时自己填的字，**不是账号**，别拿它去认人")

    def _detail(group: list[dict], key: str, extra_of):
        """一类名单的明细行（空名单不写"明细"二字，免得列出个空标题）。"""
        if not group:
            return
        limit = MAX_FOCUS_DETAIL if focus == key else (0 if focus else MAX_LIST_DETAIL)
        if not limit:
            lines.append(f"  · 另有 {len(group)} 条未列出")
            return
        lines.append("  明细:")
        for r in group[:limit]:
            lines.append(_detail_line(r, extra_of(r)))
        rest = len(group) - limit
        if rest > 0:
            lines.append(f"  · 另有 {rest} 条未列出")

    lines.append(f"① AI 直接通过（AI 判通过且已展示，没经过人工）{len(ai_passed)} 条:")
    _detail(ai_passed, "ai_passed", lambda r: "AI通过")
    lines.append(f"② AI 驳回 {len(ai_rejected)} 条：人工维持驳回 {len(keep_rej)}、"
                 f"人工改判放行 {len(open_rej)}、还等着人工复批 {len(wait_rej)}")
    _detail(ai_rejected, "ai_rejected",
            lambda r: {APPROVED_REJECT: "人工已驳回", APPROVED_OK: "人工已改判放行",
                       APPROVED_PENDING: "仍待人工"}.get(r.get("approved"), "人工侧未知"))
    lines.append(f"③ 需要人工复批 {len(pending)} 条：AI 存疑 {len(pend_flag)}、"
                 f"AI 通过但人工闸 {len(pend_pass)}、AI 驳回但人工闸 {len(wait_rej)}、"
                 f"未走 AI {len(pend_none)}")
    _detail(pending, "pending",
            lambda r: {"flag": "AI存疑", "pass": "AI已过待人工", "reject": "AI驳回待人工"}
            .get(r.get("ai_result"), "AI未审"))

    # Disagreements compare the recorded AI verdict with a nonpending final status.
    missed = [r for r in rejected if r.get("ai_result") == "pass"]
    lines.append(f"- 分歧实证（人工已经裁过的）：AI 放过但被人驳回 {len(missed)} 条；"
                 f"AI 驳回但被人放行 {len(open_rej)} 条")
    # Expose supported moderation actions alongside their actual targets.
    lines.append(
        "- **处置**：上面列出的每一条（含已驳回、待审的）都有两条通道——"
        "① 复核/改判：人工通过或驳回，**按 `talkId` 指认**（编号就是上面逐条印出来的"
        "那串数字，原样抄即可）；② 删除（**删掉取不回来**，没有回收站），"
        "按**正文原话**指认（把那条里的一小段原样抄给我）。"
        "两件事分属两条通道，指认方式不同、别混用。"
        "**没有哪一类是删不掉的**——后台清单里看得见的都删得掉。"
        "⚠️ 复核是**改判**、不是「走一遍流程」：待审的可通过可驳回，"
        "**已通过的可再驳回**（收回展示）、已驳回的可恢复通过——"
        "任何一条留言的当前结论都能改，改判同样按 `talkId` 指认。")
    if not pending:
        lines.append("- 当前没有待审留言")
    return _cap("\n".join(lines))


# Administrator rosters preserve account identity separately from the public signature.
# Both the row count and character budget bound displayed details.
BOARD_ROSTER_LIMIT = 20

# Recent rows may be shown separately when a keyword yields no match.
BOARD_ROSTER_FALLBACK = 5


def _roster_line(r: dict) -> str:
    """Render account identity separately from the optional public signature."""
    from tools.base import BOARD_APPROVED_CN
    state = BOARD_APPROVED_CN.get(r.get("approved"), "状态未知")
    sign = sanitize_untrusted(r.get("author") or "", 16)
    body = sanitize_untrusted(r.get("content") or "", 30)
    tail = f"，留名「{sign}」（留言时自己填的，不是账号）" if sign else \
           "，留名未填（匿名发表）"
    return (f"- talkId:{r.get('talkKey')} [{state}] {short_time(r.get('createTime'))} "
            f"{account_text(r)}{tail}「{body}」")


def render_board_roster(rows: list[dict], *, approved: int | None = None,
                        keyword: str | None = None) -> str:
    """Render protected board rows with normalized status and substring filters.

    Caller handles empty input and normalizes approved to 0/1/2 or None.
    State applied filters and distinguish no match from no site data. Recent
    keyword-free fallback rows retain the status filter and are labelled nonmatches.
    """
    rows = rows or []
    kw = (keyword or "").strip()
    hits = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if approved is not None and r.get("approved") != approved:
            continue
        if kw:
            hay = " ".join(str(r.get(k) or "") for k in
                           ("content", "author", "nickname", "username")).lower()
            if kw.lower() not in hay:
                continue
        hits.append(r)

    conds = []
    if approved is not None:
        from tools.base import BOARD_APPROVED_CN
        conds.append(f"状态={BOARD_APPROVED_CN.get(approved, approved)}")
    if kw:
        conds.append(f"关键词「{sanitize_untrusted(kw, 20)}」")
    cond_txt = f"；本次筛选：{'、'.join(conds)}" if conds else "；本次未筛选"

    head = [
        f"河灯留言后台名册（{_ts()}，后台共 {len(rows)} 条{cond_txt}）",
        "- **每条留言都有真实发表账号**：放灯必须登录，留名框里填的字只是自由文本"
        "（留空即匿名）——认人看「账号（…）」，**别拿正文或留名当账号**；"
        "匿名的留言一样溯得到是谁发的。",
    ]
    if not hits:
        head.append(f"- 按上面的条件一条都没匹配上（后台 {len(rows)} 条里筛出来的结果是空的）"
                    "——换个关键词、或不带条件看全部。")
        # A keyword miss may include recent rows as a clearly labelled fallback;
        # retain the approval filter and never report them as keyword matches.
        same_status = [r for r in rows
                       if isinstance(r, dict)
                       and (approved is None or r.get("approved") == approved)]
        if kw and same_status:
            head.append(f"- 关键词是**子串**匹配（不是语义匹配）：上面那几个字在正文/留名/"
                        f"账号里一个都没出现过。下面附上**不带关键词**的最近 "
                        f"{min(len(same_status), BOARD_ROSTER_FALLBACK)} 条"
                        "（**它们不符合上面的关键词**，只是此刻最新的几条，供你判断该换"
                        "哪个词、或直接按内容认人）：")
            for r in same_status[:BOARD_ROSTER_FALLBACK]:
                head.append(_roster_line(r))
            if len(same_status) > BOARD_ROSTER_FALLBACK:
                head.append(f"- 另有 {len(same_status) - BOARD_ROSTER_FALLBACK} 条未列出")
        return _cap("\n".join(head))

    lines = list(head)
    for r in hits[:BOARD_ROSTER_LIMIT]:
        line = _roster_line(r)
        # Leave room for the omitted-row count instead of truncating a roster row.
        if len("\n".join(lines + [line])) > MAX_REPORT_CHARS - 200:
            break
        lines.append(line)
    rest = len(hits) - (len(lines) - len(head))
    if rest > 0:
        lines.append(f"- 另有 {rest} 条未列出（可用 keyword 收窄，或不筛直接看全部）")
    return _cap("\n".join(lines))


def short_time(raw) -> str:
    """Return compact timestamp text, preserving inputs shorter than a full timestamp."""
    s = str(raw or "")
    return s[5:16] if len(s) >= 16 else s[:16]



def render_user_stats(data: dict, now: datetime | None = None) -> str:
    """Render backend user totals; the bounded detail list cannot determine site totals."""
    data = data or {}
    lines = [f"用户数据报表（{_ts(now)}，生成于 {data.get('generatedAt') or '未知'}）"]
    roles = "、".join(f"{r.get('role')} {r.get('count')}"
                      for r in (data.get("roleCounts") or [])) or (
                          "无" if data.get("roleCounts") == [] else "未知")
    lines.append(f"- 用户总数 {data.get('totalUsers', '未知')}（{roles}）")
    lines.append(f"- 会话 {data.get('totalConversations', '未知')} 个、"
                 f"消息 {data.get('totalMessages', '未知')} 条、"
                 f"执行回执 {data.get('totalExecutions', '未知')} 条")
    lines.append(f"- 活跃（有会话或消息）：近 7 天 {data.get('activeUsers7d', '未知')} 人、"
                 f"近 30 天 {data.get('activeUsers30d', '未知')} 人")

    users = data.get("users")
    if users is None:
        lines.append("- 用户活动明细：后台未返回，无法核对")
    elif not users:
        lines.append("- 本次未返回用户活动明细；不能据此判断全站活动")
    else:
        lines.append(f"- 本次明细 {len(users)} 人（按消息数倒序，最多 50 行）；"
                     "未列出的用户活动未知，不能据此认定没有会话或消息:")
        for u in users:
            name = sanitize_untrusted(u.get("name") or "", 20) or f"userId:{u.get('id')}"
            last = u.get("lastActiveAt") or "无活动"
            lines.append(f"  · userId:{u.get('id')} {name}（{u.get('role')}）"
                         f"会话 {u.get('conversations', '未知')}／消息 {u.get('messages', '未知')}"
                         f"／最近 {short_time(last)}")
    return _cap("\n".join(lines))


# Article traffic counts currently visible articles. Backend array order defines rank.
# Keep the displayed ranking limit aligned with Rust note_stats::TOP_N.
_RANK_TOP = 10

_COUNT_WORDS = (("views", "阅读"), ("likes", "点赞"), ("favorites", "收藏"), ("comments", "讨论"))

_TOTAL_WORDS = (("totalViews", "阅读"), ("totalLikes", "点赞"), ("totalFavorites", "收藏"),
                ("totalComments", "讨论"))


def _count_int(v) -> int | None:
    """Return an integer counter or None; booleans and absent counts are not zero."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def _counts_text(row: dict, first: str = "") -> str:
    """Render available counters with the ranking metric first."""
    keys = [k for k, _ in _COUNT_WORDS]
    if first in keys:
        keys.remove(first)
        keys.insert(0, first)
    words = dict(_COUNT_WORDS)
    parts = []
    for k in keys:
        n = _count_int(row.get(k))
        if n is not None:
            parts.append(f"{words[k]} {n}")
    return "／".join(parts)


def _rank_block(head: str, rows: list | None, metric: str) -> list[str]:
    """Render ranks in backend order and distinguish missing data from an empty ranking."""
    word = dict(_COUNT_WORDS)[metric]
    if rows is None:
        return [f"- {head}：本次没读到（后台没有返回这一项）"]
    if not rows:
        return [f"- {head}：全站没有任何文章有{word}记录"]
    # A full bounded ranking does not establish the site-wide article count.
    scope = (f"下列前 {len(rows)} 名，报表最多只列到这里"
             if len(rows) >= _RANK_TOP else f"全站共 {len(rows)} 篇有{word}记录")
    lines = [f"- {head}（按{word}量倒序，{scope}）:"]
    for i, r in enumerate(rows, 1):
        title = sanitize_untrusted(str((r or {}).get("title") or ""), 40) or "（无标题）"
        counts = _counts_text(r or {}, first=metric)
        tail = f" {counts}" if counts else ""
        lines.append(f"  · 第 {i} 名 《{title}》（noteId {r.get('noteId')}）{tail}")
    return lines


def _daily_window_lines(window):
    """Display backend coverage and subtotals without deriving business statistics."""
    if (not isinstance(window, dict) or window.get("scope") != "rolling_window"
            or not isinstance(window.get("start"), str)
            or not isinstance(window.get("end"), str)):
        return ["- 日序列范围：后端未返回覆盖范围，不能据此断言整月合计。"]
    lines = [f"- 本次日序列范围：{window['start']} 至 {window['end']}；"
             "滚动窗口不是自然月整月合计，整月比较需读取对应月报。"]
    subtotals = window.get("monthlySubtotals")
    if isinstance(subtotals, list):
        for row in subtotals:
            if isinstance(row, dict) and isinstance(row.get("month"), str):
                value = _count_int(row.get("views"))
                if value is not None:
                    lines.append(f"- 窗口内{row['month']}小计（不是整月合计）：阅读 {value}")
    return lines


def _daily_lines(data):
    lines = []
    # Missing daily data differs from a measured all-zero series.
    daily = data.get("daily")
    lines += _daily_window_lines(data.get("dailyWindow"))
    rows = [row for row in (daily or []) if isinstance(row, dict)]
    measured = [row for row in rows if any(
        _count_int(row.get(metric)) is not None
        for metric in ("views", "likes", "favorites", "comments"))]
    if daily is None:
        lines.append("- 近 30 天趋势：本次没读到（后台没有返回这一项）")
    elif not daily:
        lines.append("- 日趋势：本次未返回日明细，不能据此断言零访问。")
    elif not measured:
        lines.append("- 日趋势：指标缺失，不能据此断言零访问。")
    else:
        shown = rows[-30:]
        days = "；".join((d.get("date") or "?") + " " + _counts_text(d) for d in shown)
        prefix = f"…（更早还有 {len(rows) - len(shown)} 天）" if len(rows) > len(shown) else ""
        lines.append(f"- 逐日明细（返回 {len(rows)} 个日期，保留零值；未返回日期和指标保持未知）: "
                     f"{prefix}{days}")

    return lines


def render_note_stats(data: dict, now: datetime | None = None) -> str:
    """Render backend totals, independent rankings and the bounded daily series."""
    data = data or {}
    totals = [(w, _count_int(data.get(k))) for k, w in _TOTAL_WORDS]
    lines = [f"文章流量报表（{_ts(now)}，生成于 {data.get('generatedAt') or '未知'}）",
             "- 历史累计合计（只算当前可见文章，不是本期合计）："
             + ("、".join(f"{w} {n}" for w, n in totals if n is not None)
                or "本次没读到合计值")]
    lines += _rank_block("历史累计阅读榜", data.get("topViewed"), "views")
    lines += _rank_block("历史累计点赞榜", data.get("topLiked"), "likes")
    lines += _rank_block("历史累计收藏榜", data.get("topFavorited"), "favorites")

    lines += _daily_lines(data)

    # Each metric ranks independently; positions cannot be shared between rankings.
    lines.append("- 名次口径：阅读/点赞/收藏三张榜各自独立排名（同一篇文章在两张榜上"
                 "的名次可以不同）")
    return _cap("\n".join(lines))


def render_note_daily(data: dict, now: datetime | None = None) -> str:
    """A focused daily view does not spend its bounded space on lifetime rankings."""
    lines = [f"文章日趋势（{_ts(now)}，生成于 {data.get('generatedAt') or '未知'}）"]
    return _cap("\n".join([*lines, *_daily_lines(data)]))


# Publication periods use inclusive start/end bounds and explicit partial coverage.
# Backend totals cover the whole period; bounded ranks cover only its top articles.
_PERIOD_TOP = 5  # Rust note_stats::PERIOD_TOP_N.
# Shared with action_text so operation labels and report titles use the same granularity.
_KIND_CN = {"week": "周报", "month": "月报", "year": "年报"}


def _period_line(p: dict) -> str:
    """一期 → `- 2026-W40（10-01 ~ 10-07）阅读 120／点赞 8／收藏 2`（+ 不完整标记）。"""
    label = str(p.get("label") or p.get("key") or "?")
    span = f"（{short_time(p.get('start'))} ~ {short_time(p.get('end'))}）"
    counts = _counts_text(p, first="views") or "本次没读到合计"
    # Only an explicit true establishes partial coverage; absence does not mean complete.
    flag = "，**本期不完整**（统计起点落在本期中间，只统计了部分天数）" \
        if p.get("partial") is True else ""
    return f"- {label}{span}{counts}{flag}"


def render_note_periods(data: dict, now: datetime | None = None) -> str:
    """Render backend period totals and rankings without summing the bounded top list."""
    data = data or {}
    kind_cn = _KIND_CN.get(str(data.get("kind")), str(data.get("kind") or "分期"))
    lines = [f"文章{kind_cn}（{_ts(now)}，生成于 {data.get('generatedAt') or '未知'}）"]

    # Missing since is unavailable; null explicitly means recording has not started.
    since = data.get("since", "absent")
    if since == "absent":
        lines.append("- 统计起点：本次没读到（后台没有返回这一项）")
    elif since is None:
        lines.append("- 统计起点：站内还**一行阅读记录都没有**，所以给不出任何一期"
                     "（这是「还没开始统计」，不是「这些期没人看」）")
    else:
        lines.append(f"- 统计起点（最早有记录的那天）：{since}")

    periods = data.get("periods")
    if periods is None:
        lines.append("- 分期明细：本次没读到（后台没有返回这一项）")
        return _cap("\n".join(lines))
    if not isinstance(periods, list) or not periods:
        # A known start can still have no subsequent periods.
        if since not in ("absent", None):
            lines.append(f"- 分期明细：**没有一期落在统计起点（{since}）之后**，"
                         f"报表给不出任何一期")
        return _cap("\n".join(lines))

    lines.append(f"- 共 {len(periods)} 期，**最新的在前**：")
    for p in periods:
        if not isinstance(p, dict):
            continue
        lines.append(_period_line(p))
        top = p.get("topNotes")
        if top is None:
            lines.append("    · 本期榜单：本次没读到（后台没有返回这一项）")
            continue
        if not top:
            lines.append("    · 本期一篇被读被赞被收藏的文章都没有")
            continue
        lines.append(f"    · 本期阅读量前 {len(top)} 篇：")
        for i, r in enumerate(top, 1):
            title = sanitize_untrusted(str((r or {}).get("title") or ""), 40) or "（无标题）"
            counts = _counts_text(r or {}, first="views")
            tail = f" {counts}" if counts else ""
            lines.append(f"      · 第 {i} 名 《{title}》（noteId {r.get('noteId')}）{tail}")

    lines.append(f"- 口径：每期的合计是**本期全量**（榜单只列本期阅读量前 {_PERIOD_TOP} 篇，"
                 f"把榜上的行加起来会比合计小）；名次口径是**本期阅读量**，期与期之间"
                 f"各自独立排名（同一篇文章在两期里的名次没有关系）")
    return _cap("\n".join(lines))
