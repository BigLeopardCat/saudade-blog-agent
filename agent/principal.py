"""Actor identity and role names shared with the host's signed assertions."""

from __future__ import annotations

from dataclasses import dataclass

ROLE_SUPERADMIN = "superadmin"
ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLE_ZAKO = "zako"
KNOWN_ROLES = (ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_USER, ROLE_ZAKO)
ADMIN_ROLES = frozenset({ROLE_ADMIN, ROLE_SUPERADMIN})
CHAT_ONLY_ROLES = frozenset({ROLE_ZAKO})
SOURCE_ASSERTION = "assertion"
SOURCE_BODY = "body"


@dataclass(frozen=True)
class Principal:
    """一次对话的调用者。uid 是唯一的用户标识，role 可能未知（None）。"""

    uid: int
    role: str | None = None
    source: str = SOURCE_BODY

    @property
    def known_role(self) -> str | None:
        """认得的角色名；None 表示身份不明（角色非法/缺失）。"""
        return self.role if self.role in KNOWN_ROLES else None

    def __str__(self) -> str:  # 日志/审计用（不含任何凭据）
        return f"uid={self.uid} role={self.role or '?'} src={self.source}"


def is_verified_identity(principal: Principal | None) -> bool:
    """A positive integer actor ID must come from the verified host assertion."""
    return (principal is not None and getattr(principal, "source", None) == SOURCE_ASSERTION
            and type(getattr(principal, "uid", None)) is int and principal.uid > 0)


UNKNOWN = Principal(uid=0, role=None, source=SOURCE_BODY)
