# -*- coding: utf-8 -*-
"""站内公告与河灯留言人工复核的卡面与回执。

批 3a 从 `agent/adminops.py` 按域拆出（原文逐字节保留）。"""

from __future__ import annotations

from agent.adminops_tags import (
    BOARD_VERDICT_CN,
    clip,
)


# ── 站内公告（20260922）────────────────────────────────────────────────
# 公告**没有 id 稳定指称**（用户从来只说标题），也没有层级/父级，所以这里的渲染
# 比标签/分类那批简单得多：一律按标题指认，篇数之类的"影响面"也不存在。
# 但有一条独有纪律：**公告正文是对全体访客说的话**，回执行里绝不摘要正文
# （只报"标题改成了什么"），免得把主人的原话改了样子念出去。
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


# ── 河灯留言的人工复核：回执行措辞（20260922 第六轮）────────────────
# 与写工具同一条纪律：**措辞里不带工具名**（带了会让 narrator 复述内部名字），
# 且必须把"这一动作可逆/不可逆"说清——审核可以改判（驳回的能再放行），删除不行。
def _board_ref(row) -> str:
    """回执行里的留言指称：`talkId:<id> 「正文片段」（作者）`。

    **id 带命名空间**（20260926 批 4，理由见 tools/base.py::_board_label）：这行会
    落进跨轮执行记忆，下轮主人说"把刚才那条删了"时，planner 读到的必须是一个
    知道该拿去哪儿的 id，而不是一个可以当文章 id 用的裸数字。

    正文片段是**这条留言在跨轮记忆里唯一的可认物**（下轮主人说"把刚才那条删了"
    要能对上号），作者名字同理；时间不进回执行（列宽 300，读侧只留最近 8 行）。
    两段都经消毒（访客可控文本）。
    """
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
