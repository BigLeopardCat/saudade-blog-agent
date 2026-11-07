"""Current-user favorite reads and authoritative mutation receipts."""

from typing import Annotated

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from tools.base import _own_delete, _own_get, _own_post, _own_write_guard
from tools.result import ToolResult, _shape, fact, ok, tgt, unavailable

_ArticleId = Annotated[StrictInt, Field(gt=0, le=2_147_483_647)]


@tool
def list_my_favorites(config: RunnableConfig) -> str | ToolResult:
    """列出本人收藏的公开文章及编号；读取收藏夹无需先读取文章正文。"""
    return _shape(_own_get("/api/protected/favorites", config))


def favorite_state(article_id: int, config: RunnableConfig):
    """Read an owned relationship independently of public article visibility."""
    value = _own_get(f"/api/protected/favorites/{article_id}", config)
    if isinstance(value, ToolResult):
        return value
    principal = ((config or {}).get("configurable") or {}).get("principal")
    if (not isinstance(value, dict) or principal is None
            or type(value.get("ownerUserId")) is not int or value["ownerUserId"] != principal.uid
            or type(value.get("noteId")) is not int or value["noteId"] != article_id
            or type(value.get("isFavorite")) is not bool
            or type(value.get("articleAvailable")) is not bool
            or (value["articleAvailable"] and
                (not isinstance(value.get("title"), str) or not value["title"].strip()))
            or (not value["articleAvailable"] and value.get("title") is not None)):
        return unavailable("未能核对本人收藏与文章标识，本次未准备改动。")
    return value


def _change(article_id, desired, config):
    guard = _own_write_guard(config)
    if guard is not None:
        return guard
    value = (_own_post("/api/protected/favorites", {"noteId": article_id}, config)
             if desired else _own_delete(f"/api/protected/favorites/{article_id}", config))
    if isinstance(value, ToolResult):
        return value
    principal = ((config or {}).get("configurable") or {}).get("principal")
    if (not isinstance(value, dict) or principal is None
            or type(value.get("ownerUserId")) is not int or value["ownerUserId"] != principal.uid
            or type(value.get("noteId")) is not int or value["noteId"] != article_id
            or type(value.get("isFavorite")) is not bool or value["isFavorite"] is not desired
            or type(value.get("changed")) is not bool):
        return unavailable("请求已发出，但没有收到可验证的收藏结果；请先查询状态。")
    changed = value["changed"]
    action = "收藏" if desired else "取消收藏"
    text = (f"已{action}文章 {article_id}。" if changed
            else f"文章 {article_id} 的收藏状态已满足，本次无需改动。")
    return ok(text, meta=fact("favorite_add" if desired else "favorite_remove", changed=changed,
                             target=tgt("note", article_id), article_id=article_id,
                             change=action if changed else "状态已满足"))


@tool
def add_favorite(article_id: _ArticleId, config: RunnableConfig) -> str | ToolResult:
    """收藏已确认编号的公开文章，返回后端提交的真实变更事实。"""
    return _change(article_id, True, config)


@tool
def remove_favorite(article_id: _ArticleId, config: RunnableConfig) -> str | ToolResult:
    """取消本人已确认编号的收藏；文章变为私密或草稿也不影响移除自己的关系。"""
    return _change(article_id, False, config)


__all__ = ["list_my_favorites", "add_favorite", "remove_favorite"]
