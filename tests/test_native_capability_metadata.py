"""Initial discovery must expose the site's terms before a domain is loaded."""

import pytest
from _own_support import config
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.domains.notices.targets import DEFAULT_TITLE
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.catalog import DOMAIN_DESCRIPTIONS
from agent.runtime.catalog import loader
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore


@pytest.mark.parametrize("role", ["superadmin", "admin", "user", "zako"])
def test_initial_request_exposes_assignable_role_names_only_to_administrators(role):
    menus = []

    class Model:
        max_tokens = 64

        def bind_tools(self, tools, **kwargs):
            menus.extend(convert_to_openai_tool(tool)["function"] for tool in tools)
            return self

        def stream(self, prompt, **kwargs):
            yield AIMessageChunk(content="你好。")

    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()),
                            model_factory=lambda **_: Model())
    list(runtime.stream_events({"messages": [HumanMessage(content="你好")]},
                               config(principal=Principal(7, role, SOURCE_ASSERTION))))
    loaders = [item for item in menus if item["name"] == "load_admin_account_actions_info_tools"]
    if role in {"admin", "superadmin"}:
        assert len(loaders) == 1
        assert all(term in loaders[0]["description"] for term in
                   ("admin=管理员", "user=普通用户", "zako=杂鱼"))
        assert "superadmin=" not in loaders[0]["description"]
        assert not any(item["name"] == "prepare_account_role" for item in menus)
    else:
        assert not loaders


def test_notice_discovery_uses_the_business_default_title():
    tool = loader("admin_notices", DOMAIN_DESCRIPTIONS["admin_notices"])
    description = tool["function"]["description"]
    assert DEFAULT_TITLE in description
