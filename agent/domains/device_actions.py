"""Thin OLED demo adapter; display is an absolute command, not a toggle."""

import time
import uuid
from typing import Annotated

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import ToolException, tool
from pydantic import Field, StrictStr

from agent import authz
from agent.domains import device_reads
from agent.runtime.read_tools import scoped_reads_enabled
from agent.runtime.turns import check_cancelled
from tools import device

Text = Annotated[StrictStr, Field(min_length=1, max_length=64)]
DeviceId = Annotated[StrictStr, Field(min_length=1, max_length=128)]
NAME = "device_oled_display"


def authorized(config, name=NAME):
    cfg = (config or {}).get("configurable") or {}
    return (
        name == NAME
        and device_reads.enabled()
        and type(cfg.get("user_id")) is int
        and scoped_reads_enabled(cfg.get("principal"), authz.SCOPE_WRITE_DEVICE, cfg["user_id"])
        and authz.check(cfg["principal"], NAME).allowed
    )


def _target(uid, device_id):
    if device_id is not None:
        if not device._valid_device_id(device_id):
            raise ToolException("Invalid device_id; use an id from list_devices.")
        return device_id
    rows = [device_reads._entry(row) for row in device_reads._read(uid)]
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate device identity")
    online = [row["id"] for row in rows if row["online"]]
    if len(online) != 1:
        raise ToolException(
            "Specify device_id from list_devices; a unique online device is required."
        )
    return online[0]


def _ack(client, url, headers, device_id, request_id):
    try:
        response = client.get(url + "/" + request_id, headers=headers)
        row = response.json() if response.status_code == 200 else None
        return (
            isinstance(row, dict)
            and row.get("device_id") == device_id
            and row.get("req_id") == request_id
            and row.get("acked") is True
        )
    except (httpx.HTTPError, ValueError):
        return False


def _result(text, device_id, status, request_id):
    message = (
        "设备已确认执行显示指令。"
        if status == "acknowledged"
        else "显示指令已下发，设备尚未确认执行。"
        if status == "queued"
        else "显示请求已发出，但服务响应无法核对；实际显示状态未知，请查看设备控制台。"
    )
    receipt = {
        "skill": "device_display",
        "tool": NAME,
        "args": {"text": text, "device_id": device_id},
        "result": message,
        "action": message,
        "ts": time.time(),
        "device_status": status,
        "request_id": request_id,
    }
    return message, {"kind": "ok" if status != "unknown" else "unavailable", "receipts": [receipt]}


@tool(response_format="content_and_artifact")
def device_oled_display(
    text: Text, config: RunnableConfig, device_id: DeviceId | None = None
) -> object:
    """Display up to 64 characters on an accessible OLED demo device when requested.
    Omit device_id only when exactly one accessible device is online. No confirmation card.
    Reports command delivery separately from device acknowledgement; do not claim display completion
    without an acknowledgement. Does not manage the IoT platform.
    """
    check_cancelled(config)
    if not authorized(config):
        return "当前身份无权使用设备演示，或演示未启用。", {"kind": "unavailable"}
    if not text.strip() or any(ord(c) < 32 and c != "\n" for c in text):
        raise ToolException(
            "Display text must be nonempty and contain no control characters except newline."
        )
    uid = config["configurable"]["user_id"]
    try:
        selected = _target(uid, device_id)
        headers = {
            "Authorization": "Bearer " + device._sign_user_jwt(uid),
            "X-Request-Id": uuid.uuid4().hex,
        }
        url = device.DEVICE_SERVICE_URL.rstrip("/") + f"/api/devices/{selected}/cmd"
        check_cancelled(config)
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.put(url, headers=headers, json={"type": "display", "text": text})
            if response.status_code != 200:
                return (
                    f"设备指令未获服务接受（HTTP {response.status_code}），"
                    "请检查设备归属和在线状态。",
                    {"kind": "unavailable"},
                )
            try:
                row = response.json()
            except ValueError:
                row = None
            rid = headers["X-Request-Id"]
            if (
                not isinstance(row, dict)
                or row.get("published") is not True
                or row.get("device_id") != selected
                or row.get("req_id") != rid
            ):
                return _result(text, selected, "unknown", rid)
            acked = _ack(client, url, headers, selected, rid)
            return _result(text, selected, "acknowledged" if acked else "queued", rid)
    except (httpx.HTTPError, ValueError, RuntimeError):
        return "设备服务不可用或请求结果未知，请先查看设备控制台。", {"kind": "unavailable"}


def tools(config):
    if not authorized(config):
        return {}
    return {
        NAME: device_oled_display.model_copy(
            update={"handle_tool_error": True, "handle_validation_error": str}
        )
    }
