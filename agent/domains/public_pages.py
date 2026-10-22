"""Bounded, unauthenticated public page transport with explicit totals."""

import json
from typing import Annotated

import httpx
from pydantic import Field, StrictInt

from tools import base

Page = Annotated[StrictInt, Field(ge=1, le=10000)]
Limit = Annotated[StrictInt, Field(ge=1, le=10)]
Identifier = Annotated[StrictInt, Field(gt=0)]


def read_page(resource, page, limit, filters, project, expected=None, identity=None):
    params = {"page": page, "page_size": limit, **filters}
    params = {key: str(value).lower() if type(value) is bool else value
              for key, value in params.items() if value is not None}
    try:
        response = base._client.get(base.API_BASE.rstrip("/") + "/" + resource,
                                    params=params)
        if response.status_code != 200:
            return base.unavailable("公开资料暂时不可用。")
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return base.unavailable("公开资料读取失败。")
    if (not isinstance(data, dict) or type(data.get("code")) is not int
            or data["code"] != 200):
        return base.unavailable("公开资料返回失败或格式无法核对。")
    rows, total = data.get("data"), data.get("total")
    offset = (page - 1) * limit
    if (not isinstance(rows, list) or type(total) is not int or total < 0
            or len(rows) != min(limit, max(0, total - offset))):
        return base.unavailable("列表缺少可靠总数或分页范围不一致。")
    try:
        entries = [project(row) for row in rows]
        key = identity or (lambda row: row["id"])
        keys = [key(row) for row in entries]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate public record")
    except (ValueError, TypeError, KeyError):
        return base.unavailable("公开记录字段无法核对。")
    expected = {key: value for key, value in (expected or {}).items() if value is not None}
    if any(any(row.get(key) != value for key, value in expected.items()) for row in entries):
        return base.unavailable("公开记录与请求范围不一致。")
    result = {"resource": resource, "filters": filters, "items": entries, "total": total,
              "page": page, "pageSize": limit, "hasMore": offset + len(entries) < total}
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > 7500:
        return base.unavailable("本页内容超过读取上限，请减小limit后重试。")
    return base.ok(encoded)
