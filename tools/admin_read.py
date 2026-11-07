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
__all__ = ["logger", "get_server_status", "get_service_health", "get_user_stats",
           "get_note_stats", "get_note_periods"]

logger = logging.getLogger(__name__)

@tool
def get_server_status() -> str | ToolResult:
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
def get_service_health() -> str | ToolResult:
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
