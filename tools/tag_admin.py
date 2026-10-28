"""Shared tag index for catalog reads."""


from langchain_core.runnables.config import RunnableConfig

from tools import base as _base
from tools.result import ToolResult


def _tag_index(config: RunnableConfig):
    """读两级标签字典 → `{id: TagInfo}`；任一读不到 → **None**。

    None 不是"没有标签"，是"这次读不到字典"——调用方必须分开处理（见
    adminops.render_tag_list 的同名区分）。返回 None 而不是抛，是因为"字典读不到"
    在只读场景（渲染清单）不该让整次查询失败，而在写场景由调用方直接拒绝。
    """
    from agent import adminops_tags as _tags
    one = _base._admin_get("/api/tagone", config)
    two = _base._admin_get("/api/tagtwo", config)
    if isinstance(one, ToolResult) or isinstance(two, ToolResult):
        return None
    return _tags.build_tag_index(one, two)

__all__ = []
