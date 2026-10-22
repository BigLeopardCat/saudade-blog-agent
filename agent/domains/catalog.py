"""Bounded article metadata and account catalogs; no content or credentials."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agent.domains.admin_pages import Identifier, Limit, Page, SearchText, read_page
from agent.domains.article_metadata import article as _article
from agent.domains.article_metadata import positive as _positive
from agent.domains.article_metadata import text as _text
from agent.principal import ROLE_ADMIN, ROLE_USER, ROLE_ZAKO


def _account(row):
    if (not isinstance(row, dict) or not _positive(row.get("id"))
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
                                     "mutedUntil", "quotaUsed", "quotaLimit")}


@tool
def list_admin_notes(config: RunnableConfig, keyword: SearchText | None = None,
                     category: SearchText | None = None, page: Page = 1,
                     limit: Limit = 5, article_id: Identifier | None = None) -> object:
    """分页读取后台文章元数据，包含草稿与私密文章，排除编辑修改稿，不读取正文。

    默认5条、最多10条，含匹配总数、原始状态、公开标志、分类与分层标签。
    keyword 按连续字串匹配标题、正文或标签名，category 按分类名精确匹配；不做语义搜索。
    可用 article_id 定位一篇文章，仅返回元数据。未知状态保持原值，不推测已公开。
    """
    if article_id is not None:
        page, limit = 1, 1
    return read_page("articles", config, page, limit,
                     {"keyword": keyword, "category": category, "id": article_id}, _article,
                     {"id": article_id, "categoryName": category})


@tool
def list_accounts(config: RunnableConfig, username: SearchText | None = None,
                   page: Page = 1, limit: Limit = 5,
                   account_id: Identifier | None = None) -> object:
    """分页读取后台账号名录，默认5条、最多10条，返回匹配总数、账号名、角色、冻结、禁言与额度。

    username 必须是完整账号名，不按昵称猜测；可用 account_id 精确定位。
    超级管理员和未知角色不在名录中；不返回密码、邮箱、登录凭据，不修改账号。
    quotaLimit=0 表示不限额，用量与上限的单位均为对话轮。
    """
    if account_id is not None:
        page, limit = 1, 1
    return read_page("accounts", config, page, limit,
                     {"username": username, "id": account_id}, _account,
                     {"id": account_id, "username": username})
