"""Read exact quota targets and validate the compact guarded-action protocol."""

import re
from datetime import datetime

from agent.domains.admin_writes import available as available
from agent.principal import KNOWN_ROLES
from tools import base

PATH = "/api/protected/quota/actions"


def positive(value):
    return type(value) is int and value > 0


def reason(value):
    if not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= 255:
        raise ValueError("驳回理由须为1至255字的原文，不可省略或代编")
    return value


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("未提供完整的目标版本")
    return value


def state(value, request_id=None):
    if (not isinstance(value, dict) or not positive(value.get("userId"))
            or not isinstance(value.get("name"), str) or not value["name"].strip()
            or len(value["name"]) > 100 or value.get("role") not in KNOWN_ROLES
            or any(type(value.get(key)) is not int or value[key] < 0 for key in ("used", "limit"))
            or type(value.get("unlimited")) is not bool
            or value["unlimited"] != (value["limit"] == 0)
            or value.get("unit") != "conversation_turns"):
        raise ValueError("额度账号身份或对话轮单位无法核对")
    version(value.get("version"))
    if request_id is not None:
        row = value.get("request")
        if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] != request_id
                or type(row.get("status")) is not int or row["status"] not in (0, 1, 2)
                or any(key not in row or (row[key] is not None and
                    not isinstance(row[key], str)) for key in ("reason", "note", "handledAt"))
                or not isinstance(row.get("createdAt"), str)):
            raise ValueError("申请目标、完整理由或状态无法核对")
        if row["reason"] is not None and len(row["reason"]) > 500:
            raise ValueError("申请理由超过读取上限")
        datetime.fromisoformat(row["createdAt"])
        if row["handledAt"] is not None:
            datetime.fromisoformat(row["handledAt"])
        if row.get("handledBy") is not None and not positive(row["handledBy"]):
            raise ValueError("申请处理人无法核对")
    elif "request" in value:
        raise ValueError("主动重置不可替换为申请审批")
    return value


def read(config, *, request_id=None, user_id=None):
    if not available(config):
        return None, base.unavailable("需要已验签的管理员身份和稳定会话。")
    resource = f"requests/{request_id}" if request_id is not None else f"accounts/{user_id}"
    data = base._admin_get(PATH + "/" + resource, config)
    if isinstance(data, base.ToolResult):
        return None, data
    try:
        data = state(data, request_id)
        if user_id is not None and data["userId"] != user_id:
            raise ValueError("返回账号与指定目标不一致")
    except (ValueError, TypeError, KeyError):
        return None, base.unavailable("未能读取完整且一致的额度目标；本次未改动。")
    return data, None
