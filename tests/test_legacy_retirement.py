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
import agent.main
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.messages import AIMessage, HumanMessage
from agent.principal import Principal, SOURCE_ASSERTION
from eval.own_fixtures import favorite_backend
from agent import confirm

def call(name, args):
    return AIMessage(content='', tool_calls=[{'name':name, 'args':args, 'id':name}])

class Model:
    def __init__(self):
        self.responses = iter([
            call('navigate_page', {'path':'/about'}),
            call('load_favorites_info_tools', {}),
            call('list_my_favorites', {}),
            call('prepare_favorite_change', {'action':'add', 'article_id':19}),
        ])
    def bind_tools(self, tools, **kwargs):
        return self
    def invoke(self, messages):
        return next(self.responses)

runtime = NativeRuntime(NativeConversationStore(InMemorySaver()),
                        model_factory=lambda **_: Model())
config = {'configurable': {'thread_id': 'conv_1', 'conversation_id': 1,
                           'user_id': 1,
                           'principal': Principal(1, 'user', SOURCE_ASSERTION)}}
with favorite_backend() as backend:
    state = {'messages': [HumanMessage(content='收藏文章19')]}
    result = list(runtime.stream_events(state, config))[-1]
    assert result.execution['receipts'][0]['cmd']['url'].endswith('/about')
    token = result.execution['pending_confirm']['token']
    assert confirm.verify(token, 1, 1)['executor'] == 'favorites'
    assert backend['requests'] and all(request.method == 'GET' for request in backend['requests'])
assert not any(name in __import__('sys').modules for name in %r)
""" % (RETIRED,)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "SAUDADE_IGNORE_ENV_FILE": "1", "CHECKPOINT_DSN": ""},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
