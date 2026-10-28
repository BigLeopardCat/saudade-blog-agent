# -*- coding: utf-8 -*-
"""Site information, page commands and legacy tool entry points."""

from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from config import settings as _settings
from tools.base import SITE_BASE
from tools.result import fact, ok, tgt

# Public exports; helper names stay private to their modules.
__all__ = ["get_site_map", "get_chat_history", "get_current_time",
           "get_weather", "IOT_ENABLED", "IOT_NAV_PATH", "nav_pure_path", "nav_path_valid",
           "navigate_to", "toggle_effect", "toggle_dark_mode", "nav_label"]

# ---------------------------------------------------------------------------
# 导航 / 引导工具
# ---------------------------------------------------------------------------

@tool
def get_site_map() -> str:
    """返回博客功能结构图，用于引导用户了解博客有哪些功能及其位置。"""
    # IoT 那行随开关走：没装平台却把它列进"功能结构图"，就是让 回复模型 照着一份
    # 系统自己给的假清单介绍一个 404（同 NAV_MAP 那十个别名，见文件顶部 IOT_ENABLED）。
    iot_line = (
        f"\n- 物联网控制台 ({IOT_NAV_PATH}) — 管理访客自己的 IoT 设备"
        "（ESP32 OLED 屏幕显示等），需登录"
    ) if IOT_ENABLED else ""
    return f"""
博客功能结构：
- 首页 (/) — 展示置顶文章、最新文章列表、个人简介
- 归档 (/times) — 按时间轴归档展示所有文章
- 分类 (/category/:name) — 按分类查看文章
- 说说 (/talk) — 动态/碎语
- 留言板 (/guestbook) — 「河灯集」留言簿：页面下方输入框（提示语「此刻想说的话…」）
  写内容即可放灯；留名框默认预填昵称、可匿名，无需注册邮箱
- 关于我 (/about) — 个人介绍
- 文章详情 (/article/:id) — 查看文章全文，支持 Mermaid 图表
- 后台管理 (/dashboard) — 登录后可管理文章、分类、标签、公告等{iot_line}
"""

# Current conversation history is backed by the verified native read adapter.

@tool
def get_chat_history(
    config: RunnableConfig,
    limit: Annotated[int, "Number of recent messages, from 1 to 10"] = 5,
) -> object:
    """Read actual text history within the current verified conversation."""
    from agent.domains.own_activity import get_chat_history as read_history
    return read_history.invoke({"limit": limit}, config=config)

# ---------------------------------------------------------------------------
# 时间 / 天气工具
# ---------------------------------------------------------------------------

@tool
def get_current_time() -> str:
    """Get current date and time."""
    from utils.clock import format_local_now
    return format_local_now()

@tool
def get_weather(
    location: Annotated[str, "City, optionally country or province"],
    location_id: Annotated[int | None, "Selected geocoding candidate"] = None,
) -> object:
    """Read current weather-model data through the shared native weather adapter."""
    from agent.domains.utilities import get_weather as read_weather

    return read_weather.invoke({"location": location, "location_id": location_id})

# ---------------------------------------------------------------------------
# 导航工具
# ---------------------------------------------------------------------------

# Exact routes and dynamic route shapes are shared by native and compatibility tools.
IOT_ENABLED: bool = _settings.iot_enabled
IOT_NAV_PATH = "/device-console/"

_NAV_EXACT_PATHS = {
    "/", "/about", "/guestbook", "/talk", "/times", "/login", "/dashboard",
    "/dashboard/notes", "/dashboard/comments", "/dashboard/albums",
    "/dashboard/announcement", "/dashboard/users", "/dashboard/analytics",
    "/dashboard/usercontrol",
    *({IOT_NAV_PATH} if IOT_ENABLED else set()),
}
_NAV_PREFIX_PATHS = ("/category/", "/article/")

# Full-page navigation must wait until the reply has finished streaming.
_NAV_WHOLE_PAGE_PATHS = {IOT_NAV_PATH} if IOT_ENABLED else set()

_NAV_LABELS = {
    "/": "首页",
    "/about": "关于我",
    "/guestbook": "留言板",
    "/talk": "说说",
    "/times": "时间轴",
    "/login": "登录",
    "/dashboard": "后台",
    "/dashboard/notes": "笔记",
    "/dashboard/comments": "后台说说",
    "/dashboard/albums": "图库",
    "/dashboard/announcement": "公告",
    "/dashboard/users": "用户管理",
    "/dashboard/analytics": "数据板",
    "/dashboard/usercontrol": "站点设置",
}
if IOT_ENABLED:
    _NAV_LABELS[IOT_NAV_PATH] = "物联网平台"


def nav_label(path: str) -> str:
    """Return a stable human label for a validated route, or the route itself."""
    return _NAV_LABELS.get(str(path or ""), str(path or ""))


def nav_pure_path(p: str) -> str:
    """Extract the path without query or fragment."""
    return str(p or "").split("?", 1)[0].split("#", 1)[0]


def nav_path_valid(p: str) -> bool:
    """Accept local routes with one dynamic segment; reject URL normalization tricks."""
    if (not isinstance(p, str) or len(p) > 512 or not p.startswith("/") or p.startswith("//")
            or any(ord(char) < 33 or char == "\\" for char in p)):
        return False
    try:
        parsed = urlsplit(p)
        pure = unquote(parsed.path, errors="strict")
    except ValueError:
        return False
    if (parsed.scheme or parsed.netloc
            or any(ord(char) < 32 or char == "\\" for char in pure)):
        return False
    if parsed.path in _NAV_EXACT_PATHS:
        return True
    parts = pure.split("/")
    if len(parts) != 3 or parts[0] or not parts[2] or parts[2] in (".", ".."):
        return False
    if parts[1] == "article":
        return parts[2].isascii() and parts[2].isdigit() and int(parts[2]) > 0
    return parts[1] == "category"


# Supported stable deep links for individual content records.
# Navigation validation and visible guidance share these address forms.
_ITEM_POSITION_FORMS: tuple[tuple[str, str, str], ...] = (
    # (形态名, 完整形态（教给模型的字面）, 取值从哪里来)
    ("评论", "/article/<文章 id>?cid=<评论 id>",
     "`cid` = 那条评论的 id（站内通知那一行的 `link` 里带着它）"),
    ("河灯留言", "/guestbook?lid=<留言 id>",
     "`lid` = 那条留言的 `talkId`（审核结果通知里就是它；含**待审/未通过**的灯——"
     "公开池里没有那几条，先在「我的河灯」里找）"),
    ("说说", "/talk?tk=<说说 id>",
     "`tk` = 那条说说的 `talkId`（说说列表帧里的那一列）"),
)


@tool
def navigate_to(
    path: Annotated[str, "Page path to navigate to, e.g. / /times /category/tech "
                         "/article/3 /talk /guestbook /about /dashboard/notes"],
    confirm: Annotated[bool, "Whether user confirmation is needed. "
                              "false=direct nav, true=ask user"] = True,
) -> str:
    """Send a validated navigation command in the execution receipt."""
    p = path.strip()
    if not nav_path_valid(p):
        return (f"导航路径无效: {p!r}。可用页面: {', '.join(sorted(_NAV_EXACT_PATHS))}，"
                "以及 /article/<正整数编号>、/category/<分类名>；请从站点资料或真实链接核对。")
    full_url = f"{SITE_BASE.rstrip('/')}{p}"
    mode = "confirm" if confirm else "direct"
    if confirm:
        return ok(f"导航已发起，等主人确认后才会跳转：{full_url}",
                  meta=fact("navigate", changed=True, target=tgt("page", None, p),
                            before="当前页", after="页面已跳转",
                            cmd={"kind": "navigate", "url": full_url, "mode": mode}))
    if nav_pure_path(p) in _NAV_WHOLE_PAGE_PATHS:
        return ok(f"页面即将跳转：{full_url}（本条回复说完再跳）",
                  meta=fact("navigate", changed=True, target=tgt("page", None, p),
                            before="当前页", after="页面即将跳转",
                            cmd={"kind": "navigate", "url": full_url, "mode": mode}))
    return ok(f"页面已跳转：{full_url}",
              meta=fact("navigate", changed=True, target=tgt("page", None, p),
                        before="当前页", after="页面已跳转",
                        cmd={"kind": "navigate", "url": full_url, "mode": mode}))

@tool
def toggle_effect(
    effect: Annotated[Literal["sakura", "rain", "snow"],
                      "Effect name: sakura(樱花), rain(大雨), snow(雪花)"],
    action: Annotated[Literal["on", "off"], "开启还是关闭: on(开启), off(关闭)"] = "on",
) -> str:
    """开启或关闭博客页面的视觉效果（樱花/大雨/雪花）。
    系统按本工具的执行回执驱动前端开关（按 action 显式开合，不因重复命令翻转状态）。
    参数校验：无效 effect/action 返回提示而非执行回执——**回执只代表真实发生的切换**，
    没有回执 = 动作未发生，回复不得声称已开/已关（gate 声称检查依据）。"""
    if effect not in ("sakura", "rain", "snow"):
        return f"效果无效: {effect!r}。可选: sakura(樱花), rain(大雨), snow(雪花)"
    if action not in ("on", "off"):
        return (
            f"action 无效: {action!r}。可选: on(开启), off(关闭)。"
            f"查询效果当前状态请以对话上下文中的 current_effects 字段为准，无需调用工具。"
        )
    _cn = {"sakura": "樱花", "rain": "大雨", "snow": "雪花"}[effect]
    return ok(f"特效 {_cn}({effect}) 已{'打开' if action == 'on' else '关闭'}",
              meta=fact("toggle_effect", changed=True, target=tgt("page", None, effect),
                        before="原状态", after="开" if action == "on" else "关",
                        cmd={"kind": "effect", "effect": effect, "action": action}))


@tool
def toggle_dark_mode(
    mode: Annotated[Literal["on", "off"], "夜间模式开关: on(开启夜间模式), off(关闭夜间模式)"],
) -> str:
    """开启或关闭博客页面的夜间模式（暗色主题）。
    系统按本工具的执行回执驱动前端切换；状态会持久化记忆。"""
    if mode in ("on", "off"):
        return ok(f"夜间模式已{'打开' if mode == 'on' else '关闭'}",
                  meta=fact("toggle_dark_mode", changed=True, target=tgt("page", None, "夜间模式"),
                            before="原状态", after="开" if mode == "on" else "关",
                            cmd={"kind": "darkmode", "mode": mode}))
    return "模式参数无效，应为 on 或 off"
