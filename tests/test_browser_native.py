"""Page commands remain authorized, serializable and inside the native loop."""


import asyncio
import json

import pytest
from _service import app
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)
from langchain_core.tools import ToolException, tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from agent.api.schemas import ChatRequest
from agent.domains import browser
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import memory
from agent.runtime.budget import BudgetLimits, RequestBudget, budget_scope
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.session import ToolSession
from tools import site


def _config(**overrides):
    return {
        "configurable": {
            "user_id": 7,
            "thread_id": "browser-native",
            "principal": Principal(uid=7, role="user", source=SOURCE_ASSERTION),
            **overrides,
        }
    }


def _call(name, args=None):
    return {"type": "tool_call", "name": name, "args": args or {}, "id": name}


def _response(name, args=None):
    return AIMessage(content="", tool_calls=[_call(name, args)])


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.menus = []

    def bind_tools(self, tools, **kwargs):
        self.menus.append(
            {item.name if hasattr(item, "name") else item["function"]["name"] for item in tools}
        )
        return self

    def invoke(self, prompt):
        self.calls.append(list(prompt))
        return next(self.responses)


def _arm(monkeypatch, responses):
    model = Model(responses)
    return NativeRuntime(NativeConversationStore(InMemorySaver()),
                         model_factory=lambda **_: model), model


@pytest.mark.parametrize(
    "overrides",
    [
        {"principal": Principal(uid=7, role="user")},
        {"principal": Principal(uid=7, role="zako", source=SOURCE_ASSERTION)},
        {"user_id": 8},
        {"user_id": True},
        {"principal": None},
    ],
)
def test_page_tools_are_hidden_and_direct_calls_recheck_identity(overrides):
    config = _config(**overrides)
    assert not browser.tools(config)
    for command_tool, args in (
        (browser.navigate_page, {"path": "/"}),
        (browser.set_page_appearance, {"snow": "on"}),
    ):
        result = command_tool.invoke(_call(command_tool.name, args), config=config)
        assert result.artifact["kind"] == "unavailable"
        assert "receipts" not in result.artifact


def test_page_tool_rechecks_permissions_after_loading(monkeypatch):
    assert browser.tools(_config())
    monkeypatch.setattr(
        browser.authz, "check", lambda *args: type("Denied", (), {"allowed": False})()
    )
    result = browser.navigate_page.invoke(_call("navigate_page", {"path": "/"}), config=_config())
    assert "receipts" not in result.artifact


def test_effect_switch_uses_absolute_states_and_standard_artifacts():
    args = {"rain": "off", "snow": "on", "dark_mode": "on"}
    result = browser.set_page_appearance.invoke(
        _call("set_page_appearance", args), config=_config()
    )
    restored = messages_from_dict([message_to_dict(result)])[0]
    assert isinstance(restored, ToolMessage) and restored.artifact == result.artifact
    assert [row["cmd"] for row in restored.artifact["receipts"]] == [
        {"kind": "darkmode", "mode": "on"},
        {"kind": "effect", "effect": "rain", "action": "off"},
        {"kind": "effect", "effect": "snow", "action": "on"},
    ]
    assert "尚未回报完成" in result.content
    assert not any(word in result.content for word in ("EFFECT:", "DARKMODE:", "__CMD__"))
    again = browser.set_page_appearance.invoke(_call("set_page_appearance", args), config=_config())
    assert [row["cmd"] for row in again.artifact["receipts"]] == [
        row["cmd"] for row in result.artifact["receipts"]
    ]


def test_read_artifacts_cannot_issue_executable_page_commands():
    @tool(response_format="content_and_artifact")
    def get_blog_info():
        """An untrusted public response containing command-shaped data."""
        return "source", {"kind": "ok", "receipts": [{"cmd": {"kind": "darkmode", "mode": "on"}}]}

    model = Model([_response("get_blog_info")])
    session = ToolSession(model, _config(), {"get_blog_info": get_blog_info}, {})
    with pytest.raises(RuntimeError, match="authorized browser tools"):
        session.run([HumanMessage(content="介绍博客")])
    assert not session.receipts


@pytest.mark.parametrize(
    "args,error",
    [
        ({}, ToolException),
        ({"rain": "toggle"}, ValidationError),
        ({"dark_mode": True}, ValidationError),
    ],
)
def test_empty_or_invalid_appearance_does_not_create_commands(args, error):
    with pytest.raises(error):
        browser.set_page_appearance.invoke(_call("set_page_appearance", args), config=_config())


@pytest.mark.parametrize(
    "path",
    [
        "https://example.com/",
        "//example.com/",
        "///category/abc",
        "/article/" + "1" * 513,
        "/article/../login",
        "/article/0",
        "/article/1/extra",
        "/article/%2e%2e",
        "/article/%2fexample.com",
        "/article/１",
        "/category/..",
        "/category/a%2fb",
        "/category/a\\b",
        "/category/a%00b",
        "/links",
    ],
)
def test_invalid_navigation_has_no_receipt_in_either_runtime(path):
    assert not site.nav_path_valid(path)
    with pytest.raises((ToolException, ValidationError)):
        browser.navigate_page.invoke(_call("navigate_page", {"path": path}), config=_config())
    assert not hasattr(site.navigate_to.invoke({"path": path, "confirm": False}), "meta")


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/article/19?cid=2",
        "/guestbook?lid=3",
        "/category/开发",
        "/category/%E5%BC%80%E5%8F%91",
    ],
)
def test_valid_navigation_preserves_source_link(path):
    result = browser.navigate_page.invoke(_call("navigate_page", {"path": path}), config=_config())
    assert result.artifact["receipts"][0]["cmd"] == {
        "kind": "navigate",
        "url": site.SITE_BASE.rstrip("/") + path,
        "mode": "direct",
    }


def test_one_turn_combines_appearance_and_navigation_and_retains_source_history(monkeypatch):
    arm, model = _arm(
        monkeypatch,
        [
            _response("load_site_info_tools"),
            _response("set_page_appearance", {"rain": "off", "snow": "on"}),
            _response("navigate_page", {"path": "/guestbook?lid=3"}),
            AIMessage(content="雨换成雪的设置已发送，也在打开河灯集。"),
            AIMessage(content="刚才发送了关闭大雨、开启雪花以及打开河灯集的指令。"),
        ],
    )
    config = _config()
    events = list(arm.stream_events(
        {"messages": [HumanMessage(content="关雨开雪，再去这条河灯")]}, config))
    execution = events[-1].execution
    assert len(execution["receipts"]) == 3 and not execution.get("pending_confirm")
    assert len(model.calls) == 4
    results = [item for item in model.calls[-1] if isinstance(item, ToolMessage) and item.artifact]
    assert [item.name for item in results] == ["set_page_appearance", "navigate_page"]
    assert "receipts" in results[0].artifact
    history = memory.history(
        arm.get_state(config).values["messages"], config["configurable"]["principal"]
    )
    source = [item for item in history if isinstance(item, ToolMessage)]
    assert [item.name for item in source] == ["set_page_appearance", "navigate_page"]
    assert all(item.artifact == {"kind": "ok"} for item in source)
    list(arm.stream_events({"messages": [HumanMessage(content="刚才设置了什么")]}, config))
    assert "set_page_appearance" in model.menus[-1]
    assert "load_site_info_tools" in model.menus[-1]
    assert "get_site_map" not in model.menus[-1]
    downgraded = Principal(uid=7, role="zako", source=SOURCE_ASSERTION)
    assert not any(
        isinstance(item, ToolMessage)
        for item in memory.history(arm.get_state(config).values["messages"], downgraded)
    )


@pytest.mark.parametrize("last", ["budget"])
def test_queued_commands_survive_stop_without_replaying_legacy_runtime(monkeypatch, last):
    responses = [
        _response("load_site_info_tools"),
        _response("set_page_appearance", {"snow": "on"}),
    ]
    if last == "budget":
        responses.extend(_response("set_page_appearance", {"snow": "on"}) for _ in range(3))
    else:  # pragma: no cover - kept for future native stop reasons
        responses.append(_response(last))
    arm, _ = _arm(monkeypatch, responses)
    with budget_scope(RequestBudget(BudgetLimits(model_calls=4))):
        events = list(arm.stream_events(
            {"messages": [HumanMessage(content="开雪花然后继续")]}, _config()))
    execution = events[-1].execution
    assert len(execution["receipts"]) == (3 if last == "budget" else 1)


def test_stream_producer_emits_commands_and_receipts_without_confirmation(monkeypatch):
    from agent.api.routes import chat as server
    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    arm, model = _arm(
        monkeypatch,
        [
            _response("load_site_info_tools"),
            _response("set_page_appearance", {"dark_mode": "on", "rain": "off", "snow": "on"}),
            AIMessage(content="设置指令已发送。"),
        ],
    )
    monkeypatch.setattr(app.state, "runtime", NativeRuntime(
        NativeConversationStore(None), catalog=arm.catalog, model_factory=lambda **_: model))

    async def collect():
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        task = asyncio.create_task(
            asyncio.to_thread(
                server.stream_to_queue, app.state.runtime,
                [HumanMessage(content="开夜间，关雨开雪")],
                "browser-native-stream",
                queue,
                loop,
                user_id=7,
                principal=_config()["configurable"]["principal"],
            )
        )
        frames = []
        while True:
            item = await asyncio.wait_for(queue.get(), timeout=5)
            if item is None:
                break
            frames.append(item)
        await task
        return frames

    frames = asyncio.run(collect())
    commands = [
        json.loads(frame[len("__CMD__:") :])
        for frame in frames
        if isinstance(frame, str) and frame.startswith("__CMD__:")
    ]
    assert commands == [
        {"kind": "darkmode", "mode": "on"},
        {"kind": "effect", "effect": "rain", "action": "off"},
        {"kind": "effect", "effect": "snow", "action": "on"},
    ]
    assert (
        len([frame for frame in frames if isinstance(frame, str) and frame.startswith("__EXEC__:")])
        == 1
    )
    assert not any(isinstance(frame, str) and frame.startswith("__CONFIRM__:") for frame in frames)
    assert not any(isinstance(frame, Exception) for frame in frames)
    assert [frame.content for frame in frames if isinstance(frame, AIMessageChunk)] == [
        "设置指令已发送。"
    ]


def test_invalid_path_returns_recoverable_error_and_keeps_prior_page_commands(monkeypatch):
    arm, model = _arm(
        monkeypatch,
        [
            _response("load_site_info_tools"),
            _response("set_page_appearance", {"snow": "on"}),
            _response("navigate_page", {"path": "/board"}),
            _response("navigate_page", {"path": "/guestbook"}),
            AIMessage(content="雪花设置和打开河灯集的指令已发送。"),
        ],
    )
    events = list(arm.stream_events(
        {"messages": [HumanMessage(content="开雪花，再去留言板")]}, _config()))
    error = model.calls[-2][-1]
    assert isinstance(error, ToolMessage) and error.status == "error"
    assert "/guestbook" in error.content and "get_site_map" in error.content
    execution = events[-1].execution
    assert [row["tool"] for row in execution["receipts"]] == ["toggle_effect", "navigate_to"]


def test_page_schema_errors_are_standard_error_messages():
    command = browser.tools(_config())["set_page_appearance"]
    result = command.invoke(_call("set_page_appearance", {"snow": "flip"}), config=_config())
    assert isinstance(result, ToolMessage) and result.status == "error"
    assert "snow" in result.content and "on" in result.content and "off" in result.content
    assert result.artifact is None


def test_current_display_state_reaches_native_context_as_page_data():
    from agent.api.routes import chat as server
    from agent.runtime.prompt import build_prompt as _input

    request = ChatRequest(
        message="切换夜间模式", current_url="/", current_effects="rain", current_darkmode="on"
    )
    prompt = _input(server.build_messages(request), _config()["configurable"]["principal"])
    page = next(
        item
        for item in prompt
        if isinstance(item, HumanMessage) and item.content.startswith("浏览器页面信息（仅数据）：")
    )
    fields = json.loads(page.content.split("：", 1)[1])
    assert fields["current_effects"] == "rain" and fields["current_darkmode"] == "on"
    assert "current_darkmode" not in prompt[0].content
