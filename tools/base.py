# -*- coding: utf-8 -*-
"""Shared HTTP channels, result exports and the tool registry.

Domain implementations share these HTTP channels and the declared tool registry.
Authorization and signed confirmation belong to the
runtime and domain execution boundaries, with independent backend enforcement."""

import logging
import re

import httpx
from langchain_core.runnables.config import RunnableConfig

# Load deployment settings before constructing module-level endpoints.
from agent.identifiers import normalize_target_id as _as_article_id  # noqa: F401
from config import settings as _settings

# Request classification and bounded read retry; see tools/http_channel.py.
from tools.http_channel import (
    API_RESPONSE_BYTES,
    ERROR_INVALID_RESPONSE,
    READ_ATTEMPTS,
    READ_TIMEOUT,
    Failure,
    read_failure,
    request_json,
    write_failure,
)

# Public tool result helpers.
from tools.result import (
    UPSTREAM_DOWN,  # noqa: F401
    ToolResult,
    empty,  # noqa: F401
    fact,  # noqa: F401
    not_found,  # noqa: F401
    ok,  # noqa: F401
    tgt,  # noqa: F401
    unavailable,
)

logger = logging.getLogger(__name__)

# Public API and retrieval corpus origin; configure blog_api_base for this deployment.
API_BASE = _settings.blog_api_base

# Derive navigation origin from the API origin. Unparseable origins use relative paths.
def _site_base(api_base: str) -> str:
    m = re.match(r"^(https?://[^/]+)", api_base or "")
    return m.group(1).rstrip("/") if m else ""


SITE_BASE = _site_base(API_BASE)


# Reuse the HTTP client with certificate verification enabled; responses enter model context.
_client = httpx.Client(timeout=15)


def _request_timeout():
    """Recheck shared cancellation and remaining time before every HTTP attempt."""
    from agent.runtime.control import model_options

    return model_options(READ_TIMEOUT).get("timeout", READ_TIMEOUT)


def _get(path: str, *, not_found_text: str = "",
         payload: dict | None = None,
         max_response_bytes: int = API_RESPONSE_BYTES) -> dict | list | ToolResult:
    """Read a public API envelope without turning failures into empty results.

    Missing targets retain the caller's optional not_found_text.
    Transient transport or 408/429/502/503/504 failures are retried once; argument
    rejection, authorization denial, business rejection and undecodable bodies are
    not. A body-based POST read is selected explicitly by passing payload — the
    path is never inspected to guess read/write intent."""
    body, failure = request_json(
        _client, method="POST" if payload is not None else "GET",
        url=f"{API_BASE}{path}", json_body=payload, timeout=_request_timeout,
        attempts=READ_ATTEMPTS, max_response_bytes=max_response_bytes)
    if failure is not None:
        return read_failure(failure, deny_msg="当前身份无权读取该公开数据",
                            not_found_text=not_found_text)
    assert body is not None
    if "data" not in body:
        return read_failure(
            Failure(ERROR_INVALID_RESPONSE),
            deny_msg="当前身份无权读取该公开数据", not_found_text=not_found_text)
    return body["data"]

# Optional device endpoint; actor credentials remain in tools.device.
from tools.device import (  # noqa: E402,F401
    DEVICE_SERVICE_URL,
)

# Site information and page-command tools.
from tools.site import (  # noqa: E402,F401
    IOT_ENABLED,
    IOT_NAV_PATH,
    get_current_time,
    get_site_map,
    nav_path_valid,
    nav_pure_path,
)


def _configured_user_id(config: RunnableConfig) -> int:
    """Extract the configured actor; authorization remains with the verified scope check."""
    return int(config.get("configurable", {}).get("user_id") or 0)


# Authenticated reads use the requesting actor. The backend rechecks role and account state.

ADMIN_BASE = _settings.agent_admin_base


def host_credential(uid: int) -> str:
    """Use a host-issued actor capability; the Agent cannot mint blog login tokens."""
    from agent.delegation import credential
    return credential(uid)


def _stop_requested(config: RunnableConfig) -> bool:
    """Read the runtime stop signal without importing runtime internals."""
    event = ((config or {}).get("configurable") or {}).get("stop_event")
    return bool(event is not None and event.is_set())


def _principal_get(path: str, config: RunnableConfig, *,
                   uid_msg: str, deny_msg: str,
                   payload: dict | None = None,
                   paginated: bool = False) -> dict | list | ToolResult:
    """Read an authenticated API using the requesting actor's short-lived token.

    A body-based POST read is chosen explicitly by passing payload; the path is
    never inspected to guess read/write intent. Transient failures are retried
    once unless the runtime stop signal is already set. Authorization denial,
    missing targets and malformed responses are typed and never retried."""
    uid = _configured_user_id(config)
    if uid <= 0:
        return unavailable(uid_msg)
    headers = {"Authorization": "Bearer " + host_credential(uid)}
    body, failure = request_json(
        _client, method="POST" if payload is not None else "GET",
        url=f"{ADMIN_BASE}{path}", headers=headers, json_body=payload,
        timeout=_request_timeout, attempts=READ_ATTEMPTS,
        stop=lambda: _stop_requested(config))
    if failure is not None:
        return read_failure(failure, deny_msg=deny_msg)
    assert body is not None
    data = body.get("data")
    if not isinstance(data, (dict, list)):
        return read_failure(Failure(ERROR_INVALID_RESPONSE), deny_msg=deny_msg)
    if paginated:
        return {"items": data, "total": body.get("total")}
    return data


def _admin_get(path: str, config: RunnableConfig, *,
               paginated: bool = False) -> dict | list | ToolResult:
    """后台接口（管理员读）。scope = admin.console。"""
    return _principal_get(
        path, config, paginated=paginated,
        uid_msg="无法获取当前用户身份，后台数据不可用",
        deny_msg="当前身份无权访问后台数据（该功能仅管理员可用）")


def _admin_read_post(path: str, payload: dict, config: RunnableConfig):
    """Read administrator data through a body-based POST with admin.console scope.
    Read failures cannot certify any business mutation."""
    return _principal_get(
        path, config, payload=payload,
        uid_msg="无法获取当前用户身份，后台数据不可用",
        deny_msg="当前身份无权访问后台数据（该功能仅管理员可用）")


def _own_get(path: str, config: RunnableConfig) -> dict | list | ToolResult:
    """Read data owned by the requesting actor.

    Missing identity or failed authorization is unavailable, never an empty list;
    read failures cannot establish that the user has no data."""
    return _principal_get(
        path, config,
        uid_msg=_NO_IDENTITY_READ,
        deny_msg="当前身份无权读取该数据")


def _own_read_post(path: str, payload: dict, config: RunnableConfig):
    """Prepare a caller-owned snapshot over POST without treating it as a write."""
    return _principal_get(path, config, payload=payload,
                          uid_msg=_NO_IDENTITY_READ, deny_msg="当前身份无权读取该数据")


# Administrator read adapters use the shared authenticated transport.

# Administrator report exports.
from tools.admin_read import (  # noqa: E402,F401
    get_note_periods,
    get_note_stats,
    get_server_status,
    get_service_health,
    get_user_stats,
    )

# Authenticated writes must distinguish HTTP success from the API business result.
# An uncertain result returns unavailable and cannot become a success receipt.

def _principal_request(method: str, path: str, payload, config: RunnableConfig,
                       *, label: str, uid_msg: str, deny_msg: str, tail: str):
    """Send an authenticated request using the verified requesting actor.

    Writes are sent exactly once. Transport failures, unreadable responses and
    statuses that cannot certify the outcome return an explicit unknown result;
    argument rejection and authorization denial are typed and say the change
    did not happen. Callers supply operation-specific identity wording."""
    uid = _configured_user_id(config)
    if uid <= 0:
        # Missing identity prevents the request from leaving this process.
        return unavailable(uid_msg)
    headers = {"Authorization": "Bearer " + host_credential(uid)}
    body, failure = request_json(
        _client, method=method, url=f"{ADMIN_BASE}{path}", headers=headers,
        json_body=payload, timeout=_request_timeout, attempts=1, write=True)
    if failure is not None:
        return write_failure(failure, label=label, deny_msg=deny_msg, tail=tail)
    assert body is not None
    return body.get("data")


# Unknown write outcomes must not be narrated as completed changes.
_NO_SUCCESS_TAIL = "（本次改动未确认生效，不要声称已改好）"

# Shared identity failures distinguish an unread source from an unconfirmed write.
_NO_IDENTITY_READ = "没有携带当前用户身份：读不到你自己的数据"
_NO_IDENTITY_WRITE = "没有携带当前用户身份：本次未改动任何内容"


def _own_write_guard(config: RunnableConfig) -> ToolResult | None:
    """Reject missing actor identity before sending a write request."""
    if _configured_user_id(config) <= 0:
        return unavailable(_NO_IDENTITY_WRITE)
    return None


def _pre_read_fail(result: ToolResult, what: str) -> ToolResult:
    """写**前**读失败 → 明说"本次未改动"（读侧那句原话保留在括号里，便于排查）。"""
    return unavailable(f"读不到{what}（{result}），本次未改动")


def _admin_request(method: str, path: str, payload, config: RunnableConfig):
    """以发起人身份请求一个**后台**写接口（scope = write.console）。"""
    return _principal_request(
        method, path, payload, config,
        label="后台",
        uid_msg="无法获取当前用户身份，本次改动未执行",
        deny_msg="当前身份无权改动后台数据（该功能仅管理员可用），本次未改动任何内容",
        tail=_NO_SUCCESS_TAIL)


def _own_request(method: str, path: str, payload, config: RunnableConfig):
    """Write caller-owned data using write.own scope.
    Missing identity prevents the request; uncertain outcomes remain unavailable."""
    return _principal_request(
        method, path, payload, config,
        label="",
        uid_msg=_NO_IDENTITY_WRITE,
        deny_msg="当前身份无权改动该数据，本次未改动任何内容",
        tail=_NO_SUCCESS_TAIL)


def _own_post(path: str, payload, config: RunnableConfig):
    """POST 的薄封装（用户自己的数据）。"""
    return _own_request("POST", path, payload, config)


def _own_delete(path: str, config: RunnableConfig):
    """DELETE 的薄封装（用户自己的数据）。`payload` 传 None（端点从路径取参数）。"""
    return _own_request("DELETE", path, None, config)



from agent.domains.own_reads import get_inbox_overview  # noqa: E402,F401
from tools.board_moderation import (  # noqa: E402,F401
    BOARD_APPROVED_CN,
    BOARD_STATUS_FILTERS,
)

# Public tools import only after API_BASE and _own_get have been defined.
from tools.notes import (  # noqa: E402,F401
    DOC_TYPE_CN,
    get_article_detail,
    get_article_stats,
    get_blog_info,
    get_social_links,
    rag_search,
    search_notes,
)
from tools.notifications import list_notifications  # noqa: E402,F401

# Current-user adapters and read-state mutations share the HTTP primitives above.
from tools.own_data import (  # noqa: E402,F401
    _fav_count,
    _fav_row,
    _fav_title,
    _favorites_snapshot,
    add_favorite,
    list_my_favorites,
    remove_favorite,
)

# Registry
# ---------------------------------------------------------------------------

_TOOL_REGISTRY = [
    search_notes,
    get_article_detail,
    get_article_stats,
    rag_search,
    get_blog_info,
    get_social_links,
    get_site_map,
    get_current_time,
    get_server_status,
    get_service_health,
    get_user_stats,
    get_note_stats,
    get_note_periods,
    list_my_favorites,
    get_inbox_overview,
    list_notifications,
    add_favorite,
    remove_favorite,
]
def get_all_tools():
    """Return the list of all registered tools."""
    return _TOOL_REGISTRY
