"""Minimal read adapter for the optional shared IoT demonstration."""

import json
from typing import Annotated

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent.authz import SCOPE_READ_OWN
from agent.reports import sanitize_untrusted
from agent.runtime.read_tools import scoped_reads_enabled
from config import settings
from tools import device
from tools.result import ok, unavailable

Page = Annotated[StrictInt, Field(ge=1, le=10000)]
Limit = Annotated[StrictInt, Field(ge=1, le=10)]


def enabled():
    return bool(settings.iot_enabled)


def _read(uid):
    url = device.DEVICE_SERVICE_URL.rstrip("/") + "/api/devices"
    headers = {"Authorization": "Bearer " + device._sign_user_jwt(uid)}
    with httpx.stream("GET", url, headers=headers, timeout=10) as response:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > 131072:
                raise ValueError("Device response exceeds limit")
            chunks.append(chunk)
    rows = json.loads(b"".join(chunks))
    if not isinstance(rows, list):
        raise ValueError("Invalid device list")
    return rows


def _entry(row):
    if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
            or len(row["id"]) > 128 or not device._valid_device_id(row["id"])
            or not isinstance(row.get("name"), str) or type(row.get("online")) is not bool):
        raise ValueError("Invalid device record")
    return {"id": row["id"], "name": sanitize_untrusted(row["name"], 80),
            "nameTruncated": len(row["name"]) > 80, "online": row["online"]}


@tool
def list_devices(config: RunnableConfig, page: Page = 1, limit: Limit = 5) -> object:
    """List accessible devices, including shared demo devices; at most 10 per page."""
    cfg = (config or {}).get("configurable") or {}
    principal = cfg.get("principal")
    uid = cfg.get("user_id")
    if not enabled():
        return unavailable("物联网演示未启用。")
    if (not scoped_reads_enabled(principal, SCOPE_READ_OWN, uid)
            or type(uid) is not int or uid != getattr(principal, "uid", None)):
        return unavailable("需要已验签且一致的当前账号身份。")
    try:
        rows = _read(uid)
        entries = [_entry(row) for row in rows]
        ids = [row["id"] for row in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate device id")
    except (httpx.HTTPError, ValueError, TypeError, UnicodeError, RuntimeError):
        return unavailable("设备服务不可用、认证失败或清单格式异常，请稍后重试。")
    offset = (page - 1) * limit
    data = {"items": entries[offset:offset + limit], "total": len(entries), "page": page,
            "pageSize": limit, "hasMore": offset + limit < len(entries),
            "access": "current_account_and_shared_demo"}
    return ok(json.dumps(data, ensure_ascii=False, separators=(",", ":")), data=data)
