"""Long-history recovery and mixed reads through the real framework execution path."""

import json

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_own_activity import backend as backend
from test_own_activity import config

from agent.runtime import memory
from agent.runtime.budget import RequestBudget, budget_scope
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.results import TurnResult


class RecoverHistory:
    max_tokens = 128

    def __init__(self):
        self.prompts = []
        self.schemas = []
        self.binding_options = []

    def bind_tools(self, tools, **kwargs):
        from _budget_model import schemas
        self.schemas = schemas(tools)
        self.binding_options.append(kwargs)
        return self

    def stream(self, messages, **kwargs):
        self.prompts.append(list(messages))
        round_index = len(self.prompts)
        if round_index == 1:
            assert "TAIL_CANARY" not in str(messages), "fixture must actually require recovery"
            calls = [self.call("load_own_activity_info_tools", {}, "own"),
                     self.call("load_utilities_info_tools", {}, "utilities")]
        elif round_index == 2:
            calls = [self.call("get_chat_history", {"limit":1}, "history"),
                     self.call("list_my_board", {"item_id":1}, "board"),
                     self.call("get_current_time", {}, "clock")]
        elif round_index == 3:
            first = next(json.loads(message.content) for message in messages
                         if isinstance(message, ToolMessage) and message.tool_call_id == "history")
            entry = first["items"][0]
            assert entry["contentTruncated"] and "TAIL_CANARY" not in entry["content"]
            calls = [self.call("get_chat_history", {
                "message_id":entry["id"], "content_offset":entry["contentLength"]-2000}, "tail")]
        else:
            tail = next(json.loads(message.content) for message in messages
                        if isinstance(message, ToolMessage) and message.tool_call_id == "tail")
            assert "TAIL_CANARY" in tail["items"][0]["content"]
            assert tail["items"][0]["nextContentOffset"] is None
            board = next(message.content for message in messages
                         if isinstance(message, ToolMessage) and message.tool_call_id == "board")
            clock = next(message.content for message in messages
                         if isinstance(message, ToolMessage) and message.tool_call_id == "clock")
            assert "CLOCK_CANARY" in clock
            suffix = ("留言为 BOARD_CANARY。" if "BOARD_CANARY" in board
                      else "留言未能读取，尚未核实。")
            yield AIMessageChunk(content="历史末尾为 TAIL_CANARY；时间为 CLOCK_CANARY；" + suffix,
                                 response_metadata={"finish_reason":"stop"})
            return
        yield AIMessageChunk(content="", tool_calls=calls)

    @staticmethod
    def call(name, args, ident):
        return {"id":ident,"name":name,"args":args}


@pytest.mark.usefixtures("backend")
@pytest.mark.parametrize("board_available", [True, False])
def test_reconstructed_runtime_recovers_unseen_tail_and_preserves_mixed_read_outcomes(
    request, monkeypatch, board_available
):
    backend_state = request.getfixturevalue("backend")
    from utils import clock
    monkeypatch.setattr(clock, "format_local_now", lambda: "CLOCK_CANARY")
    backend_state["board"][0]["content"] = "BOARD_CANARY"
    if not board_available:
        backend_state["board"][0]["ownerId"] = 8  # A foreign record must remain unavailable.
    original = "长文说明\n" * 8000 + "\nTAIL_CANARY"
    backend_state["history"] = [{"id":7,"role":"assistant","content":original}]
    cfg = config()
    cfg["configurable"]["thread_id"] = "conv_9"
    saver = InMemorySaver()
    store = NativeConversationStore(saver)
    store.update_state(cfg, {"messages": memory.turn_messages(
        [HumanMessage(content="之前的长文说明")], [], original,
        cfg["configurable"]["principal"])})
    # A fresh runtime must be able to recover text outside its checkpoint excerpt.
    model = RecoverHistory()
    runtime = NativeRuntime(NativeConversationStore(saver), model_factory=lambda **_:model)
    budget = RequestBudget()
    with budget_scope(budget):
        events = list(runtime.stream_events({"messages":[HumanMessage(
            content="查一下之前长回答末尾的标记，再告诉我河灯留言和当前时间。") ]}, cfg))
    result = events[-1]
    assert isinstance(result, TurnResult) and "TAIL_CANARY" in result.reply, (
        result, budget.tokens, budget.model_calls, budget.reason)
    assert "CLOCK_CANARY" in result.reply
    assert ("BOARD_CANARY" in result.reply) is board_available
    if not board_available:
        assert "尚未核实" in result.reply
    assert result.execution is None, "read-only recovery must not prepare business changes"
    assert budget.reason is None and budget.model_calls == 4 and budget.tool_calls == 6
    assert budget.tokens <= budget.limits.request_tokens
    requests = backend_state["requests"]
    assert len(requests) == 3 and all(request.method == "GET" for request in requests)
    tail = requests[-1].url.params
    assert tail["conversation_id"] == "9" and tail["message_id"] == "7"
    assert int(tail["content_offset"]) == len(original)-2000
    assert model.schemas, "exercise the actual advertised tool menu within the budget"
    assert all(options.get("parallel_tool_calls") is not False
               for options in model.binding_options), "independent native calls may share a round"
