"""Read bounded article targets with an opaque content-bound version."""

from agent.domains.admin_writes import available as available
from tools import base

PATH = "/api/protected/article-actions/"


def canonical_status(value):
    return "public" if value == "published" else value


def row(value):
    if (
        not isinstance(value, dict)
        or type(value.get("id")) is not int
        or value["id"] <= 0
        or not isinstance(value.get("title"), str)
        or len(value["title"]) > 255
        or value.get("status") not in ("public", "published", "private", "draft")
        or type(value.get("isPublic")) is not bool
        or (
            value.get("isTop") is not None
            and (type(value["isTop"]) is not int or value["isTop"] not in (0, 1))
        )
        or "isTop" not in value
        or not isinstance(value.get("version"), str)
        or len(value["version"]) != 64
        or any(char not in "0123456789abcdef" for char in value["version"])
    ):
        raise ValueError("Invalid article target")
    if value["isPublic"] and canonical_status(value["status"]) != "public":
        raise ValueError("Inconsistent article visibility")
    return {key: value[key] for key in ("id", "title", "status", "isPublic", "isTop", "version")}


def read(article_id, config):
    value = base._admin_get(PATH + str(article_id), config)
    if isinstance(value, base.ToolResult):
        return value
    try:
        result = row(value)
        if result["id"] != article_id:
            raise ValueError("Article id mismatch")
        return result
    except (ValueError, TypeError, KeyError):
        return base.unavailable("未能核对文章目标和版本，本次未准备或执行改动。")


def satisfied(current, args):
    return (
        args["status"] is None
        or (
            canonical_status(current["status"]) == args["status"]
            and current["isPublic"] == (args["status"] == "public")
        )
    ) and (args["is_top"] is None or current["isTop"] == args["is_top"])


def description(args):
    fields = []
    if args["status"] is not None:
        labels = {"public": "公开", "private": "私密", "draft": "草稿"}
        fields.append("状态改为" + labels[args["status"]])
    if args["is_top"] is not None:
        fields.append("置顶" if args["is_top"] else "取消置顶")
    return "、".join(fields)
