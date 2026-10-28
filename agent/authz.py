"""Role grants and explicit tool scopes for the blog authorization boundary.

Runtime admission, identity verification and signed confirmation remain separate
checks. No natural-language intent or command matching belongs in this module.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.principal import ROLE_ADMIN, ROLE_SUPERADMIN, ROLE_USER, ROLE_ZAKO, Principal

SCOPE_READ_PUBLIC = "read.public"  # 公开内容：文章/标签/分类/留言/说说/公告/站点信息

SCOPE_READ_OWN = "read.own"  # 自己的私有数据：自己的会话历史、自己的设备

SCOPE_WRITE_PAGE = "write.page"  # 作用于访客自己看到的页面：导航/特效/夜间模式

SCOPE_WRITE_DEVICE = "write.device"  # 物理世界写操作：IoT 设备（当前唯一：屏幕刷字）

SCOPE_ADMIN_CONSOLE = "admin.console"  # 后台管理面**读**（Rust auth_guard 后面的东西）

SCOPE_WRITE_CONSOLE = "write.console"  # 后台管理面写入

SCOPE_WRITE_OWN = "write.own"

ALL_SCOPES = frozenset(
    {
        SCOPE_READ_PUBLIC,
        SCOPE_READ_OWN,
        SCOPE_WRITE_PAGE,
        SCOPE_WRITE_DEVICE,
        SCOPE_WRITE_OWN,
        SCOPE_ADMIN_CONSOLE,
        SCOPE_WRITE_CONSOLE,
    }
)

WRITE_SCOPES = frozenset(
    {SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE, SCOPE_WRITE_OWN, SCOPE_WRITE_CONSOLE}
)

_ROLE_SCOPES: dict[str, frozenset[str]] = {
    ROLE_USER: frozenset(
        {
            SCOPE_READ_PUBLIC,
            SCOPE_READ_OWN,
            SCOPE_WRITE_PAGE,
            SCOPE_WRITE_DEVICE,
            SCOPE_WRITE_OWN,
        }
    ),
    ROLE_ADMIN: ALL_SCOPES,
    ROLE_SUPERADMIN: ALL_SCOPES,
    ROLE_ZAKO: frozenset(),
}

TOOL_SCOPE: dict[str, str] = {
    "list_notes": SCOPE_READ_PUBLIC,
    "search_notes": SCOPE_READ_PUBLIC,
    "get_article_detail": SCOPE_READ_PUBLIC,
    "rag_search": SCOPE_READ_PUBLIC,
    "get_top_notes": SCOPE_READ_PUBLIC,
    "list_categories": SCOPE_READ_PUBLIC,
    "list_tags": SCOPE_READ_PUBLIC,
    "get_announcements": SCOPE_READ_PUBLIC,
    "list_guestbook": SCOPE_READ_PUBLIC,
    "list_talks": SCOPE_READ_PUBLIC,
    "get_blog_info": SCOPE_READ_PUBLIC,
    "get_social_links": SCOPE_READ_PUBLIC,
    "get_site_map": SCOPE_READ_PUBLIC,
    "get_current_time": SCOPE_READ_PUBLIC,
    "get_weather": SCOPE_READ_PUBLIC,
    "get_chat_history": SCOPE_READ_OWN,
    "list_devices": SCOPE_READ_OWN,
    "list_my_favorites": SCOPE_READ_OWN,
    "get_unread_counts": SCOPE_READ_OWN,
    "list_notifications": SCOPE_READ_OWN,
    "list_my_messages": SCOPE_READ_OWN,
    "list_my_board": SCOPE_READ_OWN,
    "add_favorite": SCOPE_WRITE_OWN,
    "remove_favorite": SCOPE_WRITE_OWN,
    "read_notifications": SCOPE_WRITE_OWN,
    "read_messages": SCOPE_WRITE_OWN,
    "navigate_to": SCOPE_WRITE_PAGE,
    "toggle_effect": SCOPE_WRITE_PAGE,
    "toggle_dark_mode": SCOPE_WRITE_PAGE,
    "device_oled_display": SCOPE_WRITE_DEVICE,
    "get_server_status": SCOPE_ADMIN_CONSOLE,
    "get_service_health": SCOPE_ADMIN_CONSOLE,
    "get_moderation_status": SCOPE_ADMIN_CONSOLE,
    "get_user_stats": SCOPE_ADMIN_CONSOLE,
    "get_note_stats": SCOPE_ADMIN_CONSOLE,
    "get_note_periods": SCOPE_ADMIN_CONSOLE,
    "list_admin_notes": SCOPE_ADMIN_CONSOLE,
    "list_admin_board": SCOPE_ADMIN_CONSOLE,
    "create_tag": SCOPE_WRITE_CONSOLE,
    "set_article_status": SCOPE_WRITE_CONSOLE,
    "set_article_tags": SCOPE_WRITE_CONSOLE,
    "update_tag": SCOPE_WRITE_CONSOLE,
    "delete_tag": SCOPE_WRITE_CONSOLE,
    "create_category": SCOPE_WRITE_CONSOLE,
    "update_category": SCOPE_WRITE_CONSOLE,
    "delete_category": SCOPE_WRITE_CONSOLE,
    "create_announcement": SCOPE_WRITE_CONSOLE,
    "update_announcement": SCOPE_WRITE_CONSOLE,
    "delete_announcement": SCOPE_WRITE_CONSOLE,
    "audit_board_comment": SCOPE_WRITE_CONSOLE,
    "delete_board_comment": SCOPE_WRITE_CONSOLE,
    "list_admin_comments": SCOPE_ADMIN_CONSOLE,
    "audit_comment": SCOPE_WRITE_CONSOLE,
    "delete_comment": SCOPE_WRITE_CONSOLE,
    "list_dashboard_todos": SCOPE_ADMIN_CONSOLE,
    "create_dashboard_todo": SCOPE_WRITE_CONSOLE,
    "complete_dashboard_todo": SCOPE_WRITE_CONSOLE,
    "reschedule_dashboard_todo": SCOPE_WRITE_CONSOLE,
    "freeze_account": SCOPE_WRITE_CONSOLE,
    "unfreeze_account": SCOPE_WRITE_CONSOLE,
    "set_account_role": SCOPE_WRITE_CONSOLE,
    "account_mute": SCOPE_WRITE_CONSOLE,
    "account_unmute": SCOPE_WRITE_CONSOLE,
    "send_user_notice": SCOPE_WRITE_CONSOLE,
    "list_quota_requests": SCOPE_ADMIN_CONSOLE,
    "approve_quota_request": SCOPE_WRITE_CONSOLE,
    "reject_quota_request": SCOPE_WRITE_CONSOLE,
    "reset_user_quota": SCOPE_WRITE_CONSOLE,
    "list_accounts": SCOPE_ADMIN_CONSOLE,
}

REASON_OK = "ok"

REASON_UNKNOWN_ROLE = "unknown_role"  # 身份不明 → 零权限

REASON_NO_MANIFEST = "no_manifest"  # 工具未声明 scope（fail-closed）

REASON_DENIED = "denied"  # 角色已认，但这个 scope 没授予


def scopes_for(role: str | None) -> frozenset[str]:
    """Return the explicit grant set; unknown roles have no permissions."""
    return _ROLE_SCOPES.get(role or "", frozenset())


def required_scope(tool: str) -> str | None:
    """Return a registered tool scope; unknown tools have no declaration."""
    return TOOL_SCOPE.get(tool)


def is_write(tool: str) -> bool:
    """Classify side effects from the registered scope."""
    return TOOL_SCOPE.get(tool) in WRITE_SCOPES


def manifest_gaps(tool_names) -> list[str]:
    """Report registered tools without a permission declaration."""
    return sorted((n for n in tool_names if n not in TOOL_SCOPE))


def manifest_stale(tool_names) -> list[str]:
    """Report permission declarations without a registered tool."""
    known = set(tool_names)
    return sorted((n for n in TOOL_SCOPE if n not in known))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    scope: str | None = None
    tool: str = ""

    def __str__(self) -> str:
        if self.allowed:
            return f"allow {self.tool} scope={self.scope}"
        return f"deny {self.tool} reason={self.reason} scope={self.scope or '-'}"


def check(principal: Principal | None, tool: str) -> Decision:
    """Evaluate role permission only; identity and approval are separate checks."""
    scope = required_scope(tool)
    if scope is None:
        return Decision(False, REASON_NO_MANIFEST, None, tool)
    role = principal.known_role if principal else None
    if role is None:
        return Decision(False, REASON_UNKNOWN_ROLE, scope, tool)
    if scope in scopes_for(role):
        return Decision(True, REASON_OK, scope, tool)
    return Decision(False, REASON_DENIED, scope, tool)


def holds(principal: Principal | None, scope: str) -> bool:
    """Check a declared endpoint capability without inventing a tool entry."""
    role = principal.known_role if principal else None
    return role is not None and scope in scopes_for(role)
