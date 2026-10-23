"""Native, reversible browser commands using the existing renderer protocol."""

import time
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictStr

from agent import action_text, authz
from agent.runtime.read_tools import scoped_reads_enabled
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


def _outcome(reply, receipts):
    return reply, {"kind": "ok", "receipts": receipts}


@tool(response_format="content_and_artifact")
def navigate_page(path: Path, config: RunnableConfig) -> object:
    """Open a site path explicitly requested by the user. Use get_site_map for pages,
    list_categories for category names, or a verified article link. Recommendations
    should be links in the reply. Sends a browser command; no confirmation card.
    """
    if not authorized(config, "navigate_page"):
        return "当前身份无权操作页面。", {"kind": "unavailable"}
    if not site.nav_path_valid(path):
        raise ValueError("Invalid site path; read get_site_map or the source link first")
    url = site.SITE_BASE.rstrip("/") + path
    command = {"kind": "navigate", "url": url, "mode": "direct"}
    receipt = _receipt("navigate", "navigate_to", {"path": path, "confirm": False}, command)
    reply = "已发送跳转指令：" + url + "；浏览器尚未回报完成。"
    if site.nav_pure_path(path) in site._NAV_WHOLE_PAGE_PATHS:
        reply += "本条回复结束后才会跳转。"
    return _outcome(reply, [receipt])


@tool(response_format="content_and_artifact")
def set_page_appearance(
    config: RunnableConfig,
    dark_mode: Mode | None = None,
    sakura: Mode | None = None,
    rain: Mode | None = None,
    snow: Mode | None = None,
) -> object:
    """Set the user's page theme or effects when requested. Pass on/off only for
    requested settings; omit the rest. A switch such as rain to snow sets rain=off,
    snow=on together. Commands set absolute states, so repeats do not toggle them.
    """
    effects = {
        name: mode
        for name, mode in (("sakura", sakura), ("rain", rain), ("snow", snow))
        if mode is not None
    }
    if not authorized(config, "set_page_appearance"):
        return "当前身份无权操作页面。", {"kind": "unavailable"}
    if dark_mode is None and not effects:
        raise ValueError("Specify at least one requested page setting")
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
    actions = "；".join(row["action"] for row in receipts)
    reply = f"已发送显示设置：{actions}；浏览器尚未回报完成。"
    return _outcome(reply, receipts)


def tools(config):
    if not available(config):
        return {}
    return {item.name: item for item in (navigate_page, set_page_appearance)}
