"""Blog authorization and unknown-tool recovery through actual framework middleware."""

import json

import pytest
from _memory_support import Capture
from _own_support import config
from _tool_catalog import permission_names
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import tool

from agent import authz
from agent.principal import (
    ADMIN_ROLES,
    KNOWN_ROLES,
    ROLE_ADMIN,
    ROLE_SUPERADMIN,
    ROLE_USER,
    ROLE_ZAKO,
    SOURCE_ASSERTION,
    UNKNOWN,
    Principal,
)
from agent.products.blog.tool_policy import BlogToolMiddleware
from agent.runtime.session import ToolSession


def principal(role, uid=7):
    return Principal(uid=uid, role=role, source=SOURCE_ASSERTION)


def test_native_manifest_and_role_scopes_are_closed():
    names = permission_names()
    assert not authz.manifest_gaps(names)
    assert not authz.manifest_stale(names)
    assert set(authz.TOOL_SCOPE.values()) <= authz.ALL_SCOPES
    assert all(scopes <= authz.ALL_SCOPES
               for scopes in (authz.scopes_for(role) for role in KNOWN_ROLES))
    assert ADMIN_ROLES == {ROLE_ADMIN, ROLE_SUPERADMIN}
    assert authz.scopes_for(ROLE_ZAKO) < authz.scopes_for(ROLE_USER)
    assert authz.scopes_for(ROLE_USER) < authz.scopes_for(ROLE_ADMIN)
    assert authz.scopes_for(ROLE_ADMIN) <= authz.scopes_for(ROLE_SUPERADMIN)


def test_native_authorization_fails_closed_and_checks_scope():
    assert not authz.check(UNKNOWN, "list_notes").allowed
    assert authz.check(UNKNOWN, "list_notes").reason == authz.REASON_UNKNOWN_ROLE
    assert not authz.check(None, "list_notes").allowed
    assert not authz.check(principal("invented"), "list_notes").allowed
    assert not authz.check(principal(ROLE_ADMIN), "unknown_tool").allowed
    assert authz.check(principal(ROLE_USER), "list_notes").allowed
    assert not authz.check(principal(ROLE_USER), "list_accounts").allowed
    assert authz.check(principal(ROLE_ADMIN), "list_accounts").allowed
    assert authz.check(principal(ROLE_ADMIN), "freeze_account").allowed
    assert not authz.check(principal(ROLE_USER), "freeze_account").allowed


def test_native_write_classification_is_explicit():
    assert not authz.is_write("get_chat_history")
    assert authz.is_write("navigate_to")
    assert authz.is_write("freeze_account")
    assert authz.required_scope("account_mute") == authz.SCOPE_WRITE_CONSOLE
    assert authz.required_scope("account_unmute") == authz.SCOPE_WRITE_CONSOLE
    assert authz.required_scope("set_account_role") == authz.SCOPE_WRITE_CONSOLE


def test_unknown_name_feedback_does_not_disclose_forbidden_domains():
    @tool
    def get_blog_info():
        """Public fixture, never invoked by this test."""
        raise AssertionError("Unknown name must not execute")

    @tool
    def list_accounts():
        """Private fixture, never invoked by this test."""
        raise AssertionError("Unknown name must not execute")

    model = Capture(AIMessageChunk(content="", tool_calls=[{
        "name": "invented_tool", "args": {}, "id": "unknown"}]),
        AIMessageChunk(content="unavailable"))
    ToolSession(model, config(), {}, {"public": {"get_blog_info": get_blog_info},
                "admin_accounts": {"list_accounts": list_accounts}},
                middleware=(BlogToolMiddleware,)).run([HumanMessage(content="read")])
    result = next(m for m in model.prompts[-1] if isinstance(m, ToolMessage))
    assert json.loads(result.content) == {"error": "unknown_tool", "tool": "invented_tool",
                                         "discovery_tools": ["load_public_info_tools"]}


@pytest.mark.parametrize("name", ["list_accounts", "add_favorite"])
def test_known_forbidden_read_and_unapproved_write_remain_denied(name):
    invoked = []

    @tool(name)
    def forbidden():
        """A known tool whose execution must stay denied."""
        invoked.append(True)
        return "must not execute"

    model = Capture(AIMessageChunk(content="", tool_calls=[{
        "name": name, "args": {}, "id": "denied"}]), AIMessageChunk(content="denied"))
    ToolSession(model, config(), {name: forbidden}, {}, middleware=(BlogToolMiddleware,)).run(
        [HumanMessage(content="execute")])
    result = next(m for m in model.prompts[-1] if isinstance(m, ToolMessage))
    assert result.status == "error" and result.content == "无权读取此数据。"
    assert not invoked
