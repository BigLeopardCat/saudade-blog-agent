# -*- coding: utf-8 -*-
"""管理助手写工具：**文章评论**的人工复核（审核 / 删除）—— 20261019 加。

## 为什么现在才有

留言（河灯）那一族早就有读 + 复核的通道，**评论一直没有**：主人说「把那篇文章下面
那条评论驳回」时，agent 手里没有任何工具能落地——它只能如实说"我没有直接读评论的
能力"（生产 trace 20261018T232550 就是这个形状）。后端其实一直有对应的三个端点
（`GET /api/protect/comments`、`PUT …/{id}/audit`、`DELETE …/{id}`），缺的只是这一侧。

## 与留言那一族的异同（`tools/board_moderation.py`）

**同**：目标通道、fail-closed（读回复核）、`0 = 驳回` 的请求体方向、**数字由工具内部
产生**（上层只说 pass/reject，绝不让模型碰 1/0）、命中多条一律不替主人挑。

**异**：
  ① 评论有 **id 稳定指称**（`note_comment.id`，后台名册逐条印 `commentId:<id>`），
     而且**已上线的后台页**（`CommentManage`）读的就是同一个端点 ⇒ 审核按 **id** 认，
     不按正文片段。留言那边按 id 认是因为"待办台账只摆待审的行、id 由系统现摆"；
     评论这边 id 来自名册，同样可验证（在名册里 / 不在）。
  ② 评论是**软删**（`is_deleted = 1`，行留着溯源），留言是**真删**。所以删除的回执行
     措辞不同（见 `adminops_comment.render_comment_deleted`），而且**已软删的行不可
     再审**（父仓 `audit_comment` 明确拒它）——这里也照那个口径先拒，不把请求发出去。
  ③ 评论挂在某篇文章下 ⇒ 指称与候选一律带上「文章《标题》」（主人脑子里的指代通常是
     「那篇讲 X 的文章下面那条」）。

## 搬家/新增时守的一条

⚠️ **帮手一律写成 `_base.<名>(...)`**：`_admin_get` / `_admin_request` 是被套件直接
打桩的模块全局（同 `tools/board_moderation.py` 头注那条）。按名 import 会把打桩前的
旧对象钉死在这里，桩静默失效、代码真去发网络请求，而症状只是"断言换了个理由红"。
"""

from typing import Annotated

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, fact, ok, tgt, unavailable

__all__ = ["audit_comment", "delete_comment"]


def _comment_index(config: RunnableConfig) -> dict[int, dict] | None:
    """读后台评论清单 → `{commentId: 行}`；读不到返回 None（≠"没有评论"）。

    读的是 `GET /api/protect/comments`（**不带任何筛选参数** ⇒ 全量，含已软删的行）。
    写操作的读写必须同一口径：拿筛选过的一份去核对目标，会让"这条其实还在、只是被筛掉
    了"读成"站内没有这条评论"。
    """
    data = _base._admin_get("/api/protect/comments", config)
    if isinstance(data, ToolResult):
        return None
    out: dict[int, dict] = {}
    for row in data or []:
        if not isinstance(row, dict):
            continue
        raw_id = row.get("id")
        if raw_id is None:
            continue
        try:
            cid = int(raw_id)
        except (TypeError, ValueError):
            continue
        out[cid] = row
    return out


def _comment_label(row: dict) -> str:
    """一条评论的短指称（拒绝文本里用）：`commentId:<id>（文章《标题》）`。"""
    from agent.adminops_tags import clip as _clip
    from agent.reports import sanitize_untrusted
    title = _clip(sanitize_untrusted(str((row or {}).get("noteTitle") or ""), 20), 20)
    return (f"commentId:{row.get('id')}（文章《{title or '已删除'}》）")


def _find_comment_by_id(comment_id, config: RunnableConfig, index=None):
    """按 **commentId** 定位一条评论 → `(行, None)` / `(None, 拒绝文本)`。

    三条失败各自如实说（都零写）：id 认不出（不是正整数）⇒ 说清那不是一个评论编号、
    **不退回去按正文猜**；清单读不到 ⇒ 单独一种说法（"读不到" ≠ "没有"）；清单里没有
    这个 id ⇒ 说清站内没有这条评论（可能已经被删了）。
    """
    from agent import adminops_target as _target
    if index is None:
        index = _comment_index(config)
    if index is None:
        return None, "读不到后台的评论列表，无法按编号核对那条评论，本次未改动"
    cid = _target.normalize_target_id(comment_id)
    if cid is None:
        return None, (f"「{str(comment_id or '').strip()[:40]}」不是一个评论编号，"
                      f"本次未改动——请把系统印出来的那条评论的 commentId 原样抄过来")
    hit = index.get(cid)
    if not isinstance(hit, dict):
        return None, (f"站内没有编号为 commentId:{cid} 的评论，本次未改动"
                      f"（可能记错了编号，或那条已经被删了）")
    return hit, None


@tool
def audit_comment(
    comment_id: Annotated[str | int,
                           "那条评论的 **commentId**（从系统给的清单里原样抄，"
                           "形如 37 或 commentId:37）"],
    verdict: Annotated[str, "复核结论：pass=通过（放行展示）/ reject=驳回（隐藏）"],
    config: RunnableConfig,
) -> str:
    """人工复核一条**文章评论**：**通过**（放行给所有人看）或**驳回**（隐藏起来）。
    评论按 **commentId** 指认：编号认不出、或站内没有这条评论时什么都不做，并如实说明原因。
    这一动作**不是删除**（驳回只是隐藏、评论本身留着），而且它是**改判**——待审的可以
    定通过或驳回，已经通过的可以再驳回（收回展示），已驳回的可以恢复通过；现状已经等于
    目标时如实说"与现在一致"、不发请求。**已被删除的评论不可再审**（会说清这一点）。
    需要管理员身份，且要经主人确认才会真正生效。

    ⚠️ 这是**文章评论**（长在文章正文下面），不是河灯留言——留言那条走
    `audit_board_comment`，两者是不同的表、不同的后台页，别混用。"""
    from agent import adminops_comment as _cm
    from agent import adminops_tags as _tags
    v = _tags.normalize_verdict(verdict)
    if v is None:
        return unavailable(f"认不出复核结论「{verdict}」（只能是 通过/pass 或 驳回/reject），"
                           f"未改动")
    hit, err = _find_comment_by_id(comment_id, config)
    if err:
        return unavailable(err)

    cid = int(hit.get("id"))
    if hit.get("isDeleted"):
        # 与父仓 `audit_comment` 同口径：已软删的行它直接拒（`is_deleted != 0`）。
        # 这里先拒是为了**不发一个注定失败的请求**，并把原因说成一句人话。
        return unavailable(f"评论 {_comment_label(hit)} 已经被删除了，"
                           f"删掉的评论不能再复核，本次未改动")

    want = _tags.BOARD_VERDICT_APPROVED[v]           # 1=通过 / 2=未通过
    if hit.get("approved") == want:
        return ok(_cm.render_comment_audit_noop(hit, v),
                  meta=fact("comment_audit", changed=False,
                            target=tgt("comment", cid, str(hit.get("nickname") or "")),
                            before=_cm.comment_state_cn(hit), after=_cm.comment_state_cn(hit),
                            change=f"与现在一致（{_cm.comment_state_cn(hit)}），无需改动",
                            comment_id=cid,
                            comment_author=str(hit.get("nickname") or "")))

    # 请求体按 Rust 侧口径发：1=通过 / 0=驳回（端点内部把 0 写成 approved=2）。
    data = _base._admin_request("PUT", f"/api/protect/comments/{cid}/audit",
                                {"approved": _tags.BOARD_VERDICT_BODY[v]}, config)
    if isinstance(data, ToolResult):
        return data

    after = _comment_index(config)
    if after is None:
        return unavailable("复核请求已发出，但读不回评论列表、无法确认是否真的改上了"
                           "——请到后台评论管理页核对")
    got = after.get(cid)
    if got is None:
        return unavailable(f"复核请求已发出，但读回评论列表里找不到 commentId:{cid}，"
                           f"本次改动未确认生效")
    if got.get("approved") != want:
        return unavailable(f"复核请求已发出，但读回 commentId:{cid} 的状态是"
                           f"「{_cm.comment_state_cn(got)}」、与预期的"
                           f"「{_tags.BOARD_VERDICT_CN[v]}」不一致，本次改动未确认生效")
    return ok(_cm.render_comment_audited(hit, v),
              meta=fact("comment_audit", changed=True,
                        target=tgt("comment", cid, str(hit.get("nickname") or "")),
                        before=_cm.comment_state_cn(hit), after=_tags.BOARD_VERDICT_CN[v],
                        evidence=_tags.BOARD_VERDICT_CN[v], comment_id=cid,
                        comment_author=str(hit.get("nickname") or ""),
                        change=f"{_cm.comment_state_cn(hit)} → {_tags.BOARD_VERDICT_CN[v]}"))


@tool
def delete_comment(
    comment_id: Annotated[str | int,
                           "那条评论的 **commentId**（从系统给的清单里原样抄）"],
    config: RunnableConfig,
) -> str:
    """删除一条**文章评论**（**删掉取不回来**，后台会标成「已删除」）。
    评论按 **commentId** 指认：编号认不出、或站内没有这条评论时什么都不做，并如实
    说明原因。**只是要隐藏一条评论时改用审核（驳回），不要删。**
    需要管理员身份，且要经主人确认才会真正删。

    ⚠️ 与留言那边**不同**：评论是**软删**（行还留着，后台标「已删除」、公开侧看不见），
    而河灯留言是真删。所以这里的复核读的是"这一条还在不在**待审队列**里"，
    不是"行有没有消失"。

    ⚠️ 这是**文章评论**，不是河灯留言——留言那条走 `delete_board_comment`。"""
    from agent import adminops_comment as _cm
    hit, err = _find_comment_by_id(comment_id, config)
    if err:
        return unavailable(err)

    cid = int(hit.get("id"))
    if hit.get("isDeleted"):
        return ok(_cm.render_comment_deleted(hit),
                  meta=fact("comment_delete", changed=False,
                            target=tgt("comment", cid, str(hit.get("nickname") or "")),
                            before="已删除", after="已删除",
                            change="它本来就是已删除状态，无需改动", comment_id=cid,
                            comment_author=str(hit.get("nickname") or "")))

    data = _base._admin_request("DELETE", f"/api/protect/comments/{cid}", None, config)
    if isinstance(data, ToolResult):
        return data

    after = _comment_index(config)
    if after is None:
        return unavailable("删除请求已发出，但读不回评论列表、无法确认是否真的删掉了"
                           "——请到后台评论管理页核对")
    got = after.get(cid)
    if got is None:
        # 软删的行**应该还在**（后台留着溯源）。读回时它整个消失了，说明读到的不是
        # 同一份数据（换了筛选口径 / 换了对端），此时**不能说"删掉了"**——那是猜。
        return unavailable(f"删除请求已发出，但读回评论列表里 commentId:{cid} 整条不见了"
                           f"（评论是软删，正常应该还在、只是标成已删除），"
                           f"本次改动未确认生效——请到后台评论管理页核对")
    if not got.get("isDeleted"):
        return unavailable(f"删除请求已发出，但读回 commentId:{cid} 仍是"
                           f"「{_cm.comment_state_cn(got)}」且未标记删除，本次改动未确认生效")
    return ok(_cm.render_comment_deleted(hit),
              meta=fact("comment_delete", changed=True,
                        target=tgt("comment", cid, str(hit.get("nickname") or "")),
                        before="已存在", after="已删除", change="已删除", comment_id=cid,
                        comment_author=str(hit.get("nickname") or "")))
