# -*- coding: utf-8 -*-
"""Shared HTTP channels, result exports and the tool registry.

Domain implementations live in sibling modules. Shared clients and request helpers
are resolved through this module at call time so consumers and test doubles use the
same transport. Re-exports preserve existing import paths; _TOOL_REGISTRY retains
the declared tool order. Authorization and signed confirmation belong to the
runtime and domain execution boundaries, with independent backend enforcement."""

import logging
import re
import time

import httpx
import jwt
from langchain_core.runnables.config import RunnableConfig

# Load deployment settings before constructing module-level endpoints.
from config import settings as _settings

# Result helpers remain re-exported for existing callers and transport test doubles.
from tools.result import (
    _FRAME_ID_KEYS,  # noqa: F401 —— 外面取：tests/test_item_position_guide.py
    _LIST_FIELD_KEYS,  # noqa: F401 —— 外面取：本条契约的一部分
    _LIST_FIELD_MAX,  # noqa: F401 —— 外面取：tests/test_frame_caps.py
    _LIST_ROWS_MAX,  # noqa: F401 —— 外面取：tests/test_frame_caps.py
    TGT_KINDS,  # noqa: F401 —— 外面取：tests/test_write_facts.py
    UPSTREAM_DOWN,
    ToolResult,
    _board_text_keys,  # noqa: F401 —— 外面取：tests/test_id_namespaces.py
    _cap_rows,  # noqa: F401 —— 外面取：tests/test_frame_caps.py
    _clip_fields,  # noqa: F401 —— 外面取：tests/test_frame_caps.py
    _label_id_keys,  # noqa: F401 —— 外面取：tests/test_id_namespaces.py
    _rows_with_note,  # noqa: F401 —— 外面取：tests/test_frame_caps.py
    _shape,  # noqa: F401 —— 外面取：tests/test_id_namespaces.py + tests/test_userdata.py
    empty,  # noqa: F401 —— 外面取：tests/test_hardening.py（`base.empty`，取的是函数对象）
    fact,  # noqa: F401 —— 外面取：tests/test_write_facts.py 等（`base.fact(...)`）
    is_noop,  # noqa: F401 —— 外面取：shared no-op result contract
    not_found,
    ok,  # noqa: F401 —— 外面取：tests/test_admin_write.py 等（`base.ok(...)`）
    tgt,  # noqa: F401 —— 外面取：tests/test_write_facts.py 等（`base.tgt(...)`）
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

def _get(path: str, *, not_found_text: str = "") -> dict | list | ToolResult:
    """Read a public API envelope without turning failures into empty results.

    Only callers that supply not_found_text classify HTTP 404 as a missing target;
    other HTTP, business-code, transport and decoding failures remain unavailable."""
    try:
        resp = _client.get(f"{API_BASE}{path}")
        if not_found_text and resp.status_code == 404:
            logger.info("API 404: %s", path)
            return not_found(not_found_text)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") == 200:
            return body["data"]
        logger.warning("API error: %s", body.get("message"))
        return UPSTREAM_DOWN
    except Exception as exc:
        logger.error("API call failed: %s", exc)
        return UPSTREAM_DOWN

# Import public tools after their shared channel definitions to avoid circular initialization.

# Device tools and identity helpers.
from tools.device import (  # noqa: E402,F401
    _DISPLAY_DEDUP_SECONDS,
    DEVICE_SERVICE_URL,
    _device_get_user_id,
    _jwt_secret,
    _last_display,
    _last_display_lock,
    _sign_user_jwt,
    _valid_device_id,
    device_oled_display,
    list_devices,
)

# Site information and page-command tools.
from tools.site import (  # noqa: E402,F401
    _ITEM_POSITION_FORMS,
    _NAV_EXACT_PATHS,
    _NAV_PREFIX_PATHS,
    _NAV_WHOLE_PAGE_PATHS,
    IOT_ENABLED,
    IOT_NAV_PATH,
    get_chat_history,
    get_current_time,
    get_site_map,
    get_weather,
    nav_path_valid,
    nav_pure_path,
    navigate_to,
    toggle_dark_mode,
    toggle_effect,
)

# Authenticated reads use the requesting actor. The backend rechecks role and account state.

ADMIN_BASE = _settings.agent_admin_base


def _sign_local_jwt(uid: int, role: str | None) -> str:
    """Sign a 60-second actor token for local backend requests.

    The backend reloads account role and frozen state. Numeric sub and absent aud/ver
    match its token contract: the caller identity was just verified for this request,
    so a guessed token generation would incorrectly reject valid delegated calls.
    Missing signing configuration fails before any request is sent."""
    # Reject an empty signing key before sending any authenticated request.
    secret = _jwt_secret()
    if not secret:
        raise RuntimeError("JWT_SECRET 未配置（空密钥不许代签令牌）——检查 .env / 环境变量")
    return jwt.encode({
        "sub": uid,
        "exp": int(time.time()) + 60,
        "role": role or "user",
    }, secret, algorithm="HS256")


def _principal_get(path: str, config: RunnableConfig, *,
                   uid_msg: str, deny_msg: str,
                   payload: dict | None = None,
                   paginated: bool = False) -> dict | list | ToolResult:
    """Read an authenticated API using the requesting actor's short-lived token.

    API failures remain unavailable. POST is used only for body-based read filters.
    Paginated reads preserve total counts instead of discarding envelope metadata.
    """
    uid = _device_get_user_id(config)
    if uid <= 0:
        return unavailable(uid_msg)
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        if payload is None:
            resp = _client.get(f"{ADMIN_BASE}{path}", headers=headers, timeout=15)
        else:
            resp = _client.post(f"{ADMIN_BASE}{path}", headers=headers,
                                json=payload, timeout=15)
    except Exception as exc:
        logger.error("principal API call failed: %s", exc)
        return unavailable(f"接口请求失败: {exc}")
    if resp.status_code in (401, 403):
        return unavailable(deny_msg)
    if resp.status_code != 200:
        return unavailable(f"接口返回 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception:
        return unavailable("接口返回的不是 JSON")
    if not isinstance(body, dict):
        return unavailable("接口返回的不是有效响应信封")
    if body.get("code") != 200:
        logger.warning("principal API error: %s", body.get("message"))
        return unavailable(f"接口报错: {body.get('message')}")
    data = body.get("data")
    if not isinstance(data, (dict, list)):
        return unavailable("接口未返回有效的可读取数据")
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
    """后台接口（管理员**读**，但筛选条件走请求体 ⇒ POST）。scope 同 `_admin_get`。

    与 `_admin_post` 的差别只在措辞：那条是写通道（失败时说"本次改动未确认生效"），
    这条读失败时说"后台数据不可用"——同一件事别用两句不一样的话说。
    """
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


# Administrator read tools resolve shared transports through this module at call time.

# Administrator report and moderation-list exports.
from tools.admin_read import (  # noqa: E402,F401
    get_moderation_status,
    get_note_periods,
    get_note_stats,
    get_server_status,
    get_service_health,
    get_user_stats,
    list_admin_board,
    # Article comments and guestbook messages use separate endpoints and ID namespaces.
    list_admin_comments,
)

# Authenticated writes must distinguish HTTP success from the API business result.
# An uncertain result returns unavailable and cannot become a success receipt.

def _principal_request(method: str, path: str, payload, config: RunnableConfig,
                       *, label: str, uid_msg: str, deny_msg: str, tail: str):
    """Send an authenticated request using the verified requesting actor.

    HTTP, business-code and decoding failures remain unavailable and cannot certify
    a write. Callers supply operation-specific identity and failure wording."""
    uid = _device_get_user_id(config)
    if uid <= 0:
        # Missing identity prevents the request from leaving this process.
        return unavailable(uid_msg)
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.request(method, f"{ADMIN_BASE}{path}",
                               headers=headers, json=payload, timeout=15)
    except Exception as exc:
        logger.error("principal %s %s failed: %s", method, path, exc)
        return unavailable(f"{label}接口请求失败: {exc}{tail}")
    if resp.status_code in (401, 403):
        return unavailable(deny_msg)
    if resp.status_code != 200:
        return unavailable(f"{label}接口返回 HTTP {resp.status_code}{tail}")
    try:
        body = resp.json()
    except Exception:
        return unavailable(f"{label}接口返回的不是 JSON{tail}")
    if body.get("code") != 200:
        # ⚠️ 见本节头注：只看 HTTP 状态码会把 Rust 的 ApiResponse::error 当成功。
        logger.warning("principal %s %s error: %s", method, path, body.get("message"))
        return unavailable(f"{label}接口报错: {body.get('message')}{tail}")
    return body.get("data")


# 写操作失败路径的固定尾句：下游 回复模型 靠它如实告知"别声称已改好"。
_NO_SUCCESS_TAIL = "（本次改动未确认生效，不要声称已改好）"

# Shared identity failures distinguish an unread source from an unconfirmed write.
_NO_IDENTITY_READ = "没有携带当前用户身份：读不到你自己的数据"
_NO_IDENTITY_WRITE = "没有携带当前用户身份：本次未改动任何内容"


def _own_write_guard(config: RunnableConfig) -> ToolResult | None:
    """写工具入口的统一哨兵：没身份 → 直接返回，调它之前**一个请求都不发**。

    写操作最不该做的就是在没身份时猜"写给谁"（"我以为给谁写了"比"没写成"坏得多）。
    """
    if _device_get_user_id(config) <= 0:
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
    """以发起人身份写**用户自己**的数据（scope = write.own：收藏 / 标记已读）。

    与 `_admin_request` 是同一条通道的两种措辞，判据一样严（任何不确定路径都
    unavailable）。uid<=0 时一个字节都不发：写操作最不该做的就是在没身份时猜——
    对访客来说，"我以为给谁写了"比"没写成"坏得多。
    """
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








# Known target refusals are a cross-language contract; unknown failures remain unavailable.
_NOTICE_TARGET_REFUSALS = ("用户不存在", "该账号不能接收通知")




# Tag helpers and administration tools.
from tools.tag_admin import (  # noqa: E402,F401
    _tag_index,
)


# Article ID parsing is shared by article and current-user tools.
def _as_article_id(value) -> int | None:
    """实参 → 正整数 id；不合法 → None（调用方拒绝，不猜、不默认）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    s = str(value).strip()
    return int(s) if s.isdigit() and int(s) > 0 else None


# Article target helpers and administration tools.
from tools.note_admin import (  # noqa: E402,F401
    list_admin_notes,
)


def _near_miss_names(want: str, cands, limit: int = 3):
    """Return approximate names for disambiguation, never authorization.

    Candidates contain the whitespace-normalized query or vice versa, with at
    least three characters on either side. Sort by containment, length difference
    and ID. Write selection still requires an exact, unique match."""
    w = re.sub(r"\s+", "", str(want or ""))
    if len(w) < 3:
        return []
    out = []
    for cid, name in cands:
        n = re.sub(r"\s+", "", str(name or ""))
        if len(n) < 3:
            continue
        if w in n or n in w:
            out.append((0 if w in n else 1, abs(len(n) - len(w)), cid, name))
    out.sort(key=lambda x: (x[0], x[1], x[2]))
    return [(cid, name) for _, _, cid, name in out[:limit]]


# Account target helpers and administration tools.
from agent.domains.own_reads import get_unread_counts  # noqa: E402,F401
from tools.account_admin import (  # noqa: E402,F401
    _reachable_uids,
    _user_directory,
    list_accounts,
)

# Announcement target helpers and administration tools.
# Guestbook moderation helpers and tools.
from tools.board_moderation import (  # noqa: E402,F401
    BOARD_APPROVED_CN,
    BOARD_STATUS_FILTERS,
)

# Category helpers and administration tools.
from tools.category_admin import (  # noqa: E402,F401
    _category_index,
)

# Article comment moderation uses distinct targets and receipts from guestbook moderation.
# Dashboard mutations update one row; never replace the entire user-maintained list.
from tools.dashboard_todos import (  # noqa: E402,F401
    _todo_rows,
    list_dashboard_todos,
)

# Public tools import only after API_BASE and _own_get have been defined.
from tools.notes import (  # noqa: E402,F401
    _NOTE_COUNT_KEYS,
    DOC_TYPE_CN,
    _drop_dead_mine,
    _note_counts,
    _note_public_counts,
    _note_row_with_tag_names,
    _public_tag_index,
    _read_section,
    _slim_note_rows,
    get_announcements,
    get_article_detail,
    get_blog_info,
    get_social_links,
    get_top_notes,
    list_categories,
    list_guestbook,
    list_my_board,
    list_notes,
    list_tags,
    list_talks,
    rag_search,
    search_notes,
)
from tools.notifications import list_notifications  # noqa: E402,F401

# Current-user adapters and read-state mutations share the HTTP primitives above.
from tools.own_data import (  # noqa: E402,F401
    _as_bool,
    _as_ids,
    _fav_count,
    _fav_row,
    _fav_title,
    _favorites_snapshot,
    add_favorite,
    list_my_favorites,
    list_my_messages,
    remove_favorite,
)

# Quota request helpers and administration tools.
from tools.quota_admin import (  # noqa: E402,F401
    list_quota_requests,
)

# Registry
# ---------------------------------------------------------------------------

_TOOL_REGISTRY = [
    list_notes,
    search_notes,
    get_article_detail,
    rag_search,
    get_top_notes,
    list_categories,
    list_tags,
    get_announcements,
    list_guestbook,
    list_talks,
    get_blog_info,
    get_social_links,
    get_site_map,
    get_chat_history,
    get_current_time,
    get_weather,
    navigate_to,
    toggle_effect,
    toggle_dark_mode,
    list_devices,
    device_oled_display,
    # 管理助手报表：scope = admin.console，见本节头注
    get_server_status,
    get_service_health,
    get_moderation_status,
    get_user_stats,
    # 文章流量报表：同 admin.console（读的是后台统计面）
    get_note_stats,
    # 文章**分期**报表：同 admin.console，读的是 `.../stats/notes/periods`
    # ——后台数据统计页「周报/月报/年报」那个页签的同一个端点
    get_note_periods,
    # 后台**留言名册**：同 admin.console，读的是 `GET /api/protect/board`
    # （逐条含发表账号）——与公开的 list_guestbook 是两套视图，别混用
    list_admin_board,
    # 后台**评论名册**：同 admin.console，读的是 `GET /api/protect/comments`。
    # 与上面那条并列而不是同一件事（两张表、两个后台页签），见 admin_read 的头注。
    list_admin_comments,
    # 管理助手后台写：list_admin_notes=admin.console，
    # 写工具=write.console，见"管理助手写工具"节头注
    list_admin_notes,
    # 标签改/删 + 分类增删改：同样是 write.console，
    # 目标是**名字**（模型 写名字，工具确定性解析成 id，见 _find_named_tag）
    # 站内公告代发/改/删：同样是 write.console，目标=公告标题
    # 河灯留言的人工复核：同样是 write.console，目标=正文片段
    # （留言没有名字/标题，见 _find_board_comment）
    # Article-comment IDs are distinct from guestbook IDs.
    # 用户自己的数据：scope = read.own（三档角色都有），见本节头注
    list_my_favorites,
    get_unread_counts,
    list_notifications,
    # 我自己的河灯：也是 read.own。与上面三条同族——"我自己那一份"
    # 在公开池里读不出来（公开池恒 Approved=1，且 `mine` 那一格结构上恒假）。
    list_my_board,
    # 用户自己的数据·写那一半：scope = write.own，写前先读、写后再读，
    # 见"写那一半"节头注
    add_favorite,
    remove_favorite,
    # 站内信（私信）：读信箱=read.own，标记已读=write.own，
    # 见"站内信"节头注（术语：站内信 ≠ 河灯留言）
    list_my_messages,
    # Dashboard reads and single-row mutations.
    list_dashboard_todos,
    # Account directory and actor-scoped administration tools.
    list_accounts,
    # Freeze and unfreeze preserve separate operation directions.
    # Account role policy is enforced by the backend.
    # Muting blocks publication; freezing blocks login. Their receipts must stay distinct.
    # Notices are irreversible outbound content and require review of the full body.
    # Quota approval, rejection and direct reset have distinct effects and receipts.
    list_quota_requests,
]

def get_all_tools():
    """Return the list of all registered tools."""
    return _TOOL_REGISTRY
