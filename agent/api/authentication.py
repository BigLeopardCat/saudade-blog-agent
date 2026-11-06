"""Verify host assertions before deriving identity or endpoint permissions."""

import logging

import jwt
from fastapi import HTTPException, Request
from jwt import InvalidTokenError

from agent import authz, delegation
from agent.principal import SOURCE_ASSERTION, Principal
from config.settings import settings

logger = logging.getLogger(__name__)


def verify_assertion_claims(token: str) -> dict | None:
    """Accept signed, unexpired agent assertions, including Rust's numeric subject."""
    try:
        if not settings.agent_assertion_secret:
            return None
        claims = jwt.decode(
            token,
            settings.agent_assertion_secret,
            algorithms=["HS256"],
            audience="agent",
            # Rust signs an integer sub. Keep signature, audience and expiry validation enabled.
            options={"require": ["exp", "sub", "aud"], "verify_sub": False},
        )
        subject = claims["sub"]
        if not (type(subject) is int or (isinstance(subject, str)
                and subject.isascii() and subject.isdecimal())):
            return None
        uid = int(subject)
        if uid <= 0:
            return None
        role = claims.get("role")
        return {"uid": uid, "role": str(role) if role else None,
                "delegation": claims.get("delegation", ""),
                "background_delegation": claims.get("background_delegation")}
    except (InvalidTokenError, ValueError, TypeError):
        return None


def resolve_principal(request: Request, body_uid: int) -> Principal:
    """Only verified host claims establish identity and delegated credentials."""
    token = request.headers.get("X-Agent-Assertion") or ""
    claims = verify_assertion_claims(token) if token else None
    if not claims:
        logger.warning("[auth] 缺少/无效身份断言 → 401")
        raise HTTPException(401, "缺少有效的服务间身份断言")
    try:
        delegation.accept(claims["uid"], claims["delegation"], claims["background_delegation"])
    except (ValueError, jwt.PyJWTError):
        raise HTTPException(401, "无效的主机委托") from None
    if claims["uid"] != body_uid:
        logger.warning("[auth] 断言覆盖 body.user_id：%s → %s（body 不可信）",
                       body_uid, claims["uid"])
    return Principal(uid=claims["uid"], role=claims["role"], source=SOURCE_ASSERTION)


def require_console(request: Request, uid: int) -> Principal:
    """Enforce the existing admin capability independently of chat tool policies."""
    principal = resolve_principal(request, uid)
    if not authz.holds(principal, authz.SCOPE_ADMIN_CONSOLE):
        logger.warning("[auth] 拒绝后台请求：%s（需要 %s）", principal, authz.SCOPE_ADMIN_CONSOLE)
        raise HTTPException(403, "需要管理员权限")
    return principal
