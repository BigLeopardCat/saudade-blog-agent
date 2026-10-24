"""Native progress stays on the existing process-frame path."""

import threading
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from agent.runtime.progress import ProgressEmitter
from agent.runtime.results import TurnResult
from agent.runtime.session import ToolSession


class _Model:
    def __init__(self, responses):
        self.responses = iter(responses)

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, prompt, **kwargs):
        return next(self.responses)


class _BlockingTool:
    name = "fixture_read"

    def __init__(self, started, release):
        self.started, self.release = started, release

    def invoke(self, args, config=None):
        self.started.set()
        assert self.release.wait(2)
        return "private result must stay out of progress"

    @property
    def tool(self):
        return StructuredTool.from_function(
            lambda: self.invoke({}, None), name=self.name,
            description="Read the fixture resource.",
        )


def _call(name="fixture_read"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": "1"}])


def test_progress_arrives_while_native_tool_is_blocked():
    started, release = threading.Event(), threading.Event()
    visible = []
    emitter = ProgressEmitter(visible.append)
    session = ToolSession(
        _Model([_call(), AIMessage(content="done")]),
        {"configurable": {"progress_emitter": emitter}},
        {"fixture_read": _BlockingTool(started, release).tool},
        {},
    )
    result = []
    worker = threading.Thread(target=lambda: result.append(
        session.run([HumanMessage(content="read")])) )
    worker.start()
    assert started.wait(2)
    deadline = time.monotonic() + 1
    while len(visible) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert visible
    assert all("private result" not in item for item in visible)
    release.set()
    worker.join(2)
    assert result and result[0].reply == "done"


def test_progress_is_bounded_and_deduplicated():
    visible = []
    emitter = ProgressEmitter(visible.append)
    assert emitter.emit("正在查询相关资料…", key="tool:x")
    assert not emitter.emit("正在查询相关资料…", key="tool:x")
    for i in range(50):
        emitter.emit(f"step {i}", key=f"step:{i}")
    assert len(visible) == 24


def test_transport_rechecks_stop_and_deadline_after_scheduling():
    import asyncio

    from agent.runtime.control import RequestControl
    from agent.runtime.progress import StreamProgress

    async def scenario():
        for cancelled in (True, False):
            stop = threading.Event()
            control = RequestControl(stop, time.monotonic() + 10)
            queue = asyncio.Queue()
            progress = StreamProgress(asyncio.get_running_loop(), queue, control)
            assert progress.enqueue("queued")
            if cancelled:
                stop.set()
            else:
                object.__setattr__(control, "deadline", time.monotonic() - 1)
            await asyncio.sleep(0)
            assert queue.empty()
            assert not progress.enqueue("late")

    asyncio.run(scenario())


def _prepare_server(monkeypatch, runtime, events):
    import asyncio

    import server
    from agent.principal import SOURCE_ASSERTION, Principal

    monkeypatch.setattr(server, "_agent", runtime)
    monkeypatch.setattr(server, "_resolve_principal",
                        lambda request, uid: Principal(7, "user", SOURCE_ASSERTION))
    monkeypatch.setattr(server, "_stream_slots", asyncio.BoundedSemaphore(1))
    monkeypatch.setattr(server, "STREAM_QUEUE_WAIT", .01)
    monkeypatch.setattr(server, "start_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "finish_trace", lambda *args: None)
    def capture(node, event, **data):
        events.append({"node": node, "event": event, **data})

    monkeypatch.setattr(server, "record", capture)
    monkeypatch.setattr("agent.runtime.progress.record", capture)
    return server


def test_real_sse_progress_precedes_blocked_tool_and_first_reply(monkeypatch):
    import asyncio

    from langchain_core.messages import AIMessageChunk

    entered, release = threading.Event(), threading.Event()
    events = []

    class Runtime:
        def stream_events(self, state, config):
            session = ToolSession(
                _Model([_call(), AIMessage(content="done")]), config,
                {"fixture_read": _BlockingTool(entered, release).tool}, {},
            )
            result = session.run([HumanMessage(content="read")])
            yield AIMessageChunk(content=result.reply)
            yield result

    server = _prepare_server(monkeypatch, Runtime(), events)

    async def scenario():
        response = await server.chat_stream(server.ChatRequest(message="read"), None)
        try:
            first = await asyncio.wait_for(anext(response.body_iterator), 2)
            assert first.startswith("event: progress\ndata: ")
            assert await asyncio.to_thread(entered.wait, 1)
            assert not release.is_set()
            visible = [e for e in events if e["event"] == "first_visible_event"]
            assert [e["kind"] for e in visible] == ["progress"]
            assert not any(e["event"] == "first_reply" for e in events)
            release.set()
            rest = "".join([frame async for frame in response.body_iterator])
            assert '"done"' in rest and "event: done" in rest
            assert len([e for e in events if e["event"] == "first_reply"]) == 1
        finally:
            release.set()
            await response.body_iterator.aclose()

    asyncio.run(scenario())


def test_real_sse_native_answer_chunk_arrives_before_provider_stream_ends(monkeypatch):
    import asyncio
    import json

    from langchain_core.messages import AIMessageChunk

    entered, release = threading.Event(), threading.Event()
    events = []

    class StreamingModel:
        max_tokens = 64

        def bind_tools(self, tools, **kwargs):
            return self

        def stream(self, prompt, **kwargs):
            yield AIMessageChunk(content="首段正文")
            entered.set()
            assert release.wait(2)
            yield AIMessageChunk(content="后段正文")

    class Runtime:
        def stream_events(self, state, config):
            session = ToolSession(StreamingModel(), config, {}, {})
            runner = session.stream([HumanMessage(content="回答")])
            chunks = []
            while True:
                try:
                    chunk = next(runner)
                except StopIteration as stop:
                    outcome = stop.value
                    break
                chunks.append(chunk)
                if chunk.content:
                    yield chunk
            yield outcome

    server = _prepare_server(monkeypatch, Runtime(), events)

    async def scenario():
        response = await server.chat_stream(server.ChatRequest(message="回答"), None)
        try:
            first = await asyncio.wait_for(anext(response.body_iterator), 2)
            assert first.startswith("event: progress\ndata: ")
            second = await asyncio.wait_for(anext(response.body_iterator), 2)
            assert json.loads(second.split("data: ", 1)[1]) == "首段正文"
            assert await asyncio.to_thread(entered.wait, 1)
            assert not release.is_set()
            release.set()
            rest = "".join([frame async for frame in response.body_iterator])
            assert '"后段正文"' in rest and "event: done" in rest
            assert len([e for e in events if e["event"] == "first_reply"]) == 1
        finally:
            release.set()
            await response.body_iterator.aclose()

    asyncio.run(scenario())


def test_real_sse_progress_and_whitespace_do_not_hide_empty_reply(monkeypatch):
    import asyncio

    from langchain_core.messages import AIMessageChunk

    events = []
    server = _prepare_server(monkeypatch, object(), events)

    def producer(messages, thread, queue, loop, uid, stop, *args):
        args[-1].emit("working", key="work")
        loop.call_soon_threadsafe(queue.put_nowait, AIMessageChunk(content=" \n"))
        loop.call_soon_threadsafe(queue.put_nowait, None)

    monkeypatch.setattr(server, "_run_agent_stream_to_queue", producer)

    async def scenario():
        response = await server.chat_stream(server.ChatRequest(message="read"), None)
        body = "".join([frame async for frame in response.body_iterator])
        assert server._RECOVERY_SENTENCE in body
        assert len([e for e in events if e["event"] == "first_reply"]) == 1

    asyncio.run(scenario())


def test_real_sse_internal_frames_are_not_first_visible(monkeypatch):
    import asyncio

    from langchain_core.messages import AIMessageChunk

    events = []
    server = _prepare_server(monkeypatch, object(), events)

    def producer(messages, thread, queue, loop, uid, stop, *args):
        for frame in ('__PENDING__:{}', '__EXEC__:[]', AIMessageChunk(content=" \n"),
                      RuntimeError("private error"), None):
            loop.call_soon_threadsafe(queue.put_nowait, frame)

    monkeypatch.setattr(server, "_run_agent_stream_to_queue", producer)

    async def scenario():
        response = await server.chat_stream(server.ChatRequest(message="read"), None)
        body = "".join([frame async for frame in response.body_iterator])
        assert "event: error" in body and "private error" not in body
        assert [e["kind"] for e in events if e["event"] == "first_visible_event"] == ["error"]
        assert not any(e["event"] == "first_reply" for e in events)

    asyncio.run(scenario())


def test_real_stream_close_fences_emitter_until_worker_drains(monkeypatch):
    import asyncio

    entered, release = threading.Event(), threading.Event()
    accepted = []
    events = []

    class Runtime:
        def stream_events(self, state, config):
            emitter = config["configurable"]["progress_emitter"]
            emitter.emit("working", key="first")
            entered.set()
            assert release.wait(3)
            accepted.append(emitter.emit("late", key="late"))
            yield TurnResult("")

    server = _prepare_server(monkeypatch, Runtime(), events)

    async def scenario():
        drained = asyncio.Event()
        monkeypatch.setattr(server, "finish_trace", lambda *args: drained.set())
        response = await server.chat_stream(server.ChatRequest(message="read"), None)
        try:
            assert "event: progress" in await asyncio.wait_for(anext(response.body_iterator), 2)
            assert await asyncio.to_thread(entered.wait, 1)
            await response.body_iterator.aclose()
            assert not await server._try_acquire_slot()
        finally:
            release.set()
        await asyncio.wait_for(drained.wait(), 2)
        assert accepted == [False]
        assert await server._try_acquire_slot()
        server._release_slot()

    asyncio.run(scenario())


def test_streamed_preface_and_terminal_confirmation_each_render_once(monkeypatch):
    import asyncio
    import json
    import uuid

    from langchain_core.messages import AIMessageChunk
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.runtime.capabilities import ToolCatalog
    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore
    from agent.runtime.results import TurnResult

    question = "确认本次测试操作。"

    @tool(return_direct=True)
    def fixture_prepare() -> object:
        """Prepare a fixture confirmation without executing business operations."""
        return TurnResult(question, {"pending_confirm": {
            "q": question, "token": uuid.uuid4().hex, "opts": [], "exp": 0},
            "confirm_text": question, "receipts": []})

    class Model:
        max_tokens = 64

        def bind_tools(self, tools, **kwargs):
            return self

        def stream(self, prompt, **kwargs):
            yield AIMessageChunk(content="准备好了。")
            yield AIMessageChunk(content="", tool_calls=[
                {"name": "fixture_prepare", "args": {}, "id": "prepare"}])

    store = NativeConversationStore(InMemorySaver())
    runtime = NativeRuntime(store, model_factory=lambda **_: Model(),
                            catalog=ToolCatalog(read_tools={"fixture_prepare": fixture_prepare},
                                                site_read_tools={}))
    events = []
    server = _prepare_server(monkeypatch, runtime, events)

    async def scenario():
        drained = asyncio.Event()
        monkeypatch.setattr(server, "finish_trace", lambda *args: drained.set())
        response = await server.chat_stream(
            server.ChatRequest(message="prepare", conversation_id=42), None)
        text, cards = [], []
        async for frame in response.body_iterator:
            kind, payload = frame.split("\ndata: ", 1)
            if kind == "event: confirmation":
                cards.append(json.loads(payload))
            elif kind == "event: text":
                text.append(json.loads(payload))
        await asyncio.wait_for(drained.wait(), 2)
        reply = "".join(text)
        assert reply.count("准备好了。") == 1
        assert reply.count(question) == 1 and len(cards) == 1
        stored = runtime.get_state({"configurable": {"thread_id": "conv_42"}})
        assert stored.values["messages"][-1].content == reply

    asyncio.run(scenario())


pytestmark = pytest.mark.usefixtures("service_workers")
