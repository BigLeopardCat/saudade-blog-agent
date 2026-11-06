# -*- coding: utf-8 -*-
"""站内公告与河灯留言人工复核的卡面与回执。"""

from __future__ import annotations

from agent.adminops_tags import (
    BOARD_VERDICT_CN,
    clip,
)


def announcement_title(row) -> str:
    return str((row or {}).get("title") or "").strip() or "（无标题）"


def render_announcement_created(row) -> str:
    """发布成功。**不带工具名**（同 render_tag_created 的血案）。"""
    return (f"已发布公告「{announcement_title(row)}」（id={(row or {}).get('id')}）。"
            f"首页现在就能看到它。")


def render_announcement_updated(before, after) -> str:
    """修改成功。**不复述正文**：只报标题怎么变的，正文变更只说"已更新"——
    正文可能有几百字，回执行只留列宽（300），塞进去会把摘要挤掉。"""
    b, a = announcement_title(before), announcement_title(after)
    what = f"标题「{b}」→「{a}」" if b != a else "正文已更新"
    return f"已修改公告「{a}」（{what}）（后台已复核读到新值）"


def render_announcement_noop(row) -> str:
    """要改的内容与现状一致 ⇒ 什么都不用做。**也走 ok**：这不是失败，
    "现状就是这样"是事实本身（narrator 照它答即可）。"""
    return f"公告「{announcement_title(row)}」现在就是这个样子，无需改动"


def render_announcement_deleted(row) -> str:
    """删除成功。公告是真删（没有外键、没有回收站）——回执行必须说清"取不回来"，
    否则用户以为还能撤销。"""
    return (f"已删除公告「{announcement_title(row)}」（删除后取不回来）"
            f"（后台已复核：公告列表里已经没有它）")


def _board_ref(row) -> str:
    """回执行里的留言指称：`talkId:<id> 「正文片段」（作者）`。"""
    from agent.reports import sanitize_untrusted
    body = clip(sanitize_untrusted((row or {}).get("content") or "", 30), 30)
    who = clip(sanitize_untrusted((row or {}).get("author") or "", 16), 16)
    tail = f"（{who} 的留言）" if who else ""
    return f"talkId:{row.get('talkKey')} 「{body}」{tail}"


def render_board_audited(row, verdict: str) -> str:
    """人工复核成功。**说清这是可改判的**（不是删除）——否则主人会以为留言没了。"""
    cn = BOARD_VERDICT_CN.get(verdict, verdict)
    tail = ("留言现在对所有访客可见" if verdict == "pass"
            else "留言已隐藏，作者在「我的河灯」里看到的是未通过")
    return f"已把留言 {_board_ref(row)} 人工复核为**{cn}**——{tail}（后台已复核读到新状态）"


def render_board_audit_noop(row, verdict: str) -> str:
    """现状与目标一致 ⇒ 什么都不用做。**也走 ok**："现在就是这样"是事实本身。"""
    cn = BOARD_VERDICT_CN.get(verdict, verdict)
    return f"留言 {_board_ref(row)} 现在就是「{cn}」状态，无需改动"


def render_board_deleted(row) -> str:
    """删除成功。留言是真删（没有回收站）——回执行必须说清"取不回来"。"""
    return (f"已删除留言 {_board_ref(row)}（删除后取不回来）"
            f"（后台已复核：留言列表里已经没有它）")
