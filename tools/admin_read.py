# -*- coding: utf-8 -*-
"""Read-only administrator reports.

Host metrics read local system state. Site reports call the Blog API with the
requesting user's identity; the backend checks current administrator privileges.
Tool definitions are the single source of model-visible descriptions and schemas.
"""

import logging
import os
from typing import Annotated, Literal

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, empty, ok, unavailable

# Public exports; helper names stay private to their modules.
__all__ = ["logger", "get_server_status", "get_service_health", "get_moderation_status",
           "list_admin_board", "list_admin_comments", "get_user_stats", "get_note_stats",
           "get_note_periods"]

logger = logging.getLogger(__name__)

@tool
def get_server_status() -> str:
    """查看服务器运行状态：CPU 核数与使用率、1/5/15 分钟负载、内存与 Swap 用量、
    各磁盘用量与可用空间、开机时长。数据由 agent 直接读本机 /proc 得到，不经后台接口。"""
    # Import report dependencies lazily to avoid the tool registry's import cycle.
    from agent import hostinfo as H
    from agent import reports as R
    try:
        cpu = H.cpu_percent_over()
        mem = H.mem_summary(H.parse_meminfo(H.read_meminfo()))
        if cpu is None and not mem:
            # 两样都读不到 = 这根本不是一台正常机器，如实说"读不到"而不是给一张空报表
            return unavailable("读取服务器状态失败：本机 /proc 不可读")
        # Preserve structured measurements alongside their readable projection.
        kw: R.ServerStatusKw = {"cpu_pct": cpu, "cores": os.cpu_count() or 0,
              "load": H.parse_loadavg(H.read_loadavg()), "mem": mem,
              "disks": H.disk_rows(), "uptime_s": H.read_uptime()}
        return ok(R.render_server_status(**kw), data=kw)
    except Exception as exc:
        logger.exception("get_server_status failed")
        return unavailable(f"读取服务器状态失败: {exc}")


@tool
def get_service_health() -> str:
    """读取当前服务状态、重启次数、历史探针记录、对话计数和日志体积，仅管理员。
    当前状态不能证明历史一直正常；这些读数不证明故障原因。
    """
    from agent import hostinfo as H
    from agent import reports as R
    try:
        services = [(unit, H.service_show(unit)) for unit in H.SERVICES]
        health = H.parse_health_log(H.read_text_tail(H.HEALTH_LOG),
                                    H.health_window(24))
        kw: R.ServiceHealthKw = {"services": services, "health": health,
              "traces": H.trace_stats(), "sizes": H.log_sizes()}
        return ok(R.render_service_health(**kw), data=kw)
    except Exception as exc:
        logger.exception("get_service_health failed")
        return unavailable(f"读取服务健康失败: {exc}")


@tool
def get_moderation_status(
    config: RunnableConfig,
    status: Annotated[Literal["ai_passed", "ai_rejected", "pending"] | None,
                      "只列某一类明细：ai_passed=AI 直接通过的 / ai_rejected=AI 驳回的 / "
                      "pending=需要人工复批的；不填则三类各列最近几条（其余只计数）"] = None,
) -> str:
    """查看河灯留言的审核状况：总数与待审/已通过/已驳回的分布、AI 侧判定分布
    （AI 通过 / AI 驳回 / AI 存疑转人工 / 未走 AI），以及三份可读名单——
    ① AI 直接通过的 ② AI 驳回的（并说明人工是维持驳回、改判放行还是仍在等）
    ③ 需要人工复批的（并说明是 AI 存疑还是 AI 通过后才等人看）。
    追问"把被驳回的/等人复批的都列出来"时用 status 参数聚焦某一类（列得更多）。
    明细里的「账号（…）」是**真实发表账号**（放灯必须登录，匿名留言一样有账号），
    「留名」只是留言人自己填的自由文本——要认人看账号。
    ⚠ 这是**审核状况报表**（按状态切三份名单，默认每份只印 5 条）；要"逐条看每条
    是谁发的/最近都说了什么"，用后台名册 `list_admin_board`（不分切、行数更多）。
    需要管理员身份（读的是后台留言管理视图）。"""
    from agent import reports as R
    data = _base._admin_get("/api/protect/board", config)
    if isinstance(data, ToolResult):
        return data
    if not data:
        return empty("河灯留言板目前还没有任何留言，没有可审核的内容")
    try:
        return ok(R.render_moderation_status(data, status=status),
                  data={"rows": data, "status": status})
    except Exception as exc:
        logger.exception("render_moderation_status failed")
        return unavailable(f"整理审核状况失败: {exc}")


@tool
def list_admin_board(
    config: RunnableConfig,
    status: Annotated[Literal["pending", "passed", "rejected"] | None,
                      "只看某一类：pending=待审 / passed=已通过 / rejected=未通过（已驳回）；"
                      "不填就是全部（注意与审核状况报表 get_moderation_status 的 "
                      "ai_passed|ai_rejected|pending 不是同一套取值，别混用）"] = None,
    keyword: Annotated[str | None,
                       "按关键词过滤（在正文、留名、账号昵称/用户名里做子串匹配）；"
                       "只在留言很多、要定位某一条时填"] = None,
) -> str:
    """查看河灯留言的**后台名册**：逐条给出 talkId、审核状态、时间、**发表账号**
    （userId/昵称/用户名）、留言时自己填的留名、正文节选。**待审与未通过的留言也在
    这里**（公开留言板列表只含已通过的）。

    ⚠ **要回答「这条留言是谁发的」必须用它**：公开留言板视图里那个"作者"是留言人
    自己在留名框里填的**自由文本**（可以填任何字、也可以留空——留空就是匿名），
    它**不是账号**，拿正文或留名去认人会认错。放灯必须登录，所以后台名册里每条
    留言都有真实账号，**匿名留言一样溯得到是谁发的**。
    可选 status 只看某一类审核状态，可选 keyword 按关键词收窄。需要管理员身份。"""
    from agent import reports as R
    data = _base._admin_get("/api/protect/board", config)
    if isinstance(data, ToolResult):
        return data
    rows = data if isinstance(data, list) else []
    if not rows:
        return empty("河灯留言板目前还没有任何留言（后台名册也是空的）")
    # Literal validation rejects unknown states; each report owns its status vocabulary.
    approved = _base.BOARD_STATUS_FILTERS.get(str(status)) if status is not None else None
    try:
        return ok(R.render_board_roster(rows, approved=approved, keyword=keyword),
                  meta={"count": len(rows)})
    except Exception as exc:
        logger.exception("render_board_roster failed")
        return unavailable(f"整理后台留言名册失败: {exc}")


@tool
def list_admin_comments(
    config: RunnableConfig,
    status: Annotated[Literal["pending", "passed", "rejected"] | None,
                      "只看某一类：pending=待审 / passed=已通过 / rejected=未通过（已驳回）；"
                      "不填就是全部"] = None,
    keyword: Annotated[str | None,
                       "按关键词过滤（在评论正文、作者昵称/用户名、文章标题里做子串匹配）；"
                       "只在评论很多、要定位某一条时填"] = None,
) -> str:
    """查看**文章评论**的**后台名册**：逐条给出 commentId、审核状态、时间、发表账号
    （userId/昵称/用户名）、**挂在哪篇文章下**（文章标题与 noteId）、正文节选。
    待审与未通过的评论也在这里（公开的评论区只显示已通过的）。

    ⚠ **「评论」与「留言」是两套东西，别混用**：评论长在每篇文章正文下面（后台「评论
    管理」页，`?tab=comment`）；留言是「河灯集」板块的河灯（后台「留言管理」页，
    `?tab=review`，工具是 `list_admin_board`）。主人说「评论」就用本工具，
    说「留言/河灯」就用 `list_admin_board`——答错一边就是答非所问。
    可选 status 只看某一类审核状态，可选 keyword 按关键词收窄。需要管理员身份。"""
    from agent import adminops_comment as _cm
    data = _base._admin_get("/api/protect/comments", config)
    if isinstance(data, ToolResult):
        return data
    rows = data if isinstance(data, list) else []
    if not rows:
        return empty("站内还没有任何文章评论（后台名册也是空的）")
    # 认不出的筛选值由函数签名的闭集挡在门外（`Literal[...]` ⇒ langchain 在调进本函数
    # 之前就 ArgValidationError，execute 把它包成 `__ERROR__` 帧，原因码 args_parse）。
    # 同 `list_admin_board` 那条注：此处**不写**手写兜底，真判据是那句注解。
    approved = _base.BOARD_STATUS_FILTERS.get(str(status)) if status is not None else None
    try:
        return ok(_cm.render_comment_roster(rows, approved=approved, keyword=keyword),
                  meta={"count": len(rows)})
    except Exception as exc:
        logger.exception("render_comment_roster failed")
        return unavailable(f"整理后台评论名册失败: {exc}")


@tool
def get_user_stats(config: RunnableConfig) -> str:
    """查看全站用户数据报表：用户总数与角色分布、会话/消息/执行回执的总量、
    近 7 天与近 30 天的活跃人数、以及按消息数倒序的用户明细（最多 50 行）。
    需要管理员身份。口径是"活动"不是"注册"（系统没存注册时间）。"""
    from agent import reports as R
    data = _base._admin_get("/api/protected/stats/users", config)
    if isinstance(data, ToolResult):
        return data
    if not data:
        return unavailable("后台没有返回用户统计数据，本次无法核对报表。")
    counts = ("totalUsers", "totalConversations", "totalMessages", "totalExecutions",
              "activeUsers7d", "activeUsers30d")
    if not isinstance(data, dict) or any(type(data.get(key)) is not int or data[key] < 0
                                         for key in counts):
        return unavailable("用户统计缺少有效聚合值，本次无法核对报表。")
    try:
        return ok(R.render_user_stats(data), data=data)
    except Exception as exc:
        logger.exception("render_user_stats failed")
        return unavailable(f"整理用户报表失败: {exc}")


@tool
def get_note_stats(config: RunnableConfig) -> str:
    """查询当前可见文章的历史累计合计与阅读、点赞、收藏排行榜，另附近30天日趋势。
    合计和排行均为历史累计，不是本周、本月或本年；有明确自然周期时使用get_note_periods。
    日趋势是独立滚动窗口，不能据此把累计排行榜称为窗口内排名。
    单篇文章的累计指标使用get_article_detail。本工具仅管理员可用。
    """
    from agent import reports as R
    data = _base._admin_get("/api/protected/stats/notes", config)
    if isinstance(data, ToolResult):
        return data
    if not data:
        return empty("后台没有返回文章流量数据")
    try:
        return ok(R.render_note_stats(data), data=data)
    except Exception as exc:
        logger.exception("render_note_stats failed")
        return unavailable(f"整理文章流量报表失败: {exc}")


@tool
def get_note_periods(
    kind: Annotated[Literal["week", "month", "year"],
                    "自然周期：week=周，month=月，year=年；按用户要求选择。"],
    config: RunnableConfig,
) -> str:
    """按自然周、月或年查询文章访问合计和各期阅读排行，仅管理员。
    每期合计涵盖本期全量，排行仅列前几篇。只算当前可见文章，范围由后端返回。
    当前期或统计开始前的期可能不完整；未开始统计不等于零访问。
    本工具不提供每日明细；查看近30天日序列用get_note_stats。
    """
    from agent import reports as R
    data = _base._admin_get(f"/api/protected/stats/notes/periods?kind={kind}", config)
    if isinstance(data, ToolResult):
        return data
    if not data:
        return empty("后台没有返回分期报表数据")
    try:
        return ok(R.render_note_periods(data), data=data)
    except Exception as exc:
        logger.exception("render_note_periods failed")
        return unavailable(f"整理分期报表失败: {exc}")
