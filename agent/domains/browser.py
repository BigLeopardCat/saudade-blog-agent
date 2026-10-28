"""Native, reversible browser commands using the existing renderer protocol."""

import hashlib
import time
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import ToolException, tool
from langgraph.types import interrupt
from pydantic import Field, StrictStr

from agent import action_text, authz
from agent.products.blog.read_tools import scoped_reads_enabled
from agent.runtime.progress import current_emitter
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
    """Open a verified site path when the user asks to go there, including an article
    chosen from a recommendation or resolved from conversation context. If asked
    to recommend and open, choose a source then navigate; recommending alone does
    not navigate. Use get_site_map for pages, list_categories for category names,
    or a source article link. Returns a queued browser command, not a browser
    completion acknowledgement. No confirmation card.
    """
    if not authorized(config, "navigate_page"):
        return "当前身份无权操作页面。", {"kind": "unavailable"}
    if not site.nav_path_valid(path):
        allowed = ", ".join(sorted(site._NAV_EXACT_PATHS))
        raise ToolException(
            f"Invalid site path. Exact paths: {allowed}. Dynamic paths: "
            "/article/<positive integer>, /category/<name>. "
            "Read get_site_map for page names or copy a source link."
        )
    url = site.SITE_BASE.rstrip("/") + path
    command = {"kind": "navigate", "url": url, "mode": "direct"}
    cfg = (config or {}).get("configurable") or {}
    if cfg.get("browser_feedback") and cfg.get("browser_task"):
        task_id = cfg["browser_task"]
        command_id = hashlib.sha256((task_id + path).encode()).hexdigest()[:24]
        progress = current_emitter(config)
        result = interrupt({"kind": "client_action",
            "progress_keys": progress.emitted_keys if progress is not None else [], "command": {
            **command, "path": path, "task_id": task_id, "command_id": command_id,
            "timeout_ms": 6000, "expires_at": time.time() + 60}})
        if result.get("command_id") != command_id:
            raise ToolException("Browser result does not match the pending navigation")
        status = result.get("status")
        completed = status == "completed" and result.get("path") == path
        reply = ("浏览器已进入：" + url if completed else
                 "页面跳转超时，未确认进入目标页面：" + url if status == "timeout" else
                 "页面跳转未完成：" + url)
        receipt = _receipt("navigate", "navigate_to", {"path": path, "confirm": False}, command)
        receipt.update(result=reply, browser_status="completed" if completed else status)
        return _outcome(reply, [receipt])
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
    actions = "；".join(row["action"] for row in receipts)
    reply = f"已发送显示设置：{actions}；浏览器尚未回报完成。"
    return _outcome(reply, receipts)


def tools(config):
    if not available(config):
        return {}
    pages = "; ".join(label + "=" + path for path, label in site._NAV_LABELS.items())
    navigation = navigate_page.model_copy(update={"description":
        "Open a site page immediately when requested. Known pages: " + pages
        + ". Article: /article/<id>; category: /category/<name>. Use the source article link "
        "for a chosen recommendation. No site-map lookup is needed for the known pages. "
        "With browser feedback, execution pauses durably until the client reports completion "
        "or timeout, then continues the same task. No confirmation card."})
    return {
        item.name: item.model_copy(
            update={"handle_tool_error": True, "handle_validation_error": str}
        )
        for item in (navigation, set_page_appearance)
    }
