"""Shared authorization and pagination boundaries for administrator reads."""

import json
from typing import Annotated
from urllib.parse import urlencode

from pydantic import Field, StrictInt

from agent.authz import SCOPE_ADMIN_CONSOLE
from agent.runtime.read_tools import scoped_reads_enabled
from tools import base

Page = Annotated[StrictInt, Field(ge=1, le=10000)]
Limit = Annotated[StrictInt, Field(ge=1, le=10)]
Identifier = Annotated[StrictInt, Field(gt=0)]
SearchText = Annotated[str, Field(min_length=1, max_length=100)]


def read_page(resource, config, page, limit, filters, project, expected=None):
    cfg = (config or {}).get("configurable") or {}
    if not scoped_reads_enabled(cfg.get("principal"), SCOPE_ADMIN_CONSOLE, cfg.get("user_id")):
        return base.unavailable("需要已验签且一致的管理员身份。")
    params = {"page": page, "page_size": limit, **filters}
    params = {key: str(value).lower() if type(value) is bool else value
              for key, value in params.items() if value is not None}
    data = base._admin_get("/api/protected/" + resource + "?" + urlencode(params),
                           config, paginated=True)
    if isinstance(data, base.ToolResult):
        return data
    if not isinstance(data, dict):
        return base.unavailable("列表返回格式无法核对。")
    rows, total = data.get("items"), data.get("total")
    offset = (page - 1) * limit
    if (not isinstance(rows, list) or len(rows) > limit or type(total) is not int or total < 0
            or len(rows) != min(limit, max(0, total - offset))):
        return base.unavailable("列表缺少可靠总数或分页范围不一致。")
    try:
        entries = [project(row) for row in rows]
    except (ValueError, TypeError, KeyError):
        return base.unavailable("记录字段无法核对。")
    expected = {key: value for key, value in (expected or {}).items() if value is not None}
    if any(any(row.get(key) != value for key, value in expected.items()) for row in entries):
        return base.unavailable("返回记录与请求范围不一致。")
    result = {"resource": resource, "filters": filters, "items": entries, "total": total,
              "page": page, "pageSize": limit, "hasMore": offset + len(entries) < total}
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > 7500:
        return base.unavailable("本页内容超过读取上限，请减小limit后重试。")
    return base.ok(encoded)
