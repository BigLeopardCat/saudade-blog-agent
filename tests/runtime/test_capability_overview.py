"""Capability questions use the authorized catalog without expanding tool schemas."""

import json

import pytest
from _framework_fixture import call, model_replies
from _own_support import config
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.message_content import estimate_tokens
from agent.runtime.service import AgentRuntime
from utils.trace import tool_result_text


@pytest.mark.parametrize("role", ["admin", "user", "zako"])
@pytest.mark.parametrize("available", [False, True])
def test_overview_projects_only_current_catalog_and_does_not_claim_missing_writes(role, available):
    cfg = config(principal=Principal(7, role, SOURCE_ASSERTION))
    store = CheckpointConversationStore(InMemorySaver())
    application = BlogApplication()
    initial, domains = application.tools(cfg, store.load(cfg), None, store.saver, available)
    if role == "zako":
        assert initial == domains == {}
        return
    overview = initial["get_agent_capabilities"].invoke({})
    assert overview["grants_authorization"] is False
    assert {row["name"]: list(row["tools"]) for row in overview["domains"]} == {
        name: list(tools) for name, tools in domains.items() if tools}
    for row in overview["domains"]:
        detail = initial["get_agent_capabilities"].invoke({"domain": row["name"]})
        assert detail["available"] is True and detail["grants_authorization"] is False
        assert detail["tools"] == {name: tool.description
                                   for name, tool in domains[row["name"]].items()}
    shown = {name for row in overview["domains"] for name in row["tools"]}
    assert ("prepare_user_notice" in shown) == (role == "admin" and available)
    assert ("get_moderation_status" in shown) == (role == "admin")
    assert ("prepare_favorite_change" in shown) == available
    assert "admin_automations" not in {row["name"] for row in overview["domains"]}
    if role == "user":
        assert not any(row["name"].startswith("admin_") for row in overview["domains"])
    text = json.dumps(overview, ensure_ascii=False)
    assert '"parameters"' not in text and '"properties"' not in text
    assert tool_result_text(text, "get_agent_capabilities", default_limit=8000) == text
    overview_size = estimate_tokens([ToolMessage(content=text, tool_call_id="overview")])
    schemas = json.dumps([convert_to_openai_tool(tool)
                          for tools in domains.values() for tool in tools.values()],
                         ensure_ascii=False)
    schema_size = estimate_tokens([ToolMessage(content=schemas, tool_call_id="schemas")])
    assert overview_size < 8192
    assert overview_size < schema_size / 4
    schema = convert_to_openai_tool(initial["get_agent_capabilities"])
    assert "domain" in schema["function"]["parameters"]["properties"]
    assert "domain" not in schema["function"]["parameters"].get("required", [])
    detailed = [initial["get_agent_capabilities"].invoke({"domain": name}) for name in domains]
    assert len(text) < len(json.dumps(detailed, ensure_ascii=False)) / 2
    unavailable = initial["get_agent_capabilities"].invoke({"domain": "not_authorized"})
    assert unavailable["available"] is False and unavailable["tools"] == {}


def test_real_sdk_answers_from_one_catalog_read_without_loading_or_executing_domains():
    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    store = CheckpointConversationStore(InMemorySaver())
    with model_replies({"tool_calls": [call("get_agent_capabilities")]},
                       {"content": "可查询审核情况，准备标签、分类和待办变更。"}) as (model, wire):
        runtime = AgentRuntime(store, application=BlogApplication(),
                               model_factory=lambda **_: model)
        result = list(runtime.stream_events(
            {"messages": [HumanMessage(content="管理员能做什么？")]}, cfg))
    assert result[-1].reply == "可查询审核情况，准备标签、分类和待办变更。"
    assert len(wire) == 2 and wire[0]["tools"] == wire[1]["tools"]
    observations = [message for message in wire[1]["messages"] if message["role"] == "tool"]
    assert len(observations) == 1
    data = json.loads(observations[0]["content"])
    assert any(row["name"] == "admin_work_queue" for row in data["domains"])
    assert "get_moderation_status" in observations[0]["content"]
    names = {tool["function"]["name"] for tool in wire[1]["tools"]}
    assert "prepare_user_notice" not in names and "get_moderation_status" not in names
    saved = store.load(cfg)
    assert not (saved.tool_domains or {}).get("domains")
