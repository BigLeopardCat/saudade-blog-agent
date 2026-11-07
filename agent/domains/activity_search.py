"""One bounded batch search; PostgreSQL owns matching, deduplication and counts."""

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
Keywords = Annotated[list[Keyword], Field(min_length=1, max_length=8)]
KeywordIndex = Annotated[StrictInt, Field(ge=0, le=7)]
MatchedIndices = Annotated[list[KeywordIndex], Field(min_length=1, max_length=8)]


class Counts(BaseModel):
    announcements: Count
    board: Count
    talk: Count


class KeywordCounts(BaseModel):
    keyword: Keyword
    counts: Counts
    total: Count

    @model_validator(mode="after")
    def consistent_total(self):
        if self.total != sum(self.counts.model_dump().values()):
            raise ValueError("Inconsistent keyword total")
        return self


class Hit(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: Annotated[StrictInt, Field(gt=0)]
    source: Literal["announcements", "board", "talk"]
    title: Annotated[StrictStr, Field(max_length=120)]
    titleTruncated: StrictBool
    content: Annotated[StrictStr, Field(max_length=240)]
    contentTruncated: StrictBool
    createdAt: StrictStr | None
    matchedKeywordIndices: MatchedIndices
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
    keywords: Keywords
    keywordMode: Literal["any"]
    keywordCounts: Annotated[list[KeywordCounts], Field(min_length=1, max_length=8)]
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

    @model_validator(mode="after")
    def consistent_keyword_scope(self):
        counts = self.counts.model_dump()
        if ([row.keyword for row in self.keywordCounts] != self.keywords
                or len({word.lower() for word in self.keywords}) != len(self.keywords)
                or self.total > sum(row.total for row in self.keywordCounts)):
            raise ValueError("Inconsistent activity keyword scope")
        for index, word in enumerate(self.keywordCounts):
            if any(value > counts[source] for source, value in word.counts.model_dump().items()):
                raise ValueError("Keyword count exceeds union count")
            observed = Counter(row.source for row in self.items
                               if index in row.matchedKeywordIndices)
            if any(value > word.counts.model_dump()[source] for source, value in observed.items()):
                raise ValueError("Page exceeds keyword counts")
        for row in self.items:
            indices = row.matchedKeywordIndices
            if (indices != sorted(set(indices))
                    or any(index >= len(self.keywords) for index in indices)):
                raise ValueError("Unverified or duplicate keyword matches")
        return self


@tool
def search_public_activity(keywords: Keywords, page: Page = 1, limit: Limit = 5) -> object:
    """一次提交1至8个扩写关键词，批量检索公开公告、河灯留言和说说的完整正文。

    keywords按任一词命中，不区分大小写，重复词自动去重；不是语义搜索。
    total/counts是去重并集，keywordCounts是各词计数，词间可能重叠不能相加。
    matchedKeywordIndices是命中词在keywords中的0起始位置；分页节选有明确截断标志。
    本工具只返回带截断标志的节选，不代表已读取全文。
    河灯留名不是账号，不参与匹配；待审内容不在此视图。
    """
    normalized, seen = [], set()
    for word in keywords:
        value = word.strip()
        if value.lower() not in seen:
            normalized.append(value)
            seen.add(value.lower())
    if any(not word for word in normalized):
        return base.unavailable("检索需要非空关键词。")
    try:
        payload = _response("activity/search", {}, method="POST", json_body={
            "keywords": normalized, "page": page, "page_size": limit})
        if (not isinstance(payload, dict) or type(payload.get("code")) is not int
                or payload["code"] != 200):
            raise ValueError("Invalid activity search response")
        result = SearchPage.model_validate(payload.get("data"))
        if (result.keywords, result.page, result.pageSize) != (normalized, page, limit):
            raise ValueError("Activity search scope mismatch")
        data = result.model_dump(exclude_none=True)
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 7500:
            return base.unavailable("检索结果过大，请减少关键词数量或limit。")
        return base.ok(encoded, data=data)
    except (httpx.HTTPError, ValueError):
        return base.unavailable("公开活动检索失败，不能判定是否有匹配内容。")
