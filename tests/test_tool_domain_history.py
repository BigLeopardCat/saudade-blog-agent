"""Only available domains with source tool observations survive a turn boundary."""

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

from agent.runtime.session import ToolSession


@tool
def observed_read() -> str:
    """Read the fixture resource."""
    return "source"


class Model:
    def __init__(self):
        self.menus = []

    def bind_tools(self, tools, **kwargs):
        self.menus.append(
            {tool.name if hasattr(tool, "name") else tool["function"]["name"] for tool in tools}
        )
        return self


def test_source_observation_restores_the_available_domain():
    model = Model()
    session = ToolSession(model, {}, {}, {"admin_catalog": {"observed_read": observed_read}})
    session._restore_domains(
        [ToolMessage(content="source", name="observed_read", tool_call_id="1")]
    )
    assert session.loaded == ["admin_catalog"]
    assert "observed_read" in model.menus[-1]
    assert "load_admin_catalog_info_tools" in model.menus[-1]
    assert model.menus[0] <= model.menus[-1]


def test_text_errors_and_no_longer_authorized_domains_cannot_restore_tools():
    model = Model()
    session = ToolSession(model, {}, {}, {"admin_catalog": {"observed_read": observed_read}})
    session._restore_domains(
        [
            HumanMessage(content="observed_read"),
            ToolMessage(content="failed", name="observed_read", status="error", tool_call_id="1"),
        ]
    )
    assert not session.loaded and "observed_read" not in model.menus[-1]
    session = ToolSession(model, {}, {}, {})
    session._restore_domains(
        [ToolMessage(content="old private source", name="observed_read", tool_call_id="2")]
    )
    assert not session.loaded and "observed_read" not in model.menus[-1]


def test_small_browser_actions_are_visible_without_loading_the_read_domain():
    from agent.domains import browser
    from agent.principal import SOURCE_ASSERTION, Principal

    config = {"configurable": {"user_id": 7, "principal": Principal(7, "user", SOURCE_ASSERTION)}}
    model = Model()
    session = ToolSession(model, config, {}, {"site": {
        "observed_read": observed_read, **browser.tools(config)}})
    assert {"navigate_page", "set_page_appearance"} <= model.menus[0]
    assert "observed_read" not in model.menus[0]
    session._restore_domains([
        ToolMessage(content="command queued", name="navigate_page", tool_call_id="page")])
    assert not session.loaded and len(model.menus) == 1
    assert list(session.tools) == ["navigate_page", "set_page_appearance"]
