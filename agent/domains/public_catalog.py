"""Public article and taxonomy metadata with database pagination."""

from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent.domains.article_metadata import article, positive, text
from agent.domains.public_pages import Identifier, Limit, Page, read_page

Level = Annotated[StrictInt, Field(ge=1, le=2)]


def _article(row):
    result = article(row)
    if result["isPublic"] is not True or result["status"] not in ("public", "published"):
        raise ValueError("Article is not public")
    return result


def _taxonomy(row):
    if (not isinstance(row, dict) or not positive(row.get("id"))
            or type(row.get("publicArticleCount")) is not int or row["publicArticleCount"] < 0):
        raise ValueError("Invalid public taxonomy")
    return {"id": row["id"], "name": text(row.get("name"), 100),
            "publicArticleCount": row["publicArticleCount"]}


def _category(row):
    result = _taxonomy(row)
    for key in ("color", "icon", "pathName"):
        if key not in row or (row[key] is not None and not isinstance(row[key], str)):
            raise ValueError("Invalid category metadata")
        result[key] = text(row[key], 100) if row[key] else None
    return result


def _tag(row):
    result = _taxonomy(row)
    level, parent = row.get("level"), row.get("parentId")
    if (type(level) is not int or level not in (1, 2) or "parentId" not in row
            or (parent is not None and not positive(parent))
            or (level == 1 and parent is not None)):
        raise ValueError("Invalid tag hierarchy")
    return {**result, "level": level, "parentId": parent}


@tool
def list_notes(page: Page = 1, page_size: Limit = 5,
               category_id: Identifier | None = None) -> object:
    """分页读取公开文章元数据与总数，默认5条、最多10条，可按分类编号筛选；不读取正文。"""
    return read_page("articles", page, page_size, {"category_id": category_id}, _article,
                     {"categoryId": category_id})


@tool
def get_top_notes(page: Page = 1, page_size: Limit = 5) -> object:
    """分页读取公开的置顶文章元数据与总数，默认5条、最多10条；不读取正文。"""
    return read_page("articles", page, page_size, {"top_only": True}, _article, {"isTop": 1})


@tool
def list_categories(page: Page = 1, page_size: Limit = 5) -> object:
    """分页读取公开分类与各分类已公开文章数，默认5条、最多10条；计数不包含草稿或私密文章。"""
    return read_page("categories", page, page_size, {}, _category)


@tool
def list_tags(page: Page = 1, page_size: Limit = 5, level: Level | None = None,
              parent_id: Identifier | None = None) -> object:
    """分页读取一二级标签和公开文章计数。编号须结合level识别；parent_id筛选指定一级标签的子标签。"""
    if level == 1 and parent_id is not None:
        from tools.result import invalid_args
        return invalid_args("一级标签不能同时筛选父标签。")
    return read_page("tags", page, page_size, {"level": level, "parent_id": parent_id}, _tag,
                     {"level": level, "parentId": parent_id},
                     identity=lambda row: (row["level"], row["id"]))
