"""MemorySaver 兼容存根（弃用，20260826 起）。

记忆已外置 MySQL：连续性靠 Rust 注入最近 20 条历史 + 滚动摘要（摘要由后端独立
任务生成，模型对记忆无写权限）；图每请求独立线程、无 checkpointer。本文件仅
为兼容历史导入而保留（agent/__init__.py 导出），不承担记忆职责（见架构文档
§5.1 与问题记录 5.1）。
"""

from langgraph.checkpoint.memory import MemorySaver


def get_checkpointer() -> MemorySaver:
    """Create an in-memory checkpointer for conversation history.

    The checkpointer saves the state after each step, allowing the
    agent to maintain context across multiple turns in a session.

    Returns:
        MemorySaver: An in-memory checkpoint saver.
    """
    return MemorySaver()
    
