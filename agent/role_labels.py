"""Blog role labels shared by tool descriptions, prompts and confirmation cards."""

ROLE_CN = {"superadmin": "超级管理员", "admin": "管理员",
           "user": "普通用户", "zako": "杂鱼"}


def role_cn(role) -> str:
    """Render a known role label; preserve unknown codes without inventing a role."""
    key = str(role or "").strip()
    return ROLE_CN.get(key, key or "未知身份")
