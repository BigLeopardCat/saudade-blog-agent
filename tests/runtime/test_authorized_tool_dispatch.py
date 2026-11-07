"""Dynamic execution uses the current catalog independently of the visible menu."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from test_confirmation_continuation import backend as favorite_backend

from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from tools.result import ok

pytestmark = pytest.mark.tool_scopes(
    find_activity="read.public", inspect_activity="read.public", inspect_admin="admin.console")
backend = favorite_backend


def config(role="admin"):
    return {"configurable": {"principal": Principal(7, role, SOURCE_ASSERTION), "user_id": 7,
                             "conversation_id": 42, "thread_id": "conv_42"}}


def call(name, identifier=None, **args):
    return {"name": name, "args": args, "id": identifier or name}


class Script(BaseChatModel):
    responses: list[Any]
    prompts: list = []
    menus: list = []
    max_tokens: int = 256

    @property
    def _llm_type(self):
        return "authorized-dynamic-tool-fixture"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=deepcopy(tools), **kwargs)

    def _next(self, messages, kwargs):
        self.prompts.append(deepcopy(messages))
        self.menus.append(deepcopy(kwargs.get("tools", [])))
        return self.responses.pop(0)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._next(messages, kwargs))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        response = self._next(messages, kwargs)
        yield ChatGenerationChunk(message=AIMessageChunk(
            **response.model_dump(exclude={"type"})))


@pytest.fixture
def capabilities():
    reads = []

    @tool
    def find_activity(query: str) -> object:
        """Search one synthetic public keyword."""
        reads.append(("public", query))
        return ok("Matching synthetic keyword: " + query)

    @tool
    def inspect_activity() -> object:
        """Read another synthetic public source."""
        reads.append(("detail", None))
        return ok("Other public detail.")

    @tool
    def inspect_admin() -> object:
        """Read a synthetic administrator-only record."""
        reads.append(("admin", None))
        return ok("private fixture")

    def domains(cfg, _):
        result = {"public_activity": {
            find_activity.name: find_activity, inspect_activity.name: inspect_activity}}
        if cfg["configurable"]["principal"].known_role == "admin":
            result["admin_records"] = {inspect_admin.name: inspect_admin}
        return result

    return SimpleNamespace(initial_tools=lambda _: {}, domains=domains), reads


def runtime(saver, model, catalog=None):
    return AgentRuntime(CheckpointConversationStore(saver), application=BlogApplication(catalog),
                        model_factory=lambda **_: model)


def run(selected, text="猫咪的关键词也搜一下", cfg=None):
    return list(selected.stream_events({"messages": [HumanMessage(content=text)]},
                                       cfg or config()))[-1]


def names(menu):
    return [item["function"]["name"] for item in menu]


def test_real_two_turn_followup_keeps_one_completed_read_schema_without_carrying_its_domain(
    capabilities,
):
    catalog, reads = capabilities
    saver = InMemorySaver()
    first = Script(responses=[
        AIMessage(content="", tool_calls=[call("load_public_activity_info_tools")]),
        AIMessage(content="", tool_calls=[call("find_activity", query="看板娘")]),
        AIMessage(content="已检索。")])
    run(runtime(saver, first, catalog), "找看板娘内容")
    keywords = ["猫咪", "猫娘", "看板娘", "猫猫"]
    followup = Script(responses=[AIMessage(content="", tool_calls=[
        call("find_activity", f"query-{index}", query=word)
        for index, word in enumerate(keywords)]), AIMessage(content="已找到。")])
    selected = runtime(saver, followup, catalog)
    assert run(selected).reply == "已找到。"
    assert len(followup.prompts) == 2 and reads == [
        ("public", "看板娘"), *(("public", word) for word in keywords)]
    assert {"find_activity", "inspect_activity"} <= set(names(first.menus[-1]))
    assert names(followup.menus[0]) == [*names(first.menus[0]), "find_activity"]
    assert "inspect_activity" not in names(followup.menus[0])
    assert followup.menus[0] == followup.menus[-1]
    answers = [message for message in followup.prompts[-1]
               if isinstance(message, ToolMessage) and message.tool_call_id.startswith("query-")]
    assert len(answers) == 4 and all(message.status == "success" for message in answers)


def test_unshown_cached_read_name_executes_from_current_catalog_without_a_recent_hint(capabilities):
    catalog, reads = capabilities
    saver = InMemorySaver()
    first = Script(responses=[
        AIMessage(content="", tool_calls=[call("load_public_activity_info_tools")]),
        AIMessage(content="工具已加载，尚未检索。")])
    selected = runtime(saver, first, catalog)
    run(selected, "先查看公开检索的能力")
    assert selected.store.load(config()).recent_read is None and not reads
    assert "find_activity" in names(first.menus[-1])
    followup = Script(responses=[
        AIMessage(content="", tool_calls=[call("find_activity", "cached", query="猫咪")]),
        AIMessage(content="已找到。")])
    assert run(runtime(saver, followup, catalog)).reply == "已找到。"
    assert "find_activity" not in names(followup.menus[0])
    assert len(followup.prompts) == 2 and reads == [("public", "猫咪")]
    assert any(isinstance(message, ToolMessage) and message.tool_call_id == "cached"
               and message.status == "success" for message in followup.prompts[-1])


@pytest.mark.parametrize("name,role", [("inspect_admin", "user"), ("invented_tool", "admin")])
def test_current_role_and_catalog_deny_unavailable_names_without_io(capabilities, name, role):
    catalog, reads = capabilities
    saver = InMemorySaver()
    first = Script(responses=[AIMessage(content="", tool_calls=[
        call("load_admin_records_info_tools")]), AIMessage(content="已加载。")])
    run(runtime(saver, first, catalog))
    current = Script(responses=[AIMessage(content="", tool_calls=[call(name)]),
                                AIMessage(content="本次没有执行。")])
    run(runtime(saver, current, catalog), cfg=config(role))
    assert not reads and name not in names(current.menus[0])
    assert any(isinstance(message, ToolMessage) and message.status == "error"
               for message in current.prompts[-1])


def test_exact_tool_name_still_requires_framework_argument_validation(capabilities):
    catalog, reads = capabilities
    model = Script(responses=[AIMessage(content="", tool_calls=[call("find_activity")]),
                              AIMessage(content="需要补充检索词。")])
    run(runtime(InMemorySaver(), model, catalog))
    assert not reads and len(model.prompts) == 2
    assert any(isinstance(message, ToolMessage) and message.status == "error"
               for message in model.prompts[-1])


def test_dynamic_return_direct_preserves_real_approval_narration_and_zero_writes(backend):
    model = Script(responses=[AIMessage(content="", tool_calls=[call(
        "prepare_favorite_change", action="add", article_id=19)]),
        AIMessage(content="请确认是否收藏这篇文章。")])
    selected = runtime(InMemorySaver(), model)
    result = run(selected, "准备收藏文章19", config("user"))
    assert result.execution["pending_confirm"] and result.reply == "请确认是否收藏这篇文章。"
    assert len(model.prompts) == 2 and not backend[0]["writes"]
    assert "prepare_favorite_change" not in names(model.menus[0])
    assert any(isinstance(message, ToolMessage) and
               (message.artifact or {}).get("phase") == "awaiting_approval"
               for message in model.prompts[-1])


@pytest.mark.parametrize("kind", ["invalid_args", "unavailable", "not_found"])
def test_unshown_return_direct_tool_cannot_bypass_failed_dependency(backend, kind):
    from tools.result import ToolResult

    checked = []

    @tool
    def find_activity(query: str) -> object:
        """Check one synthetic prerequisite."""
        checked.append(query)
        return ToolResult("prerequisite unavailable", kind)

    model = Script(responses=[AIMessage(content="", tool_calls=[
        call("find_activity", "failed", query="猫咪"),
        call("prepare_favorite_change", "blocked", action="add", article_id=19)]),
        AIMessage(content="前置查询未成功，尚未准备收藏。")])
    catalog = ToolCatalog(read_tools={find_activity.name: find_activity})
    result = run(runtime(InMemorySaver(), model, catalog), cfg=config("user"))
    assert checked == ["猫咪"] and not backend[0]["requests"]
    assert not (result.execution or {}).get("pending_confirm")
    assert len(model.prompts) == 2
    blocked = next(message for message in model.prompts[-1]
                   if isinstance(message, ToolMessage) and message.tool_call_id == "blocked")
    assert blocked.status == "error" and "前序调用失败" in blocked.content
