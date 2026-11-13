"""Public announcements and approved activities, with bounded text and real totals."""

from datetime import datetime

from langchain_core.tools import tool

from agent.domains.public_pages import Identifier, Limit, Page, read_page


def _text(row, key, cap):
    value, truncated = row.get(key), row.get(key + "Truncated")
    if not isinstance(value, str) or type(truncated) is not bool:
        raise ValueError("Invalid bounded text")
    # These fields are JSON data, not report prose or executable commands.
    # Preserve whitespace and literal text so a requested quotation is possible.
    return value[:cap], truncated or len(value) > cap


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


def _chronology(rows, *, page, item_id):
    # Host pages are ordered by created_at DESC, id DESC; bounds apply only to this page.
    summary = {"scope": "returned_page", "order": "createdAt_desc_id_desc",
               "newest": None, "oldest": None, "includesCollectionNewest": False}
    if not rows or any(row["createdAt"] is None for row in rows):
        return summary
    keys = [(datetime.fromisoformat(row["createdAt"]), row["id"]) for row in rows]
    if keys != sorted(keys, reverse=True):
        raise ValueError("Activity chronology does not match the host contract")
    summary.update(newest={key: rows[0][key] for key in ("id", "createdAt")},
                   oldest={key: rows[-1][key] for key in ("id", "createdAt")},
                   includesCollectionNewest=page == 1 and item_id is None)
    return summary


def _read(resource, source, page, limit, item_id):
    if item_id is not None:
        page, limit = 1, 1
    return read_page("activity/" + resource, page, limit, {"id": item_id},
                     lambda row: _row(row, source), {"id": item_id},
                     summarize=lambda rows: _chronology(rows, page=page, item_id=item_id))


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

    displayName是公开自由留名，不是发表账号；管理员查询发布者使用后台留言工具。
    概述保留各条原文的被指对象，不把陌生姓名归为博主或调用者。
    返回公开范围总数；列表正文240字，
    用item_id读取单条最多2000字，截断会标记。待审或驳回内容不在本视图。
    """
    return _read("guestbook", "board", page, limit, item_id)


@tool
def list_talks(page: Page = 1, limit: Limit = 5,
                item_id: Identifier | None = None) -> object:
    """分页读取已通过审核的公开说说，默认5条、最多10条，返回公开范围总数。

    displayName是自由留名，不是账号。列表正文240字；用item_id读取单条
    最多2000字，截断会显式标记。说说与河灯是独立内容来源。
    近期概览用第一页并说明已读范围；仅按用户指定的数量或全量范围续读后页。
    """
    return _read("talks", "talk", page, limit, item_id)
