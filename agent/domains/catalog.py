"""Bounded article metadata and account catalogs; no content or credentials."""

from collections import Counter
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictStr

from agent.domains.admin_pages import Identifier, Page, SearchText, read_page
from agent.domains.article_metadata import article as _article
from agent.domains.article_metadata import positive as _positive
from agent.domains.article_metadata import text as _text
from agent.domains.pagination import CatalogLimit
from agent.principal import ROLE_ADMIN, ROLE_USER, ROLE_ZAKO
from agent.reports import sanitize_untrusted

Username = Annotated[StrictStr, Field(min_length=1, max_length=255)]


def _article_summary(rows):
    counts = Counter(row["status"] for row in rows)
    return {"scope": "returned_items", "count": len(rows), "status_counts": [
        {"status": status, "count": count} for status, count in counts.items()]}


def _account(row):
    if (not isinstance(row, dict) or not _positive(row.get("id"))
            or not isinstance(row.get("nickname"), str)
            or row.get("role") not in (ROLE_ADMIN, ROLE_USER, ROLE_ZAKO)
            or any(type(row.get(key)) is not bool for key in ("frozen", "muted"))
            or any(type(row.get(key)) is not int or row[key] < 0
                   for key in ("quotaUsed", "quotaLimit"))
            or "mutedUntil" not in row or (row["mutedUntil"] is not None and
                                           not isinstance(row["mutedUntil"], str))):
        raise ValueError("Invalid account metadata")
    username = _text(row.get("username"), 0)
    if username != row["username"]:
        raise ValueError("Account identifier cannot be altered")
    return {key: row[key] for key in ("id", "username", "role", "frozen", "muted",
                                     "mutedUntil", "quotaUsed", "quotaLimit")} | {
        "nickname": sanitize_untrusted(row["nickname"], 255)}


@tool
def list_admin_notes(config: RunnableConfig, keyword: SearchText | None = None,
                     category: SearchText | None = None, page: Page = 1,
                     limit: CatalogLimit = 20, article_id: Identifier | None = None) -> object:
    """分页读取后台文章元数据，包含草稿与私密文章，排除编辑修改稿，不读取正文。

    默认及最多20条，含匹配总数、原始状态、公开标志、分类与分层标签；内容过大时减小limit。
    keyword 按连续字串匹配标题、正文或标签名，category 按分类名精确匹配；不做语义搜索。
    可用 article_id 定位一篇文章，仅返回元数据。未知状态保持原值，不推测已公开。
    returned_summary仅按本次返回记录的原始status计数，不是全部匹配文章或全库状态统计。
    """
    if article_id is not None:
        page, limit = 1, 1
    return read_page("articles", config, page, limit,
                     {"keyword": keyword, "category": category, "id": article_id}, _article,
                     {"id": article_id, "categoryName": category}, summarize=_article_summary)


@tool
def list_accounts(config: RunnableConfig, username: Username | None = None,
                   page: Page = 1, limit: CatalogLimit = 20,
                   account_id: Identifier | None = None, query: SearchText | None = None) -> object:
    """Search account metadata: query matches login/nickname substrings; username/account_id
    match exactly. Combined filters use AND. Disambiguate names before preparation;
    known IDs can go directly to preparation, which verifies the target and state.
    For ambiguous matches show each ID, login username and nickname to distinguish candidates.
    Returns totals, public admin fields and quotas (quotaLimit=0: unlimited turns).
    Excludes superadmin/unknown roles and credentials; empty means no match in this scope.
    """
    if account_id is not None:
        page, limit = 1, 1
    return read_page("accounts", config, page, limit,
                     {"username": username, "id": account_id, "query": query}, _account,
                     {"id": account_id, "username": username})
