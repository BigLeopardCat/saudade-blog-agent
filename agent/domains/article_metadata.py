"""Shared safe article metadata projection for public and admin catalogs."""

from agent.reports import sanitize_untrusted


def positive(value):
    return type(value) is int and value > 0


def text(value, limit=255):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing text")
    return sanitize_untrusted(value, limit)


def _nullable(row, key, check):
    return key in row and (row[key] is None or check(row[key]))


def article(row):
    if (not isinstance(row, dict) or not positive(row.get("id"))
            or not isinstance(row.get("title"), str)
            or type(row.get("isPublic")) is not bool
            or not _nullable(row, "status", lambda value: isinstance(value, str))
            or not _nullable(row, "isTop", lambda value: type(value) is int and value in (0, 1))
            or not _nullable(row, "categoryId", positive)
            or "categoryName" not in row or not isinstance(row.get("tags"), list)):
        raise ValueError("Invalid article metadata")
    tags = []
    for tag in row["tags"]:
        if (not isinstance(tag, dict) or not positive(tag.get("id"))
                or type(tag.get("level")) is not int or tag["level"] not in (1, 2)):
            raise ValueError("Invalid tag reference")
        tags.append({"id": tag["id"], "level": tag["level"], "name": text(tag.get("name"), 80)})
    result = {"id": row["id"], "title": sanitize_untrusted(row["title"], 255),
              "status": row["status"],
              "isPublic": row["isPublic"], "isTop": row["isTop"],
              "categoryId": row["categoryId"],
              "categoryName": (text(row["categoryName"], 100)
                               if row["categoryName"] is not None else None),
              "tags": tags[:20], "tagsTruncated": len(tags) > 20,
              "createdAt": row.get("createdAt"), "updatedAt": row.get("updatedAt")}
    result["titleTruncated"] = len(row["title"]) > 255
    return result

