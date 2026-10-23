"""Native progress stays on the existing process-frame path."""

import threading
import time

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from agent.runtime.progress import ProgressEmitter
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
    assert len(visible) == 1


def test_cancelled_transport_drops_late_progress():
    cancelled = threading.Event()
    visible = []
    emitter = ProgressEmitter(
        lambda text: None if cancelled.is_set() else visible.append(text)
    )
    cancelled.set()
    assert emitter.emit("正在查询相关资料…", key="tool:x")
    assert visible == []
