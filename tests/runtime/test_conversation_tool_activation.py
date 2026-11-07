"""Conversation capabilities persist independently of task state and summarized prose."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from agent.products.blog.application import BlogApplication
from agent.runtime import memory
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from agent.runtime.task_pause import pause_task
from agent.runtime.turns import AgentCancelled
from config.settings import settings
from tools.result import ok

pytestmark = pytest.mark.tool_scopes(find_activity="read.public", inspect_admin="admin.console")
ACTOR = Principal(7, "admin", SOURCE_ASSERTION)


def config(actor=ACTOR, cid=42):
    return {
        "configurable": {
            "principal": actor,
            "user_id": actor.uid,
            "conversation_id": cid,
            "thread_id": f"conv_{cid}",
        }
    }


def call(name, identifier=None, **args):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": identifier or name}]
    )


class Script(BaseChatModel):
    responses: list[Any]
    prompts: list = []
    menus: list = []
    max_tokens: int = 256

    @property
    def _llm_type(self):
        return "conversation-activation-fixture"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=deepcopy(tools), **kwargs)

    def _next(self, messages, kwargs):
        self.prompts.append(deepcopy(messages))
        self.menus.append(deepcopy(kwargs.get("tools", [])))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._next(messages, kwargs))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        response = self._next(messages, kwargs)
        yield ChatGenerationChunk(message=AIMessageChunk(**response.model_dump(exclude={"type"})))


@pytest.fixture
def capabilities():
    reads = []

    @tool
    def find_activity(query: str) -> object:
        """Search one synthetic public keyword."""
        reads.append(("public", query))
        return ok(json.dumps({"keyword": query, "items": []}))

    @tool
    def inspect_admin() -> object:
        """Read synthetic administrator-only data."""
        reads.append(("admin", None))
        return ok("private fixture")

    domains = {
        "public_activity": {find_activity.name: find_activity},
        "admin_records": {inspect_admin.name: inspect_admin},
    }

    def catalog(cfg, _):
        actor = cfg["configurable"]["principal"]
        return {
            key: tools
            for key, tools in domains.items()
            if key != "admin_records" or actor.known_role == "admin"
        }

    return (
        SimpleNamespace(initial_tools=lambda _: {pause_task.name: pause_task}, domains=catalog),
        reads,
        domains,
    )


def runtime(saver, model, catalog):
    return AgentRuntime(
        CheckpointConversationStore(saver),
        application=BlogApplication(catalog),
        model_factory=lambda **_: model,
    )


def run(selected, text="猫咪的关键词也搜一下", cfg=None, **state):
    return list(
        selected.stream_events({"messages": [HumanMessage(content=text)], **state}, cfg or config())
    )[-1]


def names(menu):
    return [item["function"]["name"] for item in menu]


def test_two_real_agent_turns_reuse_activated_tools_after_runtime_reload(capabilities):
    catalog, reads, _ = capabilities
    saver = InMemorySaver()
    first = Script(
        responses=[
            call("load_public_activity_info_tools"),
            call("find_activity", query="看板娘"),
            AIMessage(content="已检索。"),
        ]
    )
    selected = runtime(saver, first, catalog)
    assert run(selected, "找看板娘内容").reply == "已检索。"
    saved = selected.store.load(config())
    assert saved.tool_activation == {
        "scope": {"uid": 7, "role": "admin", "conversation_id": 42},
        "domains": ["public_activity"],
    }
    assert saved.run is None
    keywords = ["猫咪", "猫娘", "看板娘", "猫猫"]
    batch = AIMessage(
        content="",
        tool_calls=[
            {"name": "find_activity", "args": {"query": word}, "id": f"query-{index}"}
            for index, word in enumerate(keywords)
        ],
    )
    next_model = Script(responses=[batch, AIMessage(content="已找到。")])
    reloaded = runtime(saver, next_model, catalog)
    assert run(reloaded).reply == "已找到。"
    assert reads == [("public", "看板娘"), *(("public", word) for word in keywords)]
    assert len(next_model.prompts) == 2
    assert "find_activity" in names(next_model.menus[0])
    assert next_model.menus[0] == first.menus[-1], "schema prefix/order must survive a new task"
    assert not any(
        isinstance(m, ToolMessage) and m.status == "error"
        for prompt in next_model.prompts
        for m in prompt
    )


def test_saved_activation_order_is_stable_and_removed_catalog_domains_are_filtered(capabilities):
    catalog, reads, domains = capabilities
    saver = InMemorySaver()
    first = Script(
        responses=[
            call("load_admin_records_info_tools"),
            call("load_public_activity_info_tools"),
            AIMessage(content="已准备。"),
        ]
    )
    selected = runtime(saver, first, catalog)
    run(selected)
    assert selected.store.load(config()).tool_activation["domains"] == [
        "admin_records",
        "public_activity",
    ]
    same = Script(responses=[call("inspect_admin"), AIMessage(content="继续。")])
    run(runtime(saver, same, catalog))
    assert same.menus[0] == first.menus[-1]
    assert reads == [("admin", None)]
    restricted = SimpleNamespace(
        initial_tools=catalog.initial_tools,
        domains=lambda *_: {"public_activity": domains["public_activity"]},
    )
    filtered = Script(responses=[call("inspect_admin"), AIMessage(content="该能力已撤下。")])
    current = runtime(saver, filtered, restricted)
    run(current)
    assert "inspect_admin" not in names(filtered.menus[0])
    assert current.store.load(config()).tool_activation["domains"] == ["public_activity"]
    assert reads == [("admin", None)]


@pytest.mark.parametrize(
    "actor,cid",
    [
        (replace(ACTOR, role="user"), 42),
        (replace(ACTOR, uid=8), 42),
        (replace(ACTOR, source=SOURCE_BODY), 42),
        (ACTOR, 43),
    ],
)
def test_identity_and_conversation_changes_do_not_inherit_activation(capabilities, actor, cid):
    catalog, reads, _ = capabilities
    saver = InMemorySaver()
    initial = Script(
        responses=[call("load_admin_records_info_tools"), AIMessage(content="准备好。")]
    )
    run(runtime(saver, initial, catalog))
    current = Script(responses=[call("inspect_admin"), AIMessage(content="没有执行。")])
    selected = runtime(saver, current, catalog)
    run(selected, cfg=config(actor, cid))
    assert "inspect_admin" not in names(current.menus[0])
    assert not reads
    saved = selected.store.load(config(actor, cid)).tool_activation
    assert saved is None or saved["domains"] == []


def test_previous_checkpoint_without_activation_never_infers_it_from_loader_text(capabilities):
    catalog, reads, _ = capabilities
    saver = InMemorySaver()
    selected = runtime(saver, Script(responses=[]), catalog)
    selected.store.update_state(
        config(),
        {
            "messages": memory.turn_messages(
                [HumanMessage(content="先加载")], [], "public_activity 工具已加载。", ACTOR
            )
        },
    )
    model = Script(
        responses=[call("find_activity", query="猫咪"), AIMessage(content="需要先加载。")]
    )
    run(runtime(saver, model, catalog))
    assert "find_activity" not in names(model.menus[0]) and not reads


@pytest.mark.parametrize("interrupt", ["cancel", "pause"])
def test_checkpoint_resume_restores_child_activation_then_preserves_it_for_next_turn(
    capabilities, interrupt
):
    catalog, reads, _ = capabilities
    saver = InMemorySaver()
    response = (
        AgentCancelled()
        if interrupt == "cancel"
        else call("pause_task", instruction="稍后继续搜索猫咪")
    )
    before = Script(responses=[call("load_public_activity_info_tools"), response])
    selected = runtime(saver, before, catalog)
    if interrupt == "cancel":
        with pytest.raises(AgentCancelled):
            run(selected, task_id="held-search")
    else:
        assert run(selected, task_id="held-search").execution["paused_task"]
    after = Script(responses=[call("find_activity", query="猫咪"), AIMessage(content="恢复完成。")])
    reloaded = runtime(saver, after, catalog)
    result = list(reloaded.stream_events({"resume_task": "held-search"}, config()))[-1]
    assert result.reply == "恢复完成。" and reads == [("public", "猫咪")]
    assert after.menus[0] == before.menus[-1]
    followup = Script(
        responses=[call("find_activity", query="猫猫"), AIMessage(content="再次找到。")]
    )
    assert run(runtime(saver, followup, catalog), "猫猫呢").reply == "再次找到。"
    assert followup.menus[0] == after.menus[-1]
    assert reads == [("public", "猫咪"), ("public", "猫猫")]


def test_framework_compaction_preserves_loaded_schema(capabilities, monkeypatch):
    catalog, reads, _ = capabilities
    saver = InMemorySaver()
    first = Script(
        responses=[call("load_public_activity_info_tools"), AIMessage(content="已加载。")]
    )
    selected = runtime(saver, first, catalog)
    run(selected)
    activation = selected.store.load(config()).tool_activation
    history = []
    for index in range(20):
        history.extend(
            memory.turn_messages(
                [HumanMessage(content=f"历史{index} " + "上下文 " * 90)], [], "旧回复 " * 90, ACTOR
            )
        )
    selected.store.update_state(config(), {"messages": history})
    monkeypatch.setattr(settings, "agent_context_tokens", 12000)
    monkeypatch.setattr(settings, "agent_summary_output_tokens", 512)
    monkeypatch.setattr("models.get_llm", lambda **_: FakeListChatModel(responses=["历史摘要。 "]))
    second = Script(responses=[call("find_activity", query="猫咪"), AIMessage(content="已核实。")])
    current = runtime(saver, second, catalog)
    assert run(current).reply == "已核实。"
    snapshot = current.store.load(config())
    assert any(
        message.additional_kwargs.get("lc_source") == "summarization"
        for message in snapshot.messages
    )
    assert snapshot.tool_activation == activation
    assert second.menus[0] == first.menus[-1] and reads == [("public", "猫咪")]
