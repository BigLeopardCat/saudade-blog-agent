"""Authorized browser commands and observed completion facts."""

import json
import time
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, ToolException, tool
from pydantic import Field, StrictStr

from agent import action_text, authz
from agent.runtime.access import scoped_reads_enabled
from agent.runtime.client_actions import await_result as _await_browser
from tools import site

Path = Annotated[StrictStr, Field(min_length=1, max_length=512)]
Mode = Literal["on", "off"]
_PERMISSIONS = {
    "navigate_page": ("navigate_to",),
    "set_page_appearance": ("toggle_dark_mode", "toggle_effect"),
}


def available(config):
    cfg = (config or {}).get("configurable") or {}
    return type(cfg.get("user_id")) is int and scoped_reads_enabled(
        cfg.get("principal"), authz.SCOPE_WRITE_PAGE, cfg["user_id"]
    )


def history_allowed(principal, name):
    names = _PERMISSIONS.get(name)
    return (
        names is not None
        and scoped_reads_enabled(principal, authz.SCOPE_WRITE_PAGE)
        and all(authz.check(principal, item).allowed for item in names)
    )


def authorized(config, name):
    principal = ((config or {}).get("configurable") or {}).get("principal")
    return available(config) and history_allowed(principal, name)


def console_access(config):
    cfg = (config or {}).get("configurable") or {}
    return scoped_reads_enabled(cfg.get("principal"), authz.SCOPE_ADMIN_CONSOLE, cfg.get("user_id"))


def accessible_paths(config):
    return site.navigation_paths(config)


def _receipt(workflow, name, args, command):
    receipt = {
        "skill": workflow,
        "tool": name,
        "args": args,
        "cmd": command,
        "result": "页面指令已发送，浏览器完成状态尚未回传。",
        "ts": time.time(),
    }
    receipt["action"] = action_text.receipt_action(name, args, receipt)
    return receipt


def _outcome(reply, receipts, *, failed=False):
    return reply, {"kind": "unavailable" if failed else "ok", "receipts": receipts}


def _sent(command, receipts):
    return _outcome(
        json.dumps(
            {
                "status": "sent",
                "completion_confirmed": False,
                "scope": "browser",
                "requested": command,
                "observed": None,
                "result": receipts[0]["result"],
            },
            ensure_ascii=False,
        ),
        receipts,
    )


@tool(response_format="content_and_artifact")
def navigate_page(
    path: Path, config: RunnableConfig, tool_call_id: Annotated[str, InjectedToolCallId]
) -> object:
    """Change the current page when the user requests navigation, including opening
    a chosen recommendation. Listing pages, explaining where something is, and
    recommending articles are read-only tasks. Answer those from the available
    paths or get_site_map without changing the page. Resolve article and category
    paths from source links. No confirmation card. With browser feedback, waits
    for completion or timeout in the same durable task. Status sent means queued;
    only completed proves arrival.
    """
    if not authorized(config, "navigate_page"):
        return "当前身份无权操作页面。", {"kind": "unavailable"}
    if not site.nav_path_valid(path):
        allowed = ", ".join(sorted(accessible_paths(config)))
        raise ToolException(
            f"Invalid site path. Exact paths: {allowed}. Dynamic paths: "
            "/article/<positive integer>, /category/<name>. "
            "Read get_site_map for page names or copy a source link."
        )
    if site.nav_pure_path(path).startswith("/dashboard") and not console_access(config):
        raise ToolException("当前账号没有后台管理权限，无法进入后台管理页面。")
    from tools.base import SITE_BASE

    url = SITE_BASE.rstrip("/") + path
    command = {"kind": "navigate", "url": url, "mode": "direct"}
    cfg = (config or {}).get("configurable") or {}
    if cfg.get("browser_feedback") and cfg.get("browser_task"):
        result = _await_browser({**command, "path": path, "request_id": tool_call_id}, config)
        status = result["status"]
        completed = status == "completed"
        reply = (
            "浏览器已进入：" + url
            if completed
            else "页面跳转超时，未确认进入目标页面：" + url
            if status == "timeout"
            else "页面跳转未完成：" + url
        )
        receipt = _receipt("navigate", "navigate_to", {"path": path, "confirm": False}, command)
        receipt.update(result=reply, browser_status="completed" if completed else status)
        return _outcome(
            json.dumps(
                {
                    "status": status,
                    "completion_confirmed": completed,
                    "scope": "browser_navigation",
                    "requested": path,
                    "observed": result.get("path"),
                    "result": reply,
                },
                ensure_ascii=False,
            ),
            [receipt],
            failed=status != "completed",
        )
    receipt = _receipt("navigate", "navigate_to", {"path": path, "confirm": False}, command)
    return _sent(command, [receipt])


@tool(response_format="content_and_artifact")
def set_page_appearance(
    config: RunnableConfig,
    tool_call_id: Annotated[str, InjectedToolCallId],
    dark_mode: Mode | None = None,
    sakura: Mode | None = None,
    rain: Mode | None = None,
    snow: Mode | None = None,
) -> object:
    """Set the user's page theme or effects when requested. Pass on/off only for
    requested settings; omit the rest. A switch such as rain to snow sets rain=off,
    snow=on together. Commands set absolute states, so repeats do not toggle them.
    With browser feedback, waits for observed settings before reporting completion.
    Status sent means queued only; only completed proves application.
    """
    effects = {
        name: mode
        for name, mode in (("sakura", sakura), ("rain", rain), ("snow", snow))
        if mode is not None
    }
    if not authorized(config, "set_page_appearance"):
        return "当前身份无权操作页面。", {"kind": "unavailable"}
    if dark_mode is None and not effects:
        raise ToolException(
            "Specify at least one requested page setting: dark_mode/sakura/rain/snow"
        )
    receipts = []
    if dark_mode is not None:
        receipts.append(
            _receipt(
                "darkmode",
                "toggle_dark_mode",
                {"mode": dark_mode},
                {"kind": "darkmode", "mode": dark_mode},
            )
        )
    for effect, mode in effects.items():
        receipts.append(
            _receipt(
                "effect",
                "toggle_effect",
                {"effect": effect, "action": mode},
                {"kind": "effect", "effect": effect, "action": mode},
            )
        )
    cfg = (config or {}).get("configurable") or {}
    if cfg.get("browser_feedback") and cfg.get("browser_task"):
        settings = {**effects, **({"dark_mode": dark_mode} if dark_mode is not None else {})}
        result = _await_browser(
            {"kind": "appearance", "settings": settings, "request_id": tool_call_id}, config
        )
        status = result["status"]
        reply = json.dumps(
            {
                "status": status,
                "completion_confirmed": status == "completed",
                "scope": "browser_appearance",
                "requested": settings,
                "observed": result.get("settings"),
            },
            ensure_ascii=False,
        )
        for receipt in receipts:
            receipt.update(result=reply, browser_status=status)
        return _outcome(reply, receipts, failed=status != "completed")
    settings = {**effects, **({"dark_mode": dark_mode} if dark_mode is not None else {})}
    return _sent({"kind": "appearance", "settings": settings}, receipts)


def tools(config):
    if not available(config):
        return {}
    allowed = accessible_paths(config)
    pages = "; ".join(
        label + "=" + path for path, label in site._NAV_LABELS.items() if path in allowed
    )
    navigation = navigate_page.model_copy(
        update={
            "description": navigate_page.description + " Pages available to this caller: "
            + pages
            + ". Article: /article/<id>; category: /category/<name>. Use the source article link "
            "for a chosen recommendation. Known paths need no additional lookup. "
            "This allowlist defines supported commands, not whether other URLs exist."
        }
    )
    return {
        item.name: item.model_copy(
            update={"handle_tool_error": True, "handle_validation_error": str}
        )
        for item in (navigation, set_page_appearance)
    }
