# -*- coding: utf-8 -*-
"""Site guide, navigation metadata and page commands."""

import json
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from config import settings as _settings
from tools.result import ToolResult, fact, ok, tgt

__all__ = ["get_site_map", "get_chat_history", "get_current_time",
           "IOT_ENABLED", "IOT_NAV_PATH", "nav_pure_path", "nav_path_valid",
           "navigate_to", "toggle_effect", "toggle_dark_mode", "nav_label", "public_navigation",
           "navigation_paths"]


@tool
def get_site_map(config: RunnableConfig) -> ToolResult:
    """读取站点功能指南及当前调用者的导航可用性，不切换页面。

    指南不是完整路由清单；未列出不证明页面不存在。navigationAvailable仅为本次权限快照，
    不授予权限；具体跳转仍由navigate_page校验，文章与分类路径从真实来源取得。
    """
    features = [
        ("/", "首页", "置顶与最新公开文章、个人简介"),
        ("/times", "归档", "按时间归档公开文章"),
        ("/category/<name>", "分类", "按分类查看公开文章"),
        ("/talk", "说说", "动态与碎语"),
        ("/guestbook", "留言板", "公开浏览河灯集；发布须登录，可匿名隐藏公开留名，不需邮箱"),
        ("/about", "关于我", "个人介绍"),
        ("/article/<id>", "文章详情", "公开文章正文与Mermaid图表"),
        ("/login", "登录", "账号登录"),
        ("/dashboard", "后台管理", "仅管理员管理文章、分类、标签、公告等"),
    ]
    if IOT_ENABLED:
        features.append((IOT_NAV_PATH, "物联网平台", "须登录，管理本人设备"))
    paths = navigation_paths(config)
    data = {"resource": "site_guide", "coverage": "documented_features", "exhaustive": False,
            "grantsAuthorization": False, "pages": [
                {"path": path, "name": name, "description": description,
                 "navigationAvailable": (path in paths or bool(paths)
                                          and path in ("/article/<id>", "/category/<name>"))}
                for path, name, description in features]}
    return ok(json.dumps(data, ensure_ascii=False, separators=(",", ":")), data=data)


@tool
def get_chat_history(
    config: RunnableConfig,
    limit: Annotated[int, "Number of recent messages, from 1 to 10"] = 5,
) -> object:
    """Read actual text history within the current verified conversation."""
    from agent.domains.own_activity import get_chat_history as read_history
    return read_history.invoke({"limit": limit}, config=config)


@tool
def get_current_time() -> str:
    """Get current date and time."""
    from utils.clock import format_local_now
    return format_local_now()

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


def navigation_paths(config: RunnableConfig) -> set[str]:
    """Project the shared actor/scope checks; command execution rechecks authority."""
    from agent.authz import SCOPE_ADMIN_CONSOLE, SCOPE_WRITE_PAGE, check
    from agent.runtime.access import configured_reads_enabled

    cfg = (config or {}).get("configurable") or {}
    if (type(cfg.get("user_id")) is not int
            or not configured_reads_enabled(config, SCOPE_WRITE_PAGE)
            or not check(cfg.get("principal"), "navigate_to").allowed):
        return set()
    admin = configured_reads_enabled(config, SCOPE_ADMIN_CONSOLE)
    return {path for path in _NAV_EXACT_PATHS if not path.startswith("/dashboard") or admin}


def public_navigation() -> dict[str, str]:
    """Static public links for manual help; these do not grant tool permissions."""
    return {label: path for path, label in _NAV_LABELS.items()
            if not path.startswith("/dashboard")}


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


# Stable deep links for individual content records.
_ITEM_POSITION_FORMS: tuple[tuple[str, str, str], ...] = (
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
) -> str | ToolResult:
    """Send a validated navigation command in the execution receipt."""
    p = path.strip()
    if not nav_path_valid(p):
        return (f"导航路径无效: {p!r}。可用页面: {', '.join(sorted(_NAV_EXACT_PATHS))}，"
                "以及 /article/<正整数编号>、/category/<分类名>；请从站点资料或真实链接核对。")
    from tools.base import SITE_BASE

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
) -> str | ToolResult:
    """生成博客特效的显式开关指令（樱花、大雨或雪花）；参数错误时不生成指令。"""
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
) -> str | ToolResult:
    """开启或关闭博客页面的夜间模式（暗色主题）。
    系统按本工具的执行回执驱动前端切换；状态会持久化记忆。"""
    if mode in ("on", "off"):
        return ok(f"夜间模式已{'打开' if mode == 'on' else '关闭'}",
                  meta=fact("toggle_dark_mode", changed=True, target=tgt("page", None, "夜间模式"),
                            before="原状态", after="开" if mode == "on" else "关",
                            cmd={"kind": "darkmode", "mode": mode}))
    return "模式参数无效，应为 on 或 off"
