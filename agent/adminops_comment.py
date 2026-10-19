# -*- coding: utf-8 -*-
"""文章评论人工复核的卡面与回执（20261019 加）。

**与河灯留言那一族（`agent/adminops_board.py`）刻意逐条同形**，因为两张表的审核语义
逐字相同（`approved`：0=待审 / 1=通过 / 2=未通过），后端也是同一套
（`comments.rs::audit_comment` 与 `talks.rs::audit_board` 共用 `decide_review` 与同一份
理由回退链）。差别只有**指称**：

  · 留言**没有标题、没有归属**——认它就是认那句话本身（`talkId` + 正文片段）；
  · 评论**长在某篇文章下面**——认它除了正文，还要能说出「在哪篇文章下」，
    因为主人脑子里的指代通常是「那篇讲 X 的文章下面那条」。

所以评论的指称一律带上文章：`commentId:<id> 「正文片段」（作者 · 文章《标题》）`。
`commentId` 带命名空间是硬要求（同 `_board_label` 那条注）：帧里同时存在文章 id、
留言 talkId、账号 id、通知 id，裸数字没有任何东西说明它是哪一种。

⚠️ **「评论」与「留言」是两套东西，措辞不许混**（这正是本模块存在的理由之一）：
评论在文章正文下面、走 `note_comment` 表、后台 `?tab=comment`；留言在河灯集板块、
走 `talk` 表、后台 `?tab=review`。主人问「评论」时答留言、或反过来，都是答非所问。
"""

from __future__ import annotations

from agent.adminops_tags import (
    BOARD_VERDICT_CN,
    clip,
)
# 状态词表（0=待审 / 1=通过 / 2=未通过）**只此一份**，住在 `tools/board_moderation.py`
# ——它与 `audit_comment` 端点真正写进去的那个值同源（Rust `comments.rs::CommentAdminDto`
# 的列注）。`agent/action_text.py` / `agent/adminops_confirm.py` 也读它。
# 曾经写成 `from agent.adminops_tags import BOARD_APPROVED_CN`：那张表**不在那里**
# （`adminops_tags` 只有 verdict 那一族），导入当场炸——记在这里是因为"看起来该在那儿"
# 正是下一个人会再犯一次的理由。
from tools.board_moderation import BOARD_APPROVED_CN

# 评论的 approved 三态与留言**逐字同义**（0=待审 / 1=通过 / 2=未通过，见父仓
# `comments.rs::CommentAdminDto.approved` 的列注）。**故意不另抄一张表**：两份一定会漂，
# 而漂掉的那一份决定的是"后台印「待审」还是印「未通过」"——同一件东西两种说法。
COMMENT_APPROVED_CN = BOARD_APPROVED_CN


def _comment_row_by_id(comments, comment_id):
    """评论名册快照里按 **commentId** 取那一行（卡面渲染用）；取不到 → None。

    与 `adminops_account._board_row_by_id` 同形同判据（`normalize_target_id` 归一后
    在快照里查），只是查的是评论名册、键是 `note_comment.id`。**只用于渲染**：
    None 只表示"问句里写不出具体是哪一条"（快照读不到 / id 认不出 / 名册里没这条），
    "能不能动"仍由规划轮的目标预检与工具侧的复核各判一次。
    """
    from agent.adminops_target import normalize_target_id
    if not isinstance(comments, dict) or not comments:
        return None
    cid = normalize_target_id(comment_id)
    if cid is None:
        return None
    row = comments.get(cid)
    return row if isinstance(row, dict) else None


def comment_ref(row) -> str:
    """一条评论的指称：`commentId:<id> 「正文片段」（作者 · 文章《标题》）`。

    `commentId` 带命名空间（理由见模块头注）。正文片段与作者都经消毒——两样都是
    访客可控文本（正文是评论人写的，昵称是他自己起的）。
    """
    from agent.reports import sanitize_untrusted
    row = row or {}
    body = clip(sanitize_untrusted(row.get("content") or "", 30), 30)
    who = clip(sanitize_untrusted(row.get("nickname") or row.get("username") or "", 16), 16)
    title = clip(sanitize_untrusted(row.get("noteTitle") or "", 20), 20)
    tail = f"（{who} · 文章《{title}》）" if who or title else ""
    return f"commentId:{row.get('id')} 「{body}」{tail}"


def comment_state_cn(row) -> str:
    """这一行现在是三态里的哪一个（认不出就是"状态未知"，不猜）。"""
    code = (row or {}).get("approved")
    if code is None:
        return "状态未知"
    return COMMENT_APPROVED_CN.get(code, "状态未知")


def render_comment_roster(rows: list[dict], *, approved: int | None = None,
                          keyword: str | None = None) -> str:
    """`GET /api/protect/comments` 的返回 → **逐条名册**（含发表账号，可筛选）。

    与 `reports.render_board_roster` 同一条纪律：**读不到 ≠ 没有**（`rows` 为空由调用方
    处理）、**打印实际生效的条件**（抬头逐字写出这次筛了什么，读的人才不会把"我筛过的
    那一类"说成"站里就这些"）、筛空时明说**筛掉了多少**。

    ⚠️ 与留言名册的一处**刻意不同**：这里**不补印"不带关键词的最近几条"**。留言那份补
    是因为"留言没有别的入口、筛空了就只能凭旧印象猜"；评论有 `noteId`，读的人可以顺着
    「哪篇文章」回去看上下文（`get_article_detail`），出路本来就在手里。
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    kw = (keyword or "").strip()
    hits = []
    for r in rows:
        if approved is not None and r.get("approved") != approved:
            continue
        if kw:
            hay = " ".join(str(r.get(k) or "") for k in
                           ("content", "nickname", "username", "noteTitle")).lower()
            if kw.lower() not in hay:
                continue
        hits.append(r)

    conds = []
    if approved is not None:
        conds.append(f"状态={COMMENT_APPROVED_CN.get(approved, approved)}")
    if kw:
        from agent.reports import sanitize_untrusted
        conds.append(f"关键词「{sanitize_untrusted(kw, 20)}」")
    cond_txt = f"；本次筛选：{'、'.join(conds)}" if conds else "；本次未筛选"

    head = [
        f"文章评论后台名册（{_ts()}，后台共 {len(rows)} 条{cond_txt}）",
        "- **评论与留言不是一回事**：这里是**文章评论**（长在每篇文章正文下面，"
        "后台「评论管理」页）；河灯留言在 `/guestbook`，是另一个后台页。"
        "主人问「评论」时别把留言答回去，反之亦然。",
        "- 每条都带 `commentId:<id>` 与**它挂在哪篇文章下**（`noteId` 在行内）；"
        "「（文章已删除）」表示那篇文章已经不在了，评论本身还在。",
    ]
    if not hits:
        head.append(f"- 按上面的条件一条都没匹配上（后台 {len(rows)} 条里筛出来的结果是空的）"
                    "——换个关键词、或不带条件看全部；**这不等于站内没有评论**。")
        return "\n".join(head)

    lines = []
    for r in hits[:ROWS_LIMIT]:
        title = clip(str(r.get("noteTitle") or ""), 20) or "（文章已删除）"
        who = clip(str(r.get("nickname") or r.get("username") or ""), 16) or "（账号未知）"
        at = str(r.get("createTime") or "")[:16]
        body = clip(str(r.get("content") or ""), 40)
        deleted = "（已删除）" if r.get("isDeleted") else ""
        lines.append(
            f"· commentId:{r.get('id')}［{comment_state_cn(r)}］"
            f"{deleted} {at}\n"
            f"　　{who}（账号 id={r.get('userId')}）· 文章《{title}》(noteId={r.get('noteId')})\n"
            f"　　「{body}」")
    if len(hits) > ROWS_LIMIT:
        lines.append(f"· …还有 {len(hits) - ROWS_LIMIT} 条未列出（要更多就加关键词收窄）")
    return "\n".join(head + lines)


# 名册一次最多印多少行。与留言名册同量级——名册是给模型看的，不是分页接口。
ROWS_LIMIT = 20


def _ts() -> str:
    from agent.reports import _ts as _rts
    return _rts()


def render_comment_audited(row, verdict: str) -> str:
    """人工复核成功。**说清这是可改判的**（不是删除）——否则主人会以为评论没了。"""
    cn = BOARD_VERDICT_CN.get(verdict, verdict)
    tail = ("评论现在对所有访客可见" if verdict == "pass"
            else "评论已隐藏，作者自己那边看到的是未通过")
    return f"已把评论 {comment_ref(row)} 人工复核为**{cn}**——{tail}（后台已复核读到新状态）"


def render_comment_audit_noop(row, verdict: str) -> str:
    """现状与目标一致 ⇒ 什么都不用做。**也走 ok**："现在就是这样"是事实本身。"""
    cn = BOARD_VERDICT_CN.get(verdict, verdict)
    return f"评论 {comment_ref(row)} 现在就是「{cn}」状态，无需改动"


def render_comment_deleted(row) -> str:
    """删除成功。评论是**软删**（行还在、后台标着「已删除」，公开侧看不见）——
    回执行说清"取不回来"，同时别把它说成"行没了"（那与后台实际看到的不一致）。"""
    return (f"已删除评论 {comment_ref(row)}（删除后取不回来，后台会标成「已删除」）"
            f"（后台已复核：这一条已不在待审队列里）")
