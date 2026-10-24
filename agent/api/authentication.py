"""Verify host assertions before deriving identity or endpoint permissions."""

import logging

import jwt
from fastapi import HTTPException, Request
from jwt import InvalidTokenError

from agent import authz
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from config.settings import settings

logger = logging.getLogger(__name__)


def verify_assertion_claims(token: str) -> dict | None:
    """Accept signed, unexpired agent assertions, including Rust's numeric subject."""
    try:
        if not settings.jwt_secret:
            return None
        claims = jwt.decode(
            token, settings.jwt_secret, algorithms=["HS256"], audience="agent",
            # Rust signs an integer sub. Keep signature, audience and expiry validation enabled.
            options={"require": ["exp", "sub", "aud"], "verify_sub": False},
        )
        uid = int(claims.get("sub") or 0)
        if uid <= 0:
            return None
        role = claims.get("role")
        return {"uid": uid, "role": str(role) if role else None}
    except (InvalidTokenError, ValueError, TypeError):
        return None


def resolve_principal(request: Request, body_uid: int) -> Principal:
    """Signed claims override body identity; compatibility mode grants no role."""
    token = request.headers.get("X-Agent-Assertion") or ""
    claims = verify_assertion_claims(token) if token else None
    if claims:
        if claims["uid"] != body_uid:
            logger.warning("[auth] 断言覆盖 body.user_id：%s → %s（body 不可信）",
                           body_uid, claims["uid"])
        return Principal(uid=claims["uid"], role=claims["role"], source=SOURCE_ASSERTION)
    if settings.agent_require_assertion:
        logger.warning("[auth] 缺少/无效身份断言 → 401")
        raise HTTPException(401, "缺少有效的服务间身份断言")
    if token:
        logger.warning("[auth] 身份断言验签失败，回退信任 body.user_id=%s", body_uid)
    return Principal(uid=body_uid, role=None, source=SOURCE_BODY)


def require_console(request: Request, uid: int) -> Principal:
    """Enforce the existing admin capability independently of chat tool policies."""
    principal = resolve_principal(request, uid)
    if not authz.holds(principal, authz.SCOPE_ADMIN_CONSOLE):
        logger.warning("[auth] 拒绝后台请求：%s（需要 %s）",
                       principal, authz.SCOPE_ADMIN_CONSOLE)
        raise HTTPException(403, "需要管理员权限")
    return principal
