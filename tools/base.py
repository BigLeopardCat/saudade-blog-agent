"""Custom tool definitions for the LangChain agent.

Each tool is a @tool-decorated function with type hints, docstrings,
and error handling for production reliability.
"""

import logging
import httpx
from typing import Annotated
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

API_BASE = "https://saudade.site/api/public"

# httpx 客户端复用
_client = httpx.Client(timeout=15, verify=False)


def _get(path: str) -> dict | list:
    """Helper: call API and return data field."""
    try:
        resp = _client.get(f"{API_BASE}{path}")
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") == 200:
            return body["data"]
        logger.warning("API error: %s", body.get("message"))
        return []
    except Exception as exc:
        logger.error("API call failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# 笔记 / 文章 工具
# ---------------------------------------------------------------------------

@tool
def list_notes(
    page: Annotated[int, "Page number, default 1"] = 1,
    page_size: Annotated[int, "Items per page, default 10"] = 10,
) -> str:
    """获取文章列表，按页返回。返回文章标题、描述、分类、标签等信息。"""
    data = _get(f"/notes?page={page}&page_size={page_size}")
    return str(data)


@tool
def search_notes(keyword: Annotated[str, "搜索关键词"]) -> str:
    """搜索文章标题和内容，返回匹配的文章列表。"""
    try:
        resp = _client.post(
            f"{API_BASE}/notes/search",
            json=keyword,
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        return str(body.get("data", []))
    except Exception as exc:
        logger.error("Search failed: %s", exc)
        return str(exc)


@tool
def get_article_detail(
    article_id: Annotated[int, "文章的唯一 ID（noteKey）"],
) -> str:
    """获取指定文章的完整内容，包括标题、正文、分类、标签、时间等。"""
    data = _get(f"/notes/{article_id}")
    return str(data)


@tool
def get_top_notes() -> str:
    """获取置顶文章列表。"""
    data = _get("/topnotes")
    return str(data)


# ---------------------------------------------------------------------------
# 分类 / 标签 工具
# ---------------------------------------------------------------------------

@tool
def list_categories() -> str:
    """获取全部分类列表，包含分类名称、颜色、图标、文章数量。"""
    data = _get("/category")
    return str(data)


@tool
def list_tags() -> str:
    """获取全部一级标签列表。"""
    data = _get("/tagone")
    return str(data)


# ---------------------------------------------------------------------------
# 公告 工具
# ---------------------------------------------------------------------------

@tool
def get_announcements() -> str:
    """获取博客公告列表。"""
    data = _get("/announcements")
    return str(data)


# ---------------------------------------------------------------------------
# 友人链 工具
# ---------------------------------------------------------------------------

@tool
def list_friends() -> str:
    """获取友人链（友情链接）列表。"""
    data = _get("/friends")
    return str(data)


# ---------------------------------------------------------------------------
# 说说 / 动态 工具
# ---------------------------------------------------------------------------

@tool
def list_talks() -> str:
    """获取说说（动态/碎语）列表。"""
    data = _get("/talk")
    return str(data)


# ---------------------------------------------------------------------------
# 站点信息 工具
# ---------------------------------------------------------------------------

@tool
def get_blog_info() -> str:
    """获取博客基本信息：作者、头像、签名、ICP备案号等。"""
    data = _get("/user")
    return str(data)


@tool
def get_social_links() -> str:
    """获取社交链接（QQ、GitHub、网易云等）。"""
    data = _get("/social")
    return str(data)


# ---------------------------------------------------------------------------
# 导航 / 引导工具
# ---------------------------------------------------------------------------

@tool
def get_site_map() -> str:
    """返回博客功能结构图，用于引导用户了解博客有哪些功能及其位置。"""
    return """
博客功能结构：
- 首页 (/) — 展示置顶文章、最新文章列表、个人简介
- 归档 (/times) — 按时间轴归档展示所有文章
- 分类 (/category/:name) — 按分类查看文章
- 说说 (/talk) — 动态/碎语
- 友人链 (/friends) — 友情链接
- 关于我 (/about) — 个人介绍
- 文章详情 (/article/:id) — 查看文章全文，支持 Mermaid 图表
- 后台管理 (/dashboard) — 登录后可管理文章、分类、标签、公告等
"""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_TOOL_REGISTRY = [
    list_notes,
    search_notes,
    get_article_detail,
    get_top_notes,
    list_categories,
    list_tags,
    get_announcements,
    list_friends,
    list_talks,
    get_blog_info,
    get_social_links,
    get_site_map,
]


def get_all_tools():
    """Return the list of all registered tools."""
    return _TOOL_REGISTRY
