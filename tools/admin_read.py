# -*- coding: utf-8 -*-
"""管理助手报表工具（只读七件）：服务器状态 / 服务健康 / 审核状况 / 后台留言名册 /
用户报表 / 文章流量报表 / 分期报表（20261018 拆 `tools/base.py`）。

**为什么单独成模块**：`tools/base.py` 是全仓第二大体量文件，而这一族是**连续的 197 行、
7 个 `@tool` 函数**，且有一个干净的共同点——**scope 全是 `admin.console`**
（`agent/authz.py`），即"只有管理员用得动的那几张纸"。

**为什么它们是一族，而不是"都是读"这么粗**：它们读的东西分两条通道，
但**出口是同一个**——给人看的、渲染好的中文报表：
  · 本机读数（服务器状态 / 服务健康）：agent 与生产服务同机，直接读 /proc、systemctl、
    日志，**不经后台门**、不消耗发起人的身份；
  · 后台读（审核状况 / 留言名册 / 用户 / 流量 / 分期）：走「以发起人身份代调」——
    `_sign_local_jwt` 现签 60 秒 JWT 打本机 Rust，`auth_guard` 照旧按 `claims.sub` 查库判角色。
    **Rust 侧零改动**，agent 自己不留任何后台凭据。

**三道闸由粗到细**（只有本机读数那两件不受后两道管）：
  ① 结构性：不进 planner 的两个点名白名单（`workflows._EXPLICIT_TOOLS` /
     `_CALLABLE_QUERY_TOOLS`）⇒ planner 无法经 PARAMS.calls/tools 点到它们，
     只能由 ops_report / moderation_report / user_report 三个技能模板展开；
  ② 身份：会话角色不是 admin 时 planner 上下文里**根本看不到**这三个技能
     （`workflows.build_planner_context(role)`）；
  ③ 判据：execute 调用前的 `authz.check()`。`admin.console` **不吃 shadow 开关**
     （`authz._HARD_SCOPES`）——它是纯新增的能力，没有"观测既有流量"可谈，shadow 期也硬拦。

**为什么这几个工具返回"渲染好的中文报表"而不是 `_shape(data)`**（绕过既有惯例）：
见 `agent/reports.py` 的模块头注——LLM 数数是幻觉高发区，报表的价值全在数字可信。

## 搬家时守的两条
⚠️ **`_admin_get` 一律写成 `_base._admin_get(...)`，不许按名 import**：它是被套件直接
打桩的 11 个模块全局之一（实测 6 处）。函数对象取 globals 取的是**它自己所在模块**的
`__dict__` ⇒ 按名 import 会把打桩前的旧对象钉死在这里，桩静默失效、代码真去发网络请求，
而症状只是"断言换了个理由红"（本仓最贵的失效：看着在跑、其实没跑）。
`_base` 这个属性查找发生在**调用那一刻**，桩才生得效。
⚠️ **本模块一个名字都不按名从 `tools.base` 取**——连 `BOARD_STATUS_FILTERS`（定义在
base 第 ~1905 行、`agent/action_text.py` 也在用）也写成 `_base.BOARD_STATUS_FILTERS`。
两个理由，都不是风格偏好：
  · **时序**：本模块在 `tools/base.py` 的**原位置**就被 import（re-export 那一行），
    那时 base 还只定义到第 ~365 行，`from tools.base import BOARD_STATUS_FILTERS`
    会当场 ImportError（半初始化模块）；
  · **mypy**：模块级写一句 `from tools.base import …` 会让 mypy 在这个环上**判不出
    这七个名字的类型** ⇒ base 的 `_TOOL_REGISTRY` 当场多出 7 条 `Cannot determine type`
    （20261018 实测 `tools/base.py` 13→16 条，`eval/type_gate.py` 直接红）。
属性查找发生在**调用那一刻**，两个问题都不存在。
"""

import logging
import os
from typing import Annotated, Literal

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, empty, ok, unavailable

# `__all__` = 本模块**所有不以 `_` 开头**的模块级名字（社区惯例，不留例外；
# `tests/test_module_exports.py` 按这条规则机械判着，漂了就红）。
__all__ = ["logger", "get_server_status", "get_service_health", "get_moderation_status",
           "list_admin_board", "get_user_stats", "get_note_stats", "get_note_periods"]

logger = logging.getLogger(__name__)

@tool
def get_server_status() -> str:
    """查看服务器运行状态：CPU 核数与使用率、1/5/15 分钟负载、内存与 Swap 用量、
    各磁盘用量与可用空间、开机时长。数据由 agent 直接读本机 /proc 得到，不经后台接口。"""
    # 延迟导入：tools.base 在 `agent` 包的导入链**上游**（agent/__init__ → agent.agent
    # → agent.graph → tools），模块级 `from agent import …` 会构成循环导入。
    # 同先例见 rag/search.py 的 `from tools.base import _get`。
    from agent import hostinfo as H
    from agent import reports as R
    try:
        cpu = H.cpu_percent_over()
        mem = H.mem_summary(H.parse_meminfo(H.read_meminfo()))
        if cpu is None and not mem:
            # 两样都读不到 = 这根本不是一台正常机器，如实说"读不到"而不是给一张空报表
            return unavailable("读取服务器状态失败：本机 /proc 不可读")
        return ok(R.render_server_status(
            cpu_pct=cpu,
            cores=os.cpu_count() or 0,
            load=H.parse_loadavg(H.read_loadavg()),
            mem=mem,
            disks=H.disk_rows(),
            uptime_s=H.read_uptime(),
        ))
    except Exception as exc:
        logger.exception("get_server_status failed")
        return unavailable(f"读取服务器状态失败: {exc}")


@tool
def get_service_health() -> str:
    """查看服务健康：本机 systemd 服务（Rust 后端、Python agent，装了物联网平台时
    还有 device-service）的状态、重启次数与启动时刻，心跳探针近 24 小时的 WARN/FAIL，
    今日对话的轮数、异常收尾与质检拦截，以及存活日志的体积。读的是本机的 systemctl
    与 logs/ 目录。"""
    from agent import hostinfo as H
    from agent import reports as R
    try:
        services = [(unit, H.service_show(unit)) for unit in H.SERVICES]
        health = H.parse_health_log(H.read_text_tail(H.HEALTH_LOG),
                                    H.health_window(24))
        return ok(R.render_service_health(
            services=services,
            health=health,
            traces=H.trace_stats(),
            sizes=H.log_sizes(),
        ))
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
        return ok(R.render_moderation_status(data, status=status))
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
    # 认不出的筛选值由**函数签名的闭集**挡在门外（`Literal[...]` → langchain 在调进
    # 本函数之前就 ArgValidationError，execute 把它包成 `__ERROR__` 帧，原因码
    # `args_parse`）。这正是批 5 给 `get_moderation_status.status` 补的那一课：以前
    # 是散文里的取值，臆造值落回**混合视图**而叙述照样说成"这些是被驳回的"——答错且
    # 无声（见 docs/toolcall-stability-roadmap.md 批 5）。所以此处**不写**手写兜底：
    # 再写一层只是死代码，真判据是那句注解（`test_reports` ⑬ 钉着它不许放宽成 str）。
    #   另注：隔壁那张报表的取值是 ai_passed|ai_rejected|pending，与本工具
    # pending|passed|rejected **不是同一套**——注解与菜单行都写明了两套不能混用。
    approved = _base.BOARD_STATUS_FILTERS.get(str(status)) if status is not None else None
    try:
        return ok(R.render_board_roster(rows, approved=approved, keyword=keyword),
                  meta={"count": len(rows)})
    except Exception as exc:
        logger.exception("render_board_roster failed")
        return unavailable(f"整理后台留言名册失败: {exc}")


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
        return empty("后台没有返回用户统计数据")
    try:
        return ok(R.render_user_stats(data))
    except Exception as exc:
        logger.exception("render_user_stats failed")
        return unavailable(f"整理用户报表失败: {exc}")


@tool
def get_note_stats(config: RunnableConfig) -> str:
    """查看文章**流量**报表（全站口径）：阅读量/点赞量/收藏量的合计、三张排行榜
    （阅读榜、点赞榜、收藏榜，各取前 10，**报表里逐条印着「第 N 名」**）、
    以及最近 30 天的日趋势。

    用来回答"哪几篇最火 / 阅读量最高的是哪篇、排第几 / 这篇排第几 / 最近有人看吗"。
    只算**当前可见**的文章（草稿与私密不计入，连带它们的历史阅读量）。
    这是**全站排行**：问**某一篇**有多少阅读/点赞/收藏，用 get_article_detail
    （它逐篇带这三个数），别用本工具去猜单篇。
    需要管理员身份。"""
    from agent import reports as R
    data = _base._admin_get("/api/protected/stats/notes", config)
    if isinstance(data, ToolResult):
        return data
    if not data:
        return empty("后台没有返回文章流量数据")
    try:
        return ok(R.render_note_stats(data))
    except Exception as exc:
        logger.exception("render_note_stats failed")
        return unavailable(f"整理文章流量报表失败: {exc}")


@tool
def get_note_periods(
    kind: Annotated[Literal["week", "month", "year"],
                    "报表粒度：week=按周 / month=按月 / year=按年。"
                    "**必填**——主人问「上周/这个月/今年」就按他说的那个粒度给，"
                    "别用一个近似的档糊过去"],
    config: RunnableConfig,
) -> str:
    """查看文章的**分期**报表（周报 / 月报 / 年报）：逐期给出阅读/点赞/收藏的合计，
    以及**这一期阅读量最高的几篇**（每篇带三个数）。

    与 `get_note_stats` 是两张纸，别拿一张当另一张：
    · `get_note_stats` = **当下快照**（全站合计、三张总榜、近 30 天日趋势）；
    · 本工具 = **按期切开**（"上周怎么样""这个月和上个月比""去年哪几篇最热"）。
    口径与它一致：只算**当前可见**的文章。
    期数由后端定（一周一期给一年、一月一期给一年、一年一期给五年），**不要**自己
    指定期数；报表会写明某一期是不是**不完整**（统计功能上线那一期只统计了部分天数），
    照它说不完整，别把"部分"读成"掉了"；报表说一行记录都还没有时，那是"还没开始
    统计"，不是"这些期没人看"。
    需要管理员身份。"""
    from agent import reports as R
    data = _base._admin_get(f"/api/protected/stats/notes/periods?kind={kind}", config)
    if isinstance(data, ToolResult):
        return data
    if not data:
        return empty("后台没有返回分期报表数据")
    try:
        return ok(R.render_note_periods(data))
    except Exception as exc:
        logger.exception("render_note_periods failed")
        return unavailable(f"整理分期报表失败: {exc}")
