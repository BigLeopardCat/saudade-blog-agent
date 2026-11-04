"""One bounded public search result; PostgreSQL owns matching and counts."""

import json
from collections import Counter
from typing import Annotated, Literal

import httpx
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from agent.domains.public_pages import Limit, Page, _response
from tools import base

Count = Annotated[StrictInt, Field(ge=0)]
Keyword = Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")]


class Counts(BaseModel):
    announcements: Count
    board: Count
    talk: Count


class Hit(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: Annotated[StrictInt, Field(gt=0)]
    source: Literal["announcements", "board", "talk"]
    title: Annotated[StrictStr, Field(max_length=120)]
    titleTruncated: StrictBool
    content: Annotated[StrictStr, Field(max_length=240)]
    contentTruncated: StrictBool
    createdAt: StrictStr | None
    approved: StrictInt | None = None
    displayName: Annotated[StrictStr, Field(max_length=80)] | None = None
    displayNameTruncated: StrictBool | None = None

    @model_validator(mode="after")
    def public_visibility(self):
        if self.source != "announcements" and (self.approved != 1
                or self.displayName is None or self.displayNameTruncated is None):
            raise ValueError("Unverified public activity")
        return self


class SearchPage(BaseModel):
    keyword: StrictStr
    matchMode: Literal["literal_case_insensitive"]
    sources: tuple[Literal["announcements"], Literal["board"], Literal["talk"]]
    counts: Counts
    total: Count
    items: list[Hit]
    page: Page
    pageSize: Limit
    hasMore: StrictBool

    @model_validator(mode="after")
    def consistent_page(self):
        counts = self.counts.model_dump()
        offset = (self.page - 1) * self.pageSize
        keys = [(row.source, row.id) for row in self.items]
        if (self.total != sum(counts.values()) or len(set(keys)) != len(keys)
                or len(self.items) != min(self.pageSize, max(0, self.total - offset))
                or self.hasMore != (offset + len(self.items) < self.total)
                or any(count > counts[source]
                       for source, count in Counter(row.source for row in self.items).items())):
            raise ValueError("Inconsistent activity search totals or page")
        return self


@tool
def search_public_activity(keyword: Keyword, page: Page = 1, limit: Limit = 5) -> object:
    """按关键词一次检索公开公告、河灯留言和说说的完整正文，返回分来源总数与分页节选。

    keyword是连续字串、不区分大小写，不是语义搜索；零命中仅指该字串未命中。
    查内容是否提及某词优先用本工具；看最新动态用列表，解读长正文需按来源和编号补读。
    河灯留名不是账号，不参与匹配；待审内容不在此视图。
    """
    keyword = keyword.strip()
    if not keyword:
        return base.unavailable("检索需要非空关键词。")
    try:
        payload = _response("activity/search", {"keyword": keyword, "page": page,
                                                "page_size": limit})
        if (not isinstance(payload, dict) or type(payload.get("code")) is not int
                or payload["code"] != 200):
            raise ValueError("Invalid activity search response")
        result = SearchPage.model_validate(payload.get("data"))
        if (result.keyword, result.page, result.pageSize) != (keyword, page, limit):
            raise ValueError("Activity search scope mismatch")
        data = result.model_dump(exclude_none=True)
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 7500:
            return base.unavailable("检索结果过大，请减小limit。")
        return base.ok(encoded, data=data)
    except (httpx.HTTPError, ValueError):
        return base.unavailable("公开活动检索失败，不能判定是否有匹配内容。")
