"""Validate one bounded moderation target and its explicit absence contract."""

from agent.principal import KNOWN_ROLES
from tools import base

PATH = "/api/protected/moderation"
RESOURCES = {"board": "boards", "comment": "comments"}


def version(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _nullable_text(value, limit):
    if value is not None and (not isinstance(value, str) or len(value) > limit):
        raise ValueError("invalid target text")
    return value


def _publisher(data, uid):
    if data is None:
        return None
    if (
        not isinstance(data, dict)
        or type(data.get("id")) is not int
        or data["id"] != uid
        or data.get("role") not in KNOWN_ROLES
    ):
        raise ValueError("invalid publisher identity")
    for field in ("username", "nickname"):
        if not isinstance(data.get(field), str) or len(data[field]) > 100:
            raise ValueError("invalid publisher name")
    return {key: data[key] for key in ("id", "username", "nickname", "role")}


def project(data, resource, identifier):
    if (
        not isinstance(data, dict)
        or type(data.get("id")) is not int
        or data["id"] != identifier
        or type(data.get("userId")) is not int
        or data["userId"] < 0
        or type(data.get("approved")) is not int
        or data["approved"] not in (0, 1, 2)
        or not isinstance(data.get("content"), str)
        or len(data["content"]) > 500
        or not version(data.get("version"))
    ):
        raise ValueError("invalid moderation target")
    for field in ("contentTruncated", "isDeleted", "rootVisible", "articleVisible"):
        if type(data.get(field)) is not bool:
            raise ValueError("unknown target visibility")
    for field in ("affectedReplies", "publicReplies"):
        if type(data.get(field)) is not int or data[field] < 0:
            raise ValueError("unknown reply impact")
    if data["publicReplies"] > data["affectedReplies"]:
        raise ValueError("inconsistent reply impact")
    if resource == "board":
        if (
            data.get("articleId") is not None
            or data.get("articleTitle") is not None
            or data["isDeleted"]
            or data["affectedReplies"] != 0
        ):
            raise ValueError("wrong moderation collection")
    elif (
        type(data.get("articleId")) is not int
        or data["articleId"] <= 0
        or data["userId"] <= 0
        or data.get("displayName") is not None
    ):
        raise ValueError("invalid comment article")
    result = {
        key: data[key]
        for key in (
            "id",
            "userId",
            "content",
            "approved",
            "version",
            "isDeleted",
            "contentTruncated",
            "rootVisible",
            "articleVisible",
            "affectedReplies",
            "publicReplies",
        )
    }
    result["publisher"] = _publisher(data.get("publisher"), data["userId"])
    result.update(
        {
            key: _nullable_text(data.get(key), limit)
            for key, limit in (
                ("articleTitle", 255),
                ("displayName", 200),
                ("rejectReason", 200),
                ("aiReason", 200),
            )
        }
    )
    result["articleId"] = data.get("articleId")
    return result


def read(resource, identifier, config):
    data = base._admin_get(f"{PATH}/{RESOURCES[resource]}/{identifier}/target", config)
    if isinstance(data, base.ToolResult):
        return None, data
    try:
        if (
            not isinstance(data, dict)
            or data.get("resource") != RESOURCES[resource]
            or type(data.get("id")) is not int
            or data["id"] != identifier
            or type(data.get("exists")) is not bool
            or data["exists"] != isinstance(data.get("target"), dict)
        ):
            raise ValueError("target availability unknown")
        if not data["exists"]:
            if data.get("target") is not None:
                raise ValueError("invalid absence evidence")
            return None, None
        return project(data["target"], resource, identifier), None
    except (ValueError, TypeError, KeyError):
        return None, base.unavailable("审核接口未提供可靠目标、归属或删除影响，本次未执行。")
