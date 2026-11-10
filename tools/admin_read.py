"""Read-only administrator reports.

Diagnostics and site reports call the Blog API with the requesting user's
identity; the backend checks current administrator privileges.
Tool definitions are the single source of model-visible descriptions and schemas.
"""

import logging
from typing import Annotated, Literal

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.diagnostics import get_server_status, get_service_health
from tools.result import ToolResult, empty, ok, unavailable

# Public exports; helper names stay private to their modules.
__all__ = ["logger", "get_server_status", "get_service_health", "get_user_stats",
           "get_note_stats", "get_note_periods"]

logger = logging.getLogger(__name__)

@tool
def get_user_stats(config: RunnableConfig) -> str | ToolResult:
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
def get_note_stats(config: RunnableConfig) -> str | ToolResult:
    """查询当前可见文章的历史累计合计与阅读、点赞、收藏排行榜，另附近30天日趋势。
    合计和排行均为历史累计，不是本周、本月或本年；有明确自然周期时使用get_note_periods。
    日趋势是独立滚动窗口，不能据此把累计排行榜称为窗口内排名。
    单篇文章的累计指标使用get_article_stats。本工具仅管理员可用。
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
) -> str | ToolResult:
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
