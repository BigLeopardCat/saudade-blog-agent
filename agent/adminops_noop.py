# -*- coding: utf-8 -*-
"""「状态已达成」判据：已经就是那个样子就别弹卡、别执行。

批 3a 从 `agent/adminops.py` 按域拆出（原文逐字节保留）。fail-open 的方向永远是
弹卡：宁可多问一次，也绝不静默拒绝一次主人要的写。"""

from __future__ import annotations

from agent.adminops_account import (
    _account_row,
    _board_row_by_id,
    _quota_target_row,
    _row_frozen,
    _row_muted,
    normalize_role,
    role_cn,
)
from agent.adminops_comment import (
    _comment_row_by_id,
)
from agent.adminops_quota import (
    quota_limit,
    quota_used,
)
from agent.adminops_tags import (
    BOARD_VERDICT_APPROVED,
    BOARD_VERDICT_CN,
    find_tag,
    level_cn,
    match_tag_color,
    normalize_status,
    normalize_top,
    normalize_verdict,
    parse_tag_ids,
    render_tag_list,
    status_cn,
    top_cn,
)
from agent.adminops_target import (
    _name_list,
)
from agent.adminops_todo import (
    _TODO_CLEAR_DATES,
    _TODO_CLEAR_WORD,
    _todo_face_row,
    render_todo_when,
    todo_due_phrase,
)

# ── 「状态已达成」判据（20260926）：已经就是那个样子就别弹卡、别执行 ──────────
# 要治的病（用户实测）：对一篇**已经收藏**的文章说"收藏这篇"，系统照样弹卡，点确定
# 还照样走一遍写通道——回执倒是诚实的（工具层短路说"本来就在你的收藏夹里"），可主人
# 看到的是"一次要不要办的询问 + 一次其实没办的往返"。同一形态在八个族里都有。
#
# 判据**只在「读得到现状 + 认得出目标 + 取值就是目标」三条同时成立**时才算已达成；
# 任何一条不成立（读失败 / 同名多条 / 目标不在快照里 / 参数认不出 / 工具自己会拒绝
# 这个组合）一律返回 None ⇒ 走原路**照弹卡**。fail-open 的方向永远是弹卡：宁可多问
# 一次，也**绝不静默拒绝一次主人要的写**（"我以为它办好了"比"它多问了我一句"贵得多）。
#
# 与工具层短路的分工（两边都留着，不是重复实现）：工具层管"真被调用时怎么写"，
# 这里管"该不该弹这张卡、该不该发这次请求"。两边的现状取值必须来自**同一份快照**
# （note_index / board_index / users / todos / favorites / notifications / messages
# / 标签字典 / 公告清单）——各读各的必然出现"卡说已达成、工具却照写"的分裂。
#
# **判据要看这一下真正消耗掉的那件事**（20261001 加，代价见下）：写分两种——
# ① **改一个值**（收藏、冻结、置顶、排期…）：消耗的是"那个值现在还不是目标值"，
#    判据就是拿现状值比目标值；
# ② **处理队列里的一行**（批准/驳回额度申请、复核留言…）：消耗的是**那一行本身**
#    （服务端按 `WHERE status=0` 认领），判据只能是"那一行还在不在待办态"。
# 把 ② 按 ① 判会得出一个**看起来对、其实把门锁死**的结论：批准曾经拿"计数器就是 0"
# 当已达成（那是这个动作**顺带产生的效果**，不是它消耗的东西），于是「满额账号交了
# 一份申请」被判成没什么可办的 ⇒ 不弹卡、零调用，而那一行**还挂在待处理态**；
# 主人再说一次判据还是同一句 ⇒ **这条申请永远没有通道能办**（trace
# `20261001T105116` 实证，用户报"不作为会一直挂起"）。队列型的判据因此一律问
# "那一行还在吗"，且**快照读不到时照弹**——读不到当成"没有那一行"就是同一个洞。
#
# 现状话术的**一条硬要求**：只写状态、不写动作完成式。判据说"现在就是这个样子"，
# 说成「已完成 / 已收藏 / 已标记」就会被读成"系统替我做过了一次"（那正是这一族的
# 另一半病）。词表见下面各族返回的那几句与 tests/test_idem_noop.py 的负断言。


def reached_specs(specs, *, index=None, boards=None, notes=None, users=None, todos=None,
                  favorites=None, notifications=None, messages=None,
                  announcements=None, quota_requests=None,
                  comments=None) -> tuple[list, list]:
    """写 spec 清单 → `(该弹卡的, 已达成)`。

    纯函数（无网络、无 LLM、无 config）：快照由调用方读好传进来——它们本来就都是
    `_confirm_popup` 为了**渲染卡面**已经读到手的那几份（账号名录/待办/文章清单/
    留言清单），只有收藏、通知、站内信、公告四条是这一批**新加**的惰性读。

    `quota_requests`（20260929 加的第五份惰性读，形态 `{uid: 行}`）是额度三件在
    这里唯一用得上的快照：批准与主动重置判"额度本来就是满的"，驳回判"他现在没有
    待处理的申请"。`used` 那半来自**名字通道本来就要读的** `users`（`TempUserInfo`
    新带的 `chatQuotaUsed`）⇒ 判据不多花一次请求。

    `already` 的每一项是 `{"tool", "args", "why"}`，`why` 是给主人看的那半句现状
    （「文章 12《…》本来就在你的收藏夹里」）——掏空那一轮的整体文案与混合轮末尾那句
    补充都由它拼出来（`render_noop_text` / `render_already_note`），**不各写一套**。

    ⚠️ 快照**入参形态以 `_confirm_popup` 传的原样为准**，几族并不统一（别按"看起来
    该是什么形状"改，改了就是判不出）：`notifications` 要的是 `{id: 行}` 映射
    （`tools.base._note_items` 的产物），而 `messages` 要的是**接口原始返回**
    （`{inbox, outbox, unread}`）——它上面还有第二个事实（未读封数）是工具的写前
    基线，所以那半由 `_mailbox_inbox` 在这里现取。其余几族见各臂内的取值。
    """
    snaps = {"index": index, "boards": boards, "notes": notes, "users": users,
             "todos": todos, "favorites": favorites, "notifications": notifications,
             "messages": messages, "announcements": announcements,
             "quota_requests": quota_requests, "comments": comments}
    kept: list = []
    already: list = []
    for spec in specs or []:
        if not isinstance(spec, dict):
            kept.append(spec)
            continue
        args = spec.get("args") if isinstance(spec.get("args"), dict) else {}
        why = _reached_one(str(spec.get("tool") or ""), args, snaps)
        if why:
            already.append({"tool": spec.get("tool"), "args": args, "why": why})
        else:
            kept.append(spec)
    return kept, already


def _reached_one(tool: str, a: dict, s: dict) -> str | None:
    """单个写 spec 的「现在就已经是你要的样子了吗」→ 现状话术 | None（判不了）。"""
    # 函数内导入：tools.base 在导入链的上游（它自己延迟导入本模块），模块级互导成环。
    from tools.base import _as_article_id, _as_bool, _as_ids, _mailbox_inbox
    if tool in ("add_favorite", "remove_favorite"):
        rows = s.get("favorites")
        if not isinstance(rows, list):
            return None                      # 读不到收藏夹 ⇒ 判不了
        aid = _as_article_id(a.get("article_id"))
        if aid is None:
            return None
        hit = None
        for r in rows:
            if isinstance(r, dict) and _as_article_id(r.get("noteId")) == aid:
                hit = r
                break
        want = tool == "add_favorite"
        if want == (hit is None):
            # 要收藏而它不在 / 要取消而它在 ⇒ 正是要办的那次写，照弹。
            return None
        t = str((hit or {}).get("title") or "").strip()
        title = f"《{t}》" if t else ""
        if want:
            return f"文章 {aid}{title}本来就在你的收藏夹里"
        return f"文章 {aid}本来就不在你的收藏夹里"
    if tool in ("read_notifications", "read_messages"):
        box = s.get("notifications") if tool == "read_notifications" else s.get("messages")
        if tool == "read_messages":
            # 信箱快照回的是原始返回（它还带着未读封数给工具当写前基线），
            # 收件箱那半用**与工具同一个函数**取，不在这里重解一遍形态。
            box = _mailbox_inbox(box) if isinstance(box, dict) else None
        if not isinstance(box, dict):
            return None
        if _as_bool(a.get("all")) is True:
            for r in box.values():
                # `is not True`（不是 falsy 判据）：形态脏的行算"没读到已读"，照弹。
                if not isinstance(r, dict) or r.get("isRead") is not True:
                    return None
            if tool == "read_notifications":
                return "你的通知本来就没有未读的"
            return "你的收件箱本来就没有未读的信"
        ids = _as_ids(a.get("ids"))
        if not ids:
            return None                      # 没说清是哪几条（工具也会拒绝）
        for i in ids:
            if i not in box:
                # 名单外的 id 走的是工具的 not_found 那条路（"最近 100 条里没有"），
                # 那不是"已达成"，是一条要如实说清的失败——照弹。
                return None
            if box[i].get("isRead") is not True:
                return None
        noun = "通知" if tool == "read_notifications" else "站内信"
        return f"{noun} {'、'.join(str(i) for i in ids)} 本来就是已读状态"
    if tool in ("freeze_account", "unfreeze_account"):
        users = s.get("users")
        if not isinstance(users, dict) or not users:
            return None
        name = str(a.get("name") or "").strip()
        row = _account_row(users, name)
        if row is None:
            return None                      # 名录里没这个名字 ⇒ 工具的拒绝路，不是已达成
        frozen = _row_frozen(row)
        if frozen is None:
            return None                      # 状态读不出（不许当成"正常"）
        want = tool == "freeze_account"
        if bool(frozen) != want:
            return None
        # 状态词**不带「已」**（见本节头注那条硬要求）：写「现在就是冻结状态」而不是
        # 「已冻结」——后者在气泡里读起来像"系统刚替你冻了一次"。
        return (f"账号「{name}」（账号 id={row.get('id')}）"
                f"现在就是{'冻结' if want else '正常'}状态")
    if tool in ("account_mute", "account_unmute"):
        # 禁言 / 解禁（20261002）：判据与冻结那一支同形（**现状取自名录**，读不出/
        # 名字不在 ⇒ 判不了 ⇒ 照弹卡），但读的是**现算的 `muted`**——Rust
        # `is_muted` 到期即为假，所以"禁言期已过"这一格自然回到"照弹"（那正是要办的事）。
        # 解禁方向那句状态词**不带「已」**（同冻结族那条硬要求：「现在没有被禁言」，
        # 不写「已解除」——后者在气泡里读起来像"系统刚替你解了一次"）。
        # ⚠️ **只有解禁方向做幂等短路**（"现在就是没被禁言 ⇒ 这一下什么也不会发生"，
        # 与 Rust 的 no-op 判据同源=库里没值）。**禁言方向刻意不短路**：对已在禁言期的
        # 账号再禁一次是**有意义的操作**（改时长 / 转永久，Rust 头注明说 no-op 属发起方
        # 但禁言方向不适用）——短路掉，主人说「改成永久禁言」会被回一句"现在就是禁言中"
        # 而那一列一个字节都没动。
        users = s.get("users")
        if not isinstance(users, dict) or not users:
            return None
        name = str(a.get("name") or "").strip()
        row = _account_row(users, name)
        if row is None:
            return None                      # 名录里没这个名字 ⇒ 工具的拒绝路，不是已达成
        muted = _row_muted(row)
        if muted is None:
            return None                      # 读不出（不许当成"正常"）
        if tool == "account_mute":
            return None
        if muted:
            return None                      # 解禁才叫已达成，其余照弹
        return (f"账号「{name}」（账号 id={row.get('id')}）"
                f"现在没有被禁言")
    if tool == "set_account_role":
        # 变更身份（20261002）：判据与上面冻结那一支同形——**现状取自名录**，
        # 读不出/名字不在/身份认不出/本来就是目标身份以外，一律判"没达成"照弹卡。
        # 目标身份认不出（模型填了个不存在的身份）⇒ 判不了 ⇒ 照弹：那会走工具的
        # 零写出口（后端"站内没有这个身份"原话），不是"已达成"。
        users = s.get("users")
        if not isinstance(users, dict) or not users:
            return None
        name = str(a.get("name") or "").strip()
        row = _account_row(users, name)
        if row is None:
            return None
        want = normalize_role(a.get("role"))
        if want is None:
            return None
        if str(row.get("role") or "").strip() != want:
            return None
        return (f"账号「{name}」（账号 id={row.get('id')}）"
                f"现在的身份本来就是{role_cn(want)}")
    if tool in ("approve_quota_request", "reject_quota_request"):
        # 批准与驳回（20261001 修，同一条判据）：这两件**消耗的都是"一条待处理的申请"**
        # （服务端按 `WHERE status=0` 认领那一行），与**计数器的当前值无关**。
        # 此前批准与主动重置共用"计数器本来就是 0"那条判据，于是「一个满额账号交了
        # 一份申请」被判成已达成 ⇒ 不弹卡、零调用，而那条申请**仍挂在待处理态**；
        # 主人再说一次，判据还是同一句 ⇒ **永远没有通道能办它**（trace
        # `20261001T105116` 实证：回执明说"这一轮没有做任何改动"，后台那一行还挂着
        # 待处理；用户报「请求要么拒绝要么通过，不作为会一直挂起」）。
        # 判据的正解是**那一行还在不在**：一条待处理的申请只有批或驳才会消失。
        # 所以——快照读不到 ⇒ 判不了 ⇒ 照弹；他在申请里 ⇒ 正是要办的那一次 ⇒ 照弹；
        # 不在 ⇒ 才如实说"没有待处理的额度申请"（fail-open 的方向永远是弹卡）。
        users = s.get("users")
        reqs = s.get("quota_requests")
        if not isinstance(users, dict) or not users or not isinstance(reqs, dict):
            return None
        row, name = _quota_target_row(tool, a, users)
        if row is None:
            return None                      # 名录里没这一行 ⇒ 工具的拒绝路，不是已达成
        try:
            uid = int(row.get("id"))
        except (TypeError, ValueError):
            return None
        if uid in reqs:
            return None                      # 有申请 ⇒ 照弹
        if not name:
            name = f"账号 id={uid}"
        return f"账号「{name}」现在没有待处理的额度申请"
    if tool == "reset_user_quota":
        # 主动重置（20260929）：这个动作**不需要他申请过**（站内没有"列全部账号"的
        # 读工具，它是唯一能在"没人申请"时动额度的一条），效果是直接把计数器清零
        # ⇒ "已达成"的判据是**计数器本来就是 0**（没有可清零的东西）。
        # 与上面那两支分开：批/驳消耗的是申请行，这一支消耗的是"用掉的轮数"。
        users = s.get("users")
        if not isinstance(users, dict) or not users:
            return None                      # 名录读不到 ⇒ 判不了（同冻结族）
        row, name = _quota_target_row(tool, a, users)
        if row is None:
            return None                      # 名录里没这一行 ⇒ 工具的拒绝路，不是已达成
        if not name:
            name = f"账号 id={row.get('id')}"
        used = quota_used(row)
        lim = quota_limit(row)
        if used is None or lim is None:
            return None                      # 用量/上限读不出 ⇒ 判不了（不许当成 0）
        if used != 0:
            return None                      # 还有额度没用完 ⇒ 这一下真会改变东西，照弹
        if lim <= 0:
            # 0 = 不限额（管理员档）。这一支**不是**"额度满"，是"本来就没有上限"——
            # 说成「额度本来就是满的」会被读成"他刚好没用过"，而真相是他不受限。
            return f"账号「{name}」是不限额的管理员，本来就没有额度需要重置"
        # 读数写**余额**（20260929b 与前端四处、系统上下文行统一口径）：「剩 500/500 轮」
        # 与「0/500 轮」说的是同一件事，但前者一眼就是"满的"，后者要先想一下 0 是哪一栏。
        return (f"账号「{name}」（账号 id={row.get('id')}）的额度本来就是满的"
                f"（剩 {lim}/{lim} 轮，没有用掉任何一轮）")
    if tool == "complete_dashboard_todo":
        text = str(a.get("text") or "").strip()
        row, _n, have, _total = _todo_face_row(s.get("todos"), text)
        # 同名多条时 row 是 None（`_todo_face_row` 只认唯一命中）：那是"分不清是哪一条"，
        # 与"已经完成了"是两件事，照弹。
        if not have or row is None or row.get("done") is not True:
            return None
        return f"待办「{text}」本来就是完成状态（{render_todo_when(row)}）"
    if tool == "reschedule_dashboard_todo":
        # 改排期（20260929 批 G）：「已达成」= 那一行的排期**本来就是**这一次要写进去的
        # 那一天（含"本来就没排期、这次正是要清掉"）。比的是两个 ISO 串，与工具侧那次
        # `changed` 同源——**只在"不会真改变什么"时**才免弹卡，判不出来（快照读不到、
        # 同名多条、认不出的值）一律照弹。
        text = str(a.get("text") or "").strip()
        row, _n, have, _total = _todo_face_row(s.get("todos"), text)
        if not have or row is None:
            return None
        want = str(a.get("date") or "").strip()
        if want in _TODO_CLEAR_DATES:
            want = ""
        if str(row.get("date") or "").strip() != want:
            return None
        # 与 `render_todo_rescheduled(changed=False)` 同一句（同一件事实、两处印出来，
        # 不许各写一套）。判据仍是 `row.get("done")` 的镜像——这里比的是日期。
        when = todo_due_phrase(a.get("date"))
        was = f"本来就排在{when}" if when != _TODO_CLEAR_WORD else "本来就没有排期"
        return f"待办「{text}」{was}"
    if tool == "set_article_status":
        aid = _as_article_id(a.get("article_id"))
        notes = s.get("notes")
        row = notes.get(aid) if isinstance(notes, dict) and aid is not None else None
        if not isinstance(row, dict):
            return None                      # 清单里没有这一篇 ⇒ 工具会拒绝，不是已达成
        has_status = a.get("status") is not None and str(a.get("status")).strip() != ""
        has_top = a.get("is_top") is not None and str(a.get("is_top")).strip() != ""
        if not has_status and not has_top:
            return None
        want_status = normalize_status(a.get("status")) if has_status else None
        want_top = normalize_top(a.get("is_top")) if has_top else None
        if (has_status and want_status is None) or (has_top and want_top is None):
            return None                      # 目标值认不出 ⇒ 工具拒绝，照弹
        # 与 tools.set_article_status 的 `same` **逐项同判**：只判点名的那些字段
        # （没点名的字段保持不动，所以它是什么不影响"这次改动有没有必要"）。
        if not ((want_status is None or normalize_status(row.get("status")) == want_status)
                and (want_top is None or normalize_top(row.get("isTop")) == want_top)):
            return None
        now_cn = "、".join(filter(None, [
            status_cn(row.get("status")) if has_status else "",
            top_cn(row.get("isTop")) if has_top else ""]))
        return f"文章 {aid}{_title_of(row)}本来就是{now_cn}"
    if tool == "set_article_tags":
        index = s.get("index")
        notes = s.get("notes")
        aid = _as_article_id(a.get("article_id"))
        row = notes.get(aid) if isinstance(notes, dict) and aid is not None else None
        if not isinstance(index, dict) or not isinstance(row, dict):
            return None                      # 字典/清单读不到 ⇒ 名字对不上 id，判不了
        if a.get("replace") is not None and (a.get("add") or a.get("remove")):
            return None                      # 工具自己会拒绝这个组合，轮不到判已达成
        cur = parse_tag_ids(row.get("noteTags"))

        def _ids(items) -> list[int] | None:
            """名字/数字混合列表 → id 列表；**认不出任何一个就整条判不了**（None）。"""
            out: list[int] = []
            for it in items:
                i = _as_article_id(it)
                if i is None:
                    hit, _cands = find_tag(index, str(it))
                    if hit is None:
                        return None
                    i = hit.id
                out.append(i)
            return out

        if a.get("replace") is not None:
            new = _ids(_name_list(a.get("replace")))
        else:
            add_ids = _ids(_name_list(a.get("add")))
            rm_ids = _ids(_name_list(a.get("remove")))
            if add_ids is None or rm_ids is None or (not add_ids and not rm_ids):
                return None
            new = [i for i in cur if i not in rm_ids]
            for i in add_ids:
                if i not in new:
                    new.append(i)
        if new is None or new != cur:
            return None
        title = _title_of(row)
        # 标题与「的标签」之间那个空格只在**没有标题**时才补（`_confirm_one` 的同一条）：
        # 「《架构漫谈》 的标签」读起来像两个并列短语。
        return (f"文章 {aid}{title}"
                f"{'的标签' if title else ' 的标签'}本来就是{render_tag_list(cur, index)}")
    if tool == "audit_board_comment":
        # **只判审核**：删除没有"已经是这个状态"这回事（删一条已删的没有同值可比）。
        v = normalize_verdict(a.get("verdict"))
        if v is None:
            return None
        # 目标走 **talkId**（与 `_confirm_one` 同一条通道）；`_board_row_by_id` 判不了
        # （清单没读到 / id 认不出 / 清单里没这一条）时返回 None：与工具
        # `_find_board_comment_by_id` 同一判据，照弹。
        hit = _board_row_by_id(s.get("boards"), a.get("talk_id"))
        if hit is None or hit.get("approved") != BOARD_VERDICT_APPROVED[v]:
            return None
        who = str(hit.get("author") or hit.get("nickname") or "").strip()
        return (f"留言 #{hit.get('talkKey')}{f'（{who} 的）' if who else ''}"
                f"现在就是{BOARD_VERDICT_CN[v]}状态")
    if tool == "audit_comment":
        # 与 `audit_board_comment` 那一支**逐条同形**（只判审核、不判删除；快照判不了
        # 就照弹）。快照是**评论名册**（`comments`）不是留言清单——拿错一份的后果是
        # "评论 #37" 撞上"留言 #37"，一次本来该弹的卡被静默摘掉。
        v = normalize_verdict(a.get("verdict"))
        if v is None:
            return None
        hit = _comment_row_by_id(s.get("comments"), a.get("comment_id"))
        if hit is None or hit.get("approved") != BOARD_VERDICT_APPROVED[v]:
            return None
        who = str(hit.get("nickname") or hit.get("username") or "").strip()
        return (f"评论 #{hit.get('id')}{f'（{who} 的）' if who else ''}"
                f"现在就是{BOARD_VERDICT_CN[v]}状态")
    if tool == "create_tag":
        index = s.get("index")
        if not isinstance(index, dict):
            return None
        name = str(a.get("title") or "").strip()
        if not name:
            return None
        color_spec = str(a.get("color") or "").strip()
        hexval = match_tag_color(color_spec) if color_spec else None
        if color_spec and hexval is None:
            return None                      # 色认不出 ⇒ 工具拒绝，照弹
        pname = str(a.get("parent_tag") or "").strip()
        pid = None
        if pname:
            parent, _cands = find_tag(index, pname, level=1)
            if parent is None:
                return None                  # 父标签不存在（或同名多条）⇒ 另说，照弹
            pid = parent.id
        hit, _cands = find_tag(index, name, pid)
        # 与 tools.create_tag 的复用判据逐字同形：同名**同层**才算"已经在站里"。
        if hit is None or not (pid is not None or hit.level == 1):
            return None
        # 点名了颜色就必须也是那个色：同名标签是别的颜色时，主人要的那个标签其实
        # **还没有**（工具那条路会复用并如实说出色差）——这是"已达成"之外的一件事。
        if hexval is not None and (hit.color or "").strip().lower() != hexval.lower():
            return None
        return f"标签「{hit.label}」本来就在站里（id={hit.id}，{level_cn(hit)}标签）"
    if tool == "update_announcement":
        rows = s.get("announcements")
        if not isinstance(rows, dict):
            return None
        title = str(a.get("title") or "").strip()
        if not title:
            return None
        hits = [r for r in rows.values()
                if isinstance(r, dict) and str(r.get("title") or "").strip() == title]
        if len(hits) != 1:
            return None                      # 同名多条/没有这条 ⇒ 工具的拒绝路，照弹
        hit = hits[0]
        nt = str(a.get("new_title") or "").strip()
        nc = str(a.get("content") or "").strip()
        if not nt and not nc:
            return None
        cur_t = str(hit.get("title") or "").strip()
        cur_c = str(hit.get("content") or "")
        # 与 tools.update_announcement 的 noop 判据同形（未点名的字段原样带上）。
        if (nt or cur_t) != cur_t or (nc or cur_c.strip()) != cur_c.strip():
            return None
        return f"公告「{cur_t}」本来就是现在这个标题与正文"
    return None


def _title_of(row) -> str:
    """文章行 → `《标题》`（没有标题就什么都不写，不补占位）。"""
    t = str((row or {}).get("noteTitle") or "").strip()
    return f"《{t}》" if t else ""


def render_noop_text(already, done=None) -> str:
    """「状态已经是这个值」那一轮的**气泡正文**（确定性中文，不经 narrator）。

    三条硬要求：① 印出**现状值**（主人要能核对）；② 明说**没有做任何改动**、
    连写请求都没发出去；③ **不许出现动作完成式**（「已完成 / 已收藏 / 已标记 /
    已冻结」会被读成"系统替我做过了一次"）——说状态，不说动作。

    **第四条（20261015）**：② 是对**整轮**说的，而这一格只看得到它自己那一批。
    `done` 非空 = 这一轮**真的发出过写请求**（调用方按账甲 `ok_write_rows` 给，判据仍
    只有 `_has_real_change` 一处）⇒ 把回执原文印出来，并把 ② 收窄成"**没有为上面这几件**
    再发一次写请求"。现场 `20261015T092916`（会话 347）：同一轮先按令牌把站内通知发了
    出去，planner 随后把**两轮前就办完**的 `set_article_status(54)` 当成本轮任务 ⇒
    掏空走这一格 ⇒ 气泡说"这一轮没有做任何改动——连写请求都没有发出去"，而主人刚点的
    那件事真的办成了、回执也在系统里。③ 在这一支里**不适用**：这里印的是真发生过的那
    几件，而它们用的是回执原文（系统自己写的那句，≤200 字），这里一个字都不重写——
    藏起来正是要修的那个病，另写一遍措辞则是同一件事两处各说一次。`done` 为空时返回的
    仍是原来那一串（逐字节不变）。
    """
    parts = "；".join(str(x.get("why") or "").strip() for x in (already or []))
    head = f"这几件现在就已经是你要的样子了：{parts}。"
    rows = [str(x).strip() for x in (done or []) if str(x).strip()]
    if rows:
        return (head + "\n\n补一句对账——这一轮**确实发出过写请求**，回执是这几条：\n"
                + "\n".join(f"- {r}" for r in rows[:3])
                + "\n\n上面那几件**一件都没有动过**（没有为它们再发一次写请求）："
                  "你要的状态本来就在。要改的话直接说要改成什么样就行。")
    return (head + "\n\n所以这一轮**没有做任何改动**——连写请求都没有发出去，"
            "你要的状态本来就在。要改的话直接说要改成什么样就行。")


def render_already_note(already) -> str:
    """混合轮（既有该弹卡的、也有已达成的）挂在确认正文**末尾**的那句补充。

    必须挂在末尾而不是混进正文：主人点「确定」时要知道**这一批到底包含哪几件**
    （已达成的那几件不在里面），否则他会以为点一下两件都办了。
    """
    parts = "；".join(str(x.get("why") or "").strip() for x in (already or []))
    if not parts:
        return ""
    return (f"\n\n（另外，{parts}——这几件**没有列进这一批**，"
            f"点「确定」只会办前面那几件。）")
