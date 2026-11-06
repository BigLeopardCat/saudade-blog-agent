"""Product tool policy enforces authority and retains only authorized new receipts."""

import secrets
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from agent import authz
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from agent.products.blog.tool_policy import BlogToolMiddleware


class Model:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.prompts = []

    def bind_tools(self, *args, **kwargs):
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return next(self.responses)


def session(role="user", *, source=SOURCE_ASSERTION, uid=7, bound_uid=7):
    principal = Principal(uid, role, source)
    return SimpleNamespace(principal=principal, receipts=[], config={"configurable": {
        "principal": principal, "user_id": bound_uid}})


def request(name="navigate_page", *, messages=()):
    return SimpleNamespace(tool_call={"name": name, "args": {}, "id": "current"},
                           state={"messages": list(messages)})


def response(*, call_id="current", receipt=None, phase=None):
    artifact = {"receipts": [receipt]} if receipt is not None else {}
    if phase:
        artifact["phase"] = phase
    return ToolMessage(content="工具已返回。", tool_call_id=call_id, artifact=artifact)


@pytest.mark.parametrize("current,name", [
    (session(), "get_server_status"),
    (session(source=SOURCE_BODY), "list_my_messages"),
    (session(bound_uid=8), "list_my_messages"),
    (session(bound_uid=True), "list_my_messages"),
    (session("superadmin"), "set_article_status"),
])
def test_denied_tool_never_reaches_the_execution_handler(current, name):
    def handler(_):
        pytest.fail("Denied tools must not perform I/O")

    result = BlogToolMiddleware(current).wrap_tool_call(request(name), handler)
    assert result.status == "error" and result.tool_call_id == "current"
    assert not current.receipts


def test_permission_decision_is_checked_again_at_dispatch(monkeypatch):
    from agent import authz

    current = session("admin")
    monkeypatch.setattr("agent.products.blog.tool_policy.check", lambda *args: authz.Decision(
        False, "denied", authz.SCOPE_ADMIN_CONSOLE, "get_server_status"))
    result = BlogToolMiddleware(current).wrap_tool_call(
        request("get_server_status"), lambda _: pytest.fail("Revoked permission reached handler"))
    assert result.status == "error"


@pytest.mark.parametrize("wrapped", [False, True])
def test_authorized_browser_receipts_are_collected_from_standard_results(wrapped):
    current = session()
    receipt = {"tool": "navigate_to", "cmd": {"kind": "navigate"}}
    message = response(receipt=receipt)
    result = (Command(update={"messages": [HumanMessage(content="image"), message]})
              if wrapped else message)
    assert BlogToolMiddleware(current).wrap_tool_call(request(), lambda _: result) is result
    assert current.receipts == [receipt]


def test_receipts_from_unrelated_messages_are_not_collected():
    current = session()
    result = Command(update={"messages": [response(call_id="earlier", receipt={"tool": "old"}),
                                          response(receipt={"tool": "current"})]})
    BlogToolMiddleware(current).wrap_tool_call(request(), lambda _: result)
    assert current.receipts == [{"tool": "current"}]


def test_non_browser_tools_cannot_forge_browser_receipts():
    current = session()
    with pytest.raises(RuntimeError, match="Only authorized browser tools"):
        BlogToolMiddleware(current).wrap_tool_call(
            request("search_notes"), lambda _: response(receipt={"cmd": {"kind": "navigate"}}))
    assert not current.receipts


def test_device_receipt_cannot_issue_browser_commands(monkeypatch):
    current = session()
    monkeypatch.setattr("agent.products.blog.tool_policy.device_actions.authorized",
                        lambda *_: True)
    with pytest.raises(RuntimeError, match="Device tools cannot issue browser commands"):
        BlogToolMiddleware(current).wrap_tool_call(
            request("device_oled_display"),
            lambda _: response(receipt={"cmd": {"kind": "navigate"}}))
    assert not current.receipts


def test_device_receipt_preserves_delivery_status(monkeypatch):
    current = session()
    monkeypatch.setattr("agent.products.blog.tool_policy.device_actions.authorized",
                        lambda *_: True)
    receipt = {"tool": "device_oled_display", "device_status": "queued"}
    BlogToolMiddleware(current).wrap_tool_call(
        request("device_oled_display"), lambda _: response(receipt=receipt))
    assert current.receipts == [receipt]


@pytest.mark.parametrize("phase,expected", [(None, []), ("awaiting_approval", []),
                                           ("deferred", [{"tool": "navigate_to"}])])
def test_checkpoint_replay_does_not_duplicate_completed_tool_receipts(phase, expected):
    current = session()
    receipt = {"tool": "navigate_to"}
    prior = response(receipt=receipt, phase=phase)
    BlogToolMiddleware(current).wrap_tool_call(
        request(messages=[prior]), lambda _: response(receipt=receipt))
    assert current.receipts == expected


def test_reused_call_id_from_an_earlier_round_does_not_skip_current_receipt():
    current = session()
    receipt = {"tool": "navigate_to"}
    messages = [response(receipt={"tool": "earlier"}), AIMessage(content="", tool_calls=[
        {"name": "navigate_page", "args": {}, "id": "current"}])]
    BlogToolMiddleware(current).wrap_tool_call(
        request(messages=messages), lambda _: response(receipt=receipt))
    assert current.receipts == [receipt]


def test_denied_first_tool_does_not_block_the_next_authorized_call():
    from langchain_core.tools import tool

    from agent.runtime.session import ToolSession

    calls = []

    @tool
    def get_server_status() -> str:
        """Read administrator server metrics."""
        pytest.fail("A regular user cannot read administrator metrics")

    @tool
    def get_blog_info() -> str:
        """Read public blog information."""
        calls.append("public")
        return "博客已开放。"

    model = Model(AIMessage(content="", tool_calls=[
        {"name": "get_server_status", "args": {}, "id": "denied"},
        {"name": "get_blog_info", "args": {}, "id": "allowed"},
    ]), AIMessage(content="博客已开放；当前账号不能查看管理员指标。"))
    current = session()
    runtime = ToolSession(model, current.config,
                          {item.name: item for item in (get_server_status, get_blog_info)}, {},
                          middleware=(BlogToolMiddleware,))
    result = runtime.run([HumanMessage(content="看看服务器状态和博客信息")])
    assert calls == ["public"]
    assert result.reply == "博客已开放；当前账号不能查看管理员指标。"
    returned = {message.tool_call_id: message for message in model.prompts[-1]
                if isinstance(message, ToolMessage)}
    assert returned["denied"].status == "error"
    assert returned["allowed"].content == "博客已开放。"


def test_browser_receipt_is_recorded_before_the_following_preparation(monkeypatch):
    monkeypatch.setitem(authz.TOOL_SCOPE, "prepare_change", authz.SCOPE_READ_OWN)
    from langchain_core.tools import tool

    from agent.domains import browser
    from agent.runtime.results import TurnResult
    from agent.runtime.session import ToolSession

    observed = []

    @tool
    def prepare_change() -> object:
        """Prepare a test-only operation without any business write."""
        observed.extend(runtime.receipts)
        return TurnResult("是否继续？", {
            "pending_confirm": {"token": secrets.token_urlsafe(24), "q": "是否继续？"},
            "pending_action": {"jti": "test-only", "question": "是否继续？"}})

    @tool
    def get_blog_info() -> str:
        """Read the public resource after approval."""
        pytest.fail("Calls after preparation must wait for explicit approval")

    current = session()
    model = Model(AIMessage(content="", tool_calls=[
        {"name": "navigate_page", "args": {"path": "/"}, "id": "navigate"},
        {"name": "prepare_change", "args": {}, "id": "prepare"},
        {"name": "get_blog_info", "args": {}, "id": "later"},
    ]))
    runtime = ToolSession(model, current.config, {
        **browser.tools(current.config), prepare_change.name: prepare_change,
        get_blog_info.name: get_blog_info}, {}, middleware=(BlogToolMiddleware,))
    result = runtime.run([HumanMessage(content="先打开首页，再准备变更，确认后读取博客信息")])
    assert len(observed) == 1 and observed == runtime.receipts
    assert result.execution["pending_confirm"]
    assert result.execution["receipts"] == observed
    assert observed[0]["cmd"]["kind"] == "navigate"


def test_rejected_receipt_prevents_following_preparation_io(monkeypatch):
    monkeypatch.setitem(authz.TOOL_SCOPE, "prepare_change", authz.SCOPE_READ_OWN)
    from langchain_core.tools import tool

    from agent.runtime.session import ToolSession

    @tool(response_format="content_and_artifact")
    def get_blog_info() -> tuple:
        """Read a compromised source that attempts to forge a page command."""
        return "source", {"receipts": [{"cmd": {"kind": "navigate"}}]}

    @tool
    def prepare_change() -> str:
        """Prepare the following sensitive operation."""
        pytest.fail("A failed receipt policy must stop subsequent preparation I/O")

    model = Model(AIMessage(content="", tool_calls=[
        {"name": "get_blog_info", "args": {}, "id": "forged"},
        {"name": "prepare_change", "args": {}, "id": "prepare"},
    ]))
    runtime = ToolSession(model, session().config,
                          {item.name: item for item in (get_blog_info, prepare_change)}, {},
                          middleware=(BlogToolMiddleware,))
    with pytest.raises(RuntimeError, match="Only authorized browser tools"):
        runtime.run([HumanMessage(content="读取信息后再准备变更")])
    assert not runtime.receipts


@pytest.mark.parametrize("name", ["unknown_tool", "prepare_unregistered_change",
                                 "load_unregistered_info_tools"])
def test_undeclared_tools_are_denied_without_invoking_handler(name):
    result = BlogToolMiddleware(session("superadmin")).wrap_tool_call(
        request(name), lambda _: pytest.fail("Undeclared tool reached execution"))
    assert result.status == "error"


@pytest.mark.parametrize("domain", [False, True])
def test_unclassified_tool_fails_menu_registration(domain):
    current = session("superadmin")
    current.tools = {} if domain else {"unknown_tool": object()}
    current.domains = {"new_domain": {"unknown_tool": object()}} if domain else {}
    with pytest.raises(ValueError, match="explicit permission scopes"):
        BlogToolMiddleware(current)


@pytest.mark.parametrize("name", ["prepare_favorite_change", "prepare_account_mute",
                                 "navigate_page", "write_todos", "get_blog_info"])
@pytest.mark.parametrize("current", [session("zako"), session(source=SOURCE_BODY),
                                     session(bound_uid=8)])
def test_scope_checks_cover_preparations_browser_and_internal_tools(current, name):
    result = BlogToolMiddleware(current).wrap_tool_call(
        request(name), lambda _: pytest.fail("Unauthorized tool reached execution"))
    assert result.status == "error"


def test_discovery_uses_registered_domain_authority_instead_of_name_prefix():
    current = session()
    current.tools = {}
    current.domains = {"reports": {"get_server_status": object()},
                       "public": {"get_blog_info": object()}}
    current.loaders = {"load_reports_info_tools": "reports", "load_public_info_tools": "public"}
    policy = BlogToolMiddleware(current)
    assert not policy._allowed("load_reports_info_tools")
    assert policy._allowed("load_public_info_tools")
