"""Native conversation store behavior after the legacy graph adapter retirement."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import checkpoint
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.checkpoint_store import ConversationDeleted
from agent.runtime.results import TurnResult
from agent.runtime.service import AgentRuntime


def config():
    return {"configurable": {
        "thread_id": "conv_42", "conversation_id": 42, "user_id": 7,
        "principal": Principal(7, "user", SOURCE_ASSERTION),
    }}


class Model:
    max_tokens = 64

    def __init__(self, text):
        self.text = text

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages):
        return AIMessage(content=self.text)


def runtime(saver, text="答复"):
    return AgentRuntime(
        CheckpointConversationStore(saver),
        application=BlogApplication(),
        model_factory=lambda **_: Model(text),
    )


def test_reconstructed_native_runtime_continues_saved_history():
    saver = InMemorySaver()
    first = runtime(saver, "第一轮")
    list(first.stream_events({"messages": [HumanMessage(content="问题")]}, config()))
    rebuilt = runtime(saver, "第二轮")
    events = list(rebuilt.stream_events({"messages": [HumanMessage(content="继续")]}, config()))
    assert events[-1] == TurnResult("第二轮")
    messages = rebuilt.get_state(config()).values["messages"]
    assert [message.content for message in messages] == ["问题", "第一轮", "继续", "第二轮"]


def test_delete_barrier_rejects_late_native_write():
    saver = InMemorySaver()
    selected = runtime(saver)
    list(selected.stream_events({"messages": [HumanMessage(content="一次")]}, config()))
    assert checkpoint.delete_thread(saver, 42)
    with pytest.raises(ConversationDeleted):
        selected.store.update_state(config(), {"messages": [AIMessage(content="迟到")]})
