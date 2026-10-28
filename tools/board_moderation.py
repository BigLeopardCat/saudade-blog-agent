# -*- coding: utf-8 -*-
"""Guestbook moderation tools and target helpers.

Targets use stable IDs or unique body fragments; ambiguous matches require clarification.
The tool maps pass/reject to the backend numeric contract and verifies the
result after writing. Deletion must be checked because a missing ID may otherwise
produce a successful no-op. Shared transports are resolved through _base."""

import re
from typing import Annotated

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, fact, ok, tgt, unavailable

# Public exports; helper names stay private to their modules.
__all__ = ["BOARD_APPROVED_CN", "BOARD_STATUS_FILTERS",
           "audit_board_comment", "delete_board_comment"]

# ---------------------------------------------------------------------------
# 管理助手写工具：河灯留言的人工复核
# ---------------------------------------------------------------------------
# 与上一节的公告三件同一套纪律（名字通道 + fail-closed + 读回复核），三处**留言
# 独有**的事实决定了实现的形状：
#   ① 留言**没有标题、没有名字**（`talk` 表只有 content/cat/author/时间）：用户嘴里
#      说的就是**那句话本身** ⇒ 目标通道 = **正文片段**（唯一子串匹配）。这是名字
#      通道的留言版：目标仍是"用户说过的东西"，解析仍是确定性的，解不出仍然零写。
#      命中的那条**由工具认、不由模型认**——模型只负责把用户描述的那句原话抄进来。
#   ② 审核端点的请求体是 `{approved: i8}`，且 **0 = 驳回**（Rust 侧写 approved=2
#      "未通过"，与"待审 0"区分）——方向传反的后果是"该驳回的给放行了"。所以
#      **数字由工具内部产生**，上层只说 pass/reject，绝不让模型碰 1/0。
#   ③ 删除端点对不存在的 id **静默无操作**（`delete_by_id` 不报错，照样返回
#      "Deleted"）⇒ 同公告取向：**读不回就等于没删掉**，不猜。
#
# 与"名字通道"的唯一差别在**歧义的概率**：标签名撞车很罕见，而正文片段
# （"好"/"谢谢"）一撞就是好几条。所以这里的取向更严——**命中多条一律不替主人挑**
# （把候选连同作者/时间/原文列出来让他指认），绝不按"最新的那条"猜。
BOARD_APPROVED_CN = {0: "待审", 1: "已通过", 2: "未通过"}

# 后台名册（`list_admin_board`）的 status 实参 → approved 值。**只有三态、没有
# "全部"**：不填就是不筛（多一个 `all` 就多一个会填错的值，而空着本来就是同一个意思）。
BOARD_STATUS_FILTERS = {"pending": 0, "passed": 1, "rejected": 2}


def _board_index(config: RunnableConfig) -> dict[int, dict] | None:
    """读后台留言清单 → `{talkKey: 行}`；读不到返回 None（≠"没有留言"，同 _tag_index）。

    读的是 `GET /api/protect/board`（后台管理视图，含 approved 与 ai_result 两列，
    也是 `get_moderation_status` 的同一份数据源）——**写操作的读写必须同一口径**，
    否则"读的是台账、写的是另一行"这类错会一直藏到线上。
    """
    data = _base._admin_get("/api/protect/board", config)
    if isinstance(data, ToolResult):
        return None
    out: dict[int, dict] = {}
    for row in data or []:
        if not isinstance(row, dict):
            continue
        try:
            tid = int(row.get("talkKey"))
        except (TypeError, ValueError):
            continue
        row["talkKey"] = tid               # 就地归一成 int：下游 hit["talkKey"] 直接可用
        out[tid] = row
    return out


def _board_label(row: dict) -> str:
    """Render a target label with its talk ID, signature, state and time.

    Signatures are untrusted free text, not account identities. Body excerpts
    are added separately by callers according to their display budget."""
    from agent.adminops_tags import clip as _clip
    from agent.reports import sanitize_untrusted
    who = sanitize_untrusted(row.get("author") or row.get("nickname") or "", 16)
    if not who:
        who = f"userId:{row.get('userId')}"
    at = str(row.get("createTime") or "")[:16]
    return f"talkId:{row.get('talkKey')} {_clip(who, 16)}（{at}）"


def _board_excerpt(row: dict) -> str:
    """留言正文的一小段（问句预览用；访客可控 ⇒ 同样消毒）。"""
    from agent.adminops_tags import clip as _clip
    from agent.reports import sanitize_untrusted
    return _clip(sanitize_untrusted(row.get("content") or "", 40), 40)


def _find_board_comment(quote, config: RunnableConfig, index=None):
    """Find one guestbook entry by an exact body substring, then whitespace-normalized substring.

    Missing data and no matching entry remain distinct failures. Multiple matches
    require clarification; never select the newest match implicitly. index may
    provide a consistent snapshot for repeated reads within one operation."""
    if index is None:
        index = _base._board_index(config)
    if index is None:
        return None, "读不到后台的留言列表，无法把这段话对应到某条留言，本次未改动"
    want = str(quote or "").strip()
    if not want:
        return None, "没有给出能指认那一条留言的原话片段，本次未改动"
    hits = [r for r in index.values() if want in str(r.get("content") or "")]
    if not hits:
        squashed = re.sub(r"\s+", "", want)
        if squashed:
            hits = [r for r in index.values()
                    if squashed in re.sub(r"\s+", "", str(r.get("content") or ""))]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        cands = "；".join(f"{_board_label(r)}「{_board_excerpt(r)}」" for r in hits[:5])
        more = f"（还有 {len(hits) - 5} 条未列出）" if len(hits) > 5 else ""
        return None, (f"站内有 {len(hits)} 条留言都含「{want}」：{cands}{more}。"
                      f"无法确定是哪一条，本次未改动——请给一段更完整的原话"
                      f"（或说明作者/大致时间），我再动手")
    return None, (f"站内没有含「{want}」的河灯留言，本次未改动"
                  f"（可能记错了字，或那条已经被删了）")


# ⚠️ 三个 id 参数的注记写成 `str | int` 是**故意的、别收紧成 `str`**：模型照抄的是
# 一串数字，而计划模板展开时那段文字要过一遍 JSON 解析（`"$user_id"` → `126`），
# 纯数字串**必然**变 `int` 回来。声明 `str` 的话 pydantic 会在工具入口直接拒掉——
# 一条本来正确的写操作在结构上到不了执行。判据本体是
# `adminops.normalize_target_id`，它本来就认 `int`／纯数字串／带命名空间三种形态。
def _find_board_comment_by_id(talk_id, config: RunnableConfig, index=None):
    """Find a guestbook entry by a positive talk ID in the authorized snapshot.

    Invalid IDs, unavailable snapshots and absent entries have distinct failures.
    Never fall back from an invalid ID to guessing by body text."""
    from agent import adminops_target as _target
    if index is None:
        index = _base._board_index(config)
    if index is None:
        return None, "读不到后台的留言列表，无法按编号核对那条留言，本次未改动"
    tid = _target.normalize_target_id(talk_id)
    if tid is None:
        return None, (f"「{str(talk_id or '').strip()[:40]}」不是一个留言编号，"
                      f"本次未改动——请把系统印出来的那条留言的 talkId 原样抄过来")
    hit = index.get(tid)
    if not isinstance(hit, dict):
        return None, (f"站内没有编号为 talkId:{tid} 的留言，本次未改动"
                      f"（可能记错了编号，或那条已经被删了）")
    return hit, None


def _board_state_cn(row: dict) -> str:
    return BOARD_APPROVED_CN.get(row.get("approved"), "状态未知")


@tool
def audit_board_comment(
    talk_id: Annotated[str | int,
                        "那条留言的 **talkId**（从系统给的清单里原样抄，形如 101 或 talkId:101）"],
    verdict: Annotated[str, "复核结论：pass=通过（放行展示）/ reject=驳回（隐藏）"],
    config: RunnableConfig,
) -> str:
    """人工复核一条河灯留言：**通过**（放行给所有人看）或**驳回**（隐藏起来）。
    留言按 **talkId** 指认：编号认不出、或站内没有这条留言时什么都不做，并如实说明原因。
    这一动作**不是删除**（驳回只是隐藏、留言本身留着），而且它是**改判**——
    待审的可以定通过或驳回，**已经通过的可以再驳回**（收回展示），已驳回的可以恢复
    通过；现状已经等于目标时如实说"与现在一致"、不发请求。需要管理员身份，
    且要经主人确认才会真正生效。"""
    from agent import adminops_board as _board
    from agent import adminops_tags as _tags
    v = _tags.normalize_verdict(verdict)
    if v is None:
        return unavailable(f"认不出复核结论「{verdict}」（只能是 通过/pass 或 驳回/reject），"
                           f"未改动")
    hit, err = _find_board_comment_by_id(talk_id, config)
    if err:
        return unavailable(err)

    tid = int(hit.get("talkKey"))
    want = _tags.BOARD_VERDICT_APPROVED[v]           # 1=通过 / 2=未通过（见节头注 ②）
    if hit.get("approved") == want:
        # 现状即目标：**也走 ok**（这不是失败，"现在就是这样"是事实本身）。
        return ok(_board.render_board_audit_noop(hit, v),
                  meta=fact("board_audit", changed=False,
                            target=tgt("board_comment", tid, str(hit.get("author") or "")),
                            before=_board_state_cn(hit), after=_board_state_cn(hit),
                            change=f"与现在一致（{_board_state_cn(hit)}），无需改动",
                            board_id=tid, board_author=str(hit.get("author") or "")))

    # 请求体按 Rust 侧口径发：1=通过 / 0=驳回（端点内部把 0 写成 approved=2）。
    data = _base._admin_request("PUT", f"/api/protect/board/{tid}/audit",
                          {"approved": _tags.BOARD_VERDICT_BODY[v]}, config)
    if isinstance(data, ToolResult):
        return data

    after = _base._board_index(config)
    if after is None:
        return unavailable("复核请求已发出，但读不回留言列表、无法确认是否真的改上了"
                           "——请到后台留言管理页核对")
    got = after.get(tid)
    if got is None:
        return unavailable(f"复核请求已发出，但读回留言列表里找不到 #{tid} 这条留言，"
                           f"本次改动未确认生效")
    if got.get("approved") != want:
        return unavailable(f"复核请求已发出，但读回 #{tid} 的状态是"
                           f"「{_board_state_cn(got)}」、与预期的「{_tags.BOARD_VERDICT_CN[v]}」"
                           f"不一致，本次改动未确认生效")
    return ok(_board.render_board_audited(hit, v),
              meta=fact("board_audit", changed=True,
                        target=tgt("board_comment", tid, str(hit.get("author") or "")),
                        before=_board_state_cn(hit), after=_tags.BOARD_VERDICT_CN[v],
                        evidence=_tags.BOARD_VERDICT_CN[v],
                        board_id=tid, board_author=str(hit.get("author") or ""),
                        change=f"{_board_state_cn(hit)} → {_tags.BOARD_VERDICT_CN[v]}"))


@tool
def delete_board_comment(
    quote: Annotated[str, "用来指认是哪一条留言的**原话片段**（从那条留言正文里原样抄一段）"],
    config: RunnableConfig,
) -> str:
    """删除一条河灯留言（**删掉取不回来**，也没有回收站）。
    留言按正文片段指认：片段对不上、或站内有好几条都含这段时什么都不做，并如实
    说明原因与候选。**只是要隐藏一条留言时改用审核（驳回），不要删。**
    需要管理员身份，且要经主人确认才会真正删。

    **待审与被驳回的留言删得掉**（`_board_index` 读的是后台管理清单
    `GET /api/protect/board`，它不过滤 approved）——20260928 实证的反面说法
    （"系统没有删除被驳回留言的通道"）是编的，见 `reports.render_moderation_status`
    末尾那条"处置"注记。

    ⚠️ **为什么删除不跟审核一起改成按 `talkId`**（20260929 批 H · S2 的选择，
    **20261001 补注：理由变了，结论没变，待主人拍板**）：当初的理由是"已通过/已驳回
    留言的 talkId 全站只有一处读得到（审核状况报表的明细行），而那份报表每类只列前
    `MAX_LIST_DETAIL` 条（5 条；点名某一类才放宽到 20）"⇒ 只认 id 就可能连编号都拿
    不到。**后台留言名册（`list_admin_board`）落地后，这句不再成立**——名册逐条给
    talkId、还能按关键词收窄。留下来的理由只剩两条，都还成立：① 主人指认删除时手里
    拿的本来就是那句话本身，让他抄一小段比让他报编号更接近他真实说的话；② 这条链
    （按原话指认 + 卡面印原文）已经端到端验过，而改 id 要连带动弹确认卡面与探针。
    所以**维持现状**，但"只有一处读得到 id"这条论据已作废——别再用它论证什么。
    审核那条（`audit_board_comment`）治的则是"台账里等着办的那一行"，而**待办台账
    只摆待审**（approved=0）的行，id 由系统现摆，所以它按 id 认。
    保留正文片段通道的另一半理由：删除是一条"取不回来"的动作，让他抄
    一句原话（而不是报一个他自己也认不出的编号）在这条路上反而更稳。"""
    from agent import adminops_board as _board
    hit, err = _find_board_comment(quote, config)
    if err:
        return unavailable(err)

    tid = int(hit.get("talkKey"))
    data = _base._admin_request("DELETE", f"/api/protect/board/{tid}", None, config)
    if isinstance(data, ToolResult):
        return data

    after = _base._board_index(config)
    if after is None:
        return unavailable("删除请求已发出，但读不回留言列表、无法确认是否真的删掉了"
                           "——请到后台留言管理页核对")
    if tid in after:
        return unavailable(f"删除请求已发出，但读回留言列表里 {_board_label(hit)} 还在，"
                           f"本次改动未确认生效")
    return ok(_board.render_board_deleted(hit),
              meta=fact("board_delete", changed=True,
                        target=tgt("board_comment", tid, str(hit.get("author") or "")),
                        before="已存在", after="已删除", change="已删除",
                        board_id=tid, board_author=str(hit.get("author") or "")))
