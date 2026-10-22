"""Public announcements and approved activities, with bounded text and real totals."""

from langchain_core.tools import tool

from agent.domains.public_pages import Identifier, Limit, Page, read_page
from agent.reports import sanitize_untrusted


def _text(row, key, cap):
    value, truncated = row.get(key), row.get(key + "Truncated")
    if not isinstance(value, str) or type(truncated) is not bool:
        raise ValueError("Invalid bounded text")
    return sanitize_untrusted(value, cap), truncated or len(value) > cap


def _row(row, source):
    if (not isinstance(row, dict) or type(row.get("id")) is not int
            or row["id"] <= 0):
        raise ValueError("Invalid or duplicate activity identifier")
    content, clipped = _text(row, "content", 2000)
    title, title_clipped = _text(row, "title", 255)
    created = row.get("createdAt")
    if created is not None and not isinstance(created, str):
        raise ValueError("Invalid creation time")
    result = {"id": row["id"], "title": title, "titleTruncated": title_clipped,
              "content": content, "contentTruncated": clipped, "createdAt": created}
    if source != "announcements":
        if (row.get("source") != source or type(row.get("approved")) is not int
                or row["approved"] != 1 or not isinstance(row.get("displayName"), str)
                or type(row.get("displayNameTruncated")) is not bool):
            raise ValueError("Invalid public activity boundary")
        name, name_clipped = _text(row, "displayName", 80)
        result.update({"source": source, "approved": 1,
                       "displayName": name, "displayNameTruncated": name_clipped})
    return result


def _read(resource, source, page, limit, item_id):
    if item_id is not None:
        page, limit = 1, 1
    return read_page("activity/" + resource, page, limit, {"id": item_id},
                     lambda row: _row(row, source), {"id": item_id})


@tool
def get_announcements(page: Page = 1, limit: Limit = 5,
                      item_id: Identifier | None = None) -> object:
    """分页读取公开公告，默认5条、最多10条，返回可靠总数。

    列表正文最多240字；用item_id读取单条最多2000字，截断会显式标记。
    """
    return _read("announcements", "announcements", page, limit, item_id)


@tool
def list_guestbook(page: Page = 1, limit: Limit = 5,
                    item_id: Identifier | None = None) -> object:
    """分页读取已通过审核的公开河灯留言，默认5条、最多10条。

    displayName是自由留名，不是账号。返回公开范围总数；列表正文240字，
    用item_id读取单条最多2000字，截断会标记。待审或驳回内容不在本视图。
    """
    return _read("guestbook", "board", page, limit, item_id)


@tool
def list_talks(page: Page = 1, limit: Limit = 5,
                item_id: Identifier | None = None) -> object:
    """分页读取已通过审核的公开说说，默认5条、最多10条，返回公开范围总数。

    displayName是自由留名，不是账号。列表正文240字；用item_id读取单条
    最多2000字，截断会显式标记。说说与河灯是独立内容来源。
    """
    return _read("talks", "talk", page, limit, item_id)
