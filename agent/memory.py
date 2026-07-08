"""Conversation memory management.

In langchain 1.3+, the agent uses LangGraph with a checkpointer
for conversation persistence across turns.
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
