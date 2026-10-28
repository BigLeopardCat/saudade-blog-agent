"""Shared category index for catalog reads."""


from langchain_core.runnables.config import RunnableConfig

from tools import base as _base
from tools.result import ToolResult


def _category_index(config: RunnableConfig):
    """分类字典 → `{id: CategoryInfo}`；读不到返回 None（≠"没有分类"）。"""
    from agent import adminops_tags as _tags
    data = _base._admin_get("/api/category", config)
    if isinstance(data, ToolResult):
        return None
    return _tags.build_category_index(data)

__all__ = []
