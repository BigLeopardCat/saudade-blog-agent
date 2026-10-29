"""Administrator reads require verified, bound identity before any local or HTTP I/O."""

import secrets
from types import SimpleNamespace

import httpx
import jwt
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent import authz
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from agent.products.blog.capabilities import ToolCatalog
from agent.products.blog.read_tools import ADMIN_READ_TOOLS, MODERATION_READ_TOOLS, admin_tools
from agent.products.blog.tool_policy import BlogToolMiddleware
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.session import ToolSession
from tools import base


class Model:
    def __init__(self, replies=()):
        self.replies = iter(replies)
        self.menus = []

    def bind_tools(self, tools, **kwargs):
        from _budget_model import schemas
        self.kwargs = {"tools": schemas(tools)}
        self.menus.append(list(tools))
        return self

    def invoke(self, prompt):
        return next(self.replies)


class LocalTool:
    name = "get_server_status"

    def __init__(self):
        self.calls = []

    def invoke(self, args, config=None):
        self.calls.append(config)
        return base.ok("CPU 17%，内存 36%。")


def config(role="superadmin", source=SOURCE_ASSERTION, uid=7, **extra):
    return {"configurable": {"principal": Principal(uid, role, source), **extra}}


def invoke_authorized(session, name, args):
    request = SimpleNamespace(tool_call={"name": name, "args": args, "id": "read"},
                              state={"messages": []})
    return BlogToolMiddleware(session).wrap_tool_call(
        request, lambda current: session.tools[name].invoke(args, config=session.config))


@pytest.mark.parametrize("role,source,uid,visible", [
    ("admin", SOURCE_ASSERTION, 7, True), ("superadmin", SOURCE_ASSERTION, 7, True),
    ("user", SOURCE_ASSERTION, 7, False), ("zako", SOURCE_ASSERTION, 7, False),
    ("invented", SOURCE_ASSERTION, 7, False), (None, SOURCE_ASSERTION, 7, False),
    ("admin", SOURCE_BODY, 7, False), ("admin", SOURCE_ASSERTION, True, False),
    ("admin", SOURCE_ASSERTION, 0, False),
])
def test_admin_menu_visibility_follows_verified_authority(role, source, uid, visible):
    domains = ToolCatalog().domains(config(role, source, uid), False)
    assert ("admin_reports" in domains) is visible
    assert ("admin_moderation" in domains) is visible
    assert ("admin_work_queue" in domains) is visible
    assert ("admin_catalog" in domains) is visible
    assert ("admin_account_actions" in domains) is visible
    if visible:
        assert set(domains["admin_moderation"]) == MODERATION_READ_TOOLS
        assert set(domains["admin_reports"]) == ADMIN_READ_TOOLS
        assert set(domains["admin_account_actions"]) == {"list_accounts"}


@pytest.mark.parametrize("cfg", [config("user"), config(source=SOURCE_BODY),
                                 config(user_id=8), config(user_id=True)])
def test_private_tool_cannot_run_even_if_accidentally_included_in_menu(cfg, monkeypatch):
    tool = LocalTool()
    session = ToolSession(Model(), cfg, {tool.name: tool}, {})
    assert invoke_authorized(session, tool.name, {}).status == "error"
    assert not tool.calls


def test_permission_is_checked_again_after_domain_discovery(monkeypatch):
    tool = LocalTool()
    session = ToolSession(Model(), config(), {tool.name: tool}, {})
    monkeypatch.setattr("agent.products.blog.tool_policy.check", lambda *args: authz.Decision(
        False, "denied", authz.SCOPE_ADMIN_CONSOLE, tool.name))
    assert invoke_authorized(session, tool.name, {}).status == "error"
    assert not tool.calls


def test_read_loop_never_executes_a_registered_write_tool():
    tool = LocalTool()
    tool.name = "set_article_status"
    session = ToolSession(Model(), config(), {tool.name: tool}, {})
    assert invoke_authorized(session, tool.name, {"note_id": 19, "status": 0}).status == "error"
    assert not tool.calls


def test_native_admin_read_loads_only_requested_domain_and_never_calls_legacy(monkeypatch):
    def call(name):
        return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": name}])

    model = Model([call("load_admin_reports_info_tools"), call("get_server_status"),
                   AIMessage(content="CPU 17%，内存 36%。")])
    tool = LocalTool()
    cfg = config(user_id=7)
    runtime = NativeRuntime(
        NativeConversationStore(None),
        catalog=ToolCatalog(admin_read_tools={tool.name: tool}),
        model_factory=lambda **_: model,
    )
    events = list(runtime.stream_events(
        {"messages": [HumanMessage(content="查看服务器状态")]}, cfg))
    assert len(tool.calls) == 1
    assert all(tool.calls[0]["configurable"][key] == value
               for key, value in cfg["configurable"].items())
    menus = [{item["function"]["name"] for item in menu} for menu in model.menus]
    assert tool.name not in menus[0] and tool.name in menus[1]
    assert not any(name.startswith("prepare_") for name in menus[1])
    assert events[-1].reply == "CPU 17%，内存 36%。"


def test_admin_menu_rejects_write_scope_and_preserves_legacy_descriptions(monkeypatch):
    original = base.get_note_periods.description
    tools = admin_tools()
    assert tools["get_note_periods"].description != original
    assert base.get_note_periods.description == original
    monkeypatch.setitem(authz.TOOL_SCOPE, "get_note_periods", authz.SCOPE_WRITE_CONSOLE)
    with pytest.raises(RuntimeError, match="admin.console reads"):
        admin_tools()


@pytest.mark.parametrize("data", [{"users": []}, {"totalUsers": True}, {"totalUsers": -1}])
def test_missing_or_invalid_statistics_never_become_a_zero_report(monkeypatch, data):
    monkeypatch.setattr(base, "_admin_get", lambda *args: data)
    result = base.get_user_stats.invoke({}, config=config())
    assert result.kind == "unavailable"


@pytest.mark.parametrize("status", [200, 403, 503])
def test_real_statistics_tool_uses_actor_identity_and_preserves_api_failure(monkeypatch, status):
    secret = secrets.token_urlsafe(32)
    data = {"totalUsers": 0, "totalConversations": 0, "totalMessages": 0,
            "totalExecutions": 0, "activeUsers7d": 0, "activeUsers30d": 0,
            "roleCounts": [], "users": []}
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/api/protected/stats/users"
        token = request.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, secret, algorithms=["HS256"], options={"verify_sub": False})
        assert claims["sub"] == 7
        return httpx.Response(status, json={"code": 200, "data": data})

    monkeypatch.setattr(base, "_jwt_secret", lambda: secret)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        session = ToolSession(Model(), config(user_id=7), admin_tools(), {})
        result = invoke_authorized(session, "get_user_stats", {})
    assert len(requests) == 1
    assert result.kind == ("ok" if status == 200 else "unavailable")
    if status == 200:
        assert "用户总数 0" in str(result)
