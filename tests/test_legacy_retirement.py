"""The supported service must not load the retired planner implementation."""

import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RETIRED = (
    "agent.agent",
    "agent.graph",
    "agent.planner_llm",
    "agent.plan_codec",
    "agent.deterministic_plan",
    "agent.prompts",
    "agent.tasks",
    "agent.workflow_defs",
    "agent.runtime.hybrid",
    "agent.runtime.graph_store",
    "agent.runtime.legacy_bridge",
)


def test_retired_modules_have_no_importable_source():
    assert all(importlib.util.find_spec(name) is None for name in RETIRED)


def test_native_service_process_does_not_load_retired_modules():
    script = """
import server
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.messages import AIMessage
from agent.principal import Principal, SOURCE_ASSERTION

class Model:
    def bind_tools(self, tools, **kwargs):
        return self
    def invoke(self, messages):
        return AIMessage(content='原生响应')

runtime = NativeRuntime(NativeConversationStore(InMemorySaver()),
                        model_factory=lambda **_: Model())
config = {'configurable': {'thread_id': 'conv_1', 'conversation_id': 1,
                           'user_id': 1,
                           'principal': Principal(1, 'user', SOURCE_ASSERTION)}}
list(runtime.stream_events({'messages': []}, config))
assert not any(name in __import__('sys').modules for name in %r)
""" % (RETIRED,)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env={"PYTHONPATH": str(ROOT)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
