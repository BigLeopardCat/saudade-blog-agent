"""Shared conversation fixtures, independent of pytest collection order."""

from types import SimpleNamespace

from langchain_core.messages import AIMessageChunk

from agent.api.context import build_messages
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore

ACTOR = Principal(7, "user", SOURCE_ASSERTION)
SCOPE = {"user_id": 7, "role": "user", "conversation_id": 42}


def config(principal=ACTOR):
    return {"configurable": {"thread_id": "conv_42", "conversation_id": 42,
                             "user_id": principal.uid, "principal": principal}}


class Capture:
    max_tokens = 64

    def __init__(self, *responses):
        self.prompts = []
        self.responses = iter(responses)

    def bind_tools(self, tools, **kwargs):
        from _budget_model import schemas
        self.kwargs = {"tools": schemas(tools)}
        return self

    def stream(self, messages, **kwargs):
        self.prompts.append(list(messages))
        yield next(self.responses, AIMessageChunk(content="收到。"))


def runtime(saver, model):
    catalog = SimpleNamespace(initial_tools=lambda _: {}, domains=lambda *_: {})
    return NativeRuntime(NativeConversationStore(saver), catalog=catalog,
                         model_factory=lambda **_: model)


def run(selected, req, cfg=None):
    return list(selected.stream_events(
        {"messages": build_messages(req, holds_context=selected.store.available)}, cfg or config()))
