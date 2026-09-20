"""调用者身份（principal）——秘书类功能的地基（20260920）。

背景（为什么要单独造这个类型）：
  今天 agent 从 Rust 只拿到一个**裸 uid**（server._resolve_user_id → user_id），
  它进图之前就已经丢了"这个人是谁、他能让我做什么"。于是每个工具自己想办法：
  数据工具读公开网页、设备工具拿 uid 现签一张 JWT、"谁能做什么"无处表达——
  这正是"秘书"（以某人的名义、按授予的范围办事）落不了地的第一个卡点。

  本模块把身份变成一个显式对象，作为**唯一构造点**：
    Principal(uid, role, source)

  role 的权威来源是 Rust 侧断言里的 role 声明（Rust 从 DB 查、不信登录 token 里
  可能 7 天前的角色——与 middleware.rs 同一条纪律）；取不到就是 None = **身份不明**，
  绝不"默认当成管理员"，也绝不"默认放行"（见 agent/authz.py 的处理）。
  source 只用于审计与排障（"这条身份是验签来的还是回退信任 body 来的"）。
"""

from __future__ import annotations

from dataclasses import dataclass

# ── 角色常量 ─────────────────────────────────────────────────────────
# 与 Rust 侧 src/authz.rs 的 ROLE_* 是**跨语言契约**（同名同义），改一侧须同步另一侧。
# admin —— 博主本人，后台全权（middleware.rs auth_guard 现在只认它）
# secretary —— 秘书：可以读他人数据、可以代博主做写操作，但进不了后台管理面
# user —— 普通访客/体验账号：只能读公开内容、操作自己的设备与自己的页面
ROLE_ADMIN = "admin"
ROLE_SECRETARY = "secretary"
ROLE_USER = "user"

KNOWN_ROLES = (ROLE_ADMIN, ROLE_SECRETARY, ROLE_USER)

# 身份来源（审计字段，不做判据）
SOURCE_ASSERTION = "assertion"  # Rust 签名断言（权威）
SOURCE_BODY = "body"            # 回退信任请求体（AGENT_REQUIRE_ASSERTION=0 时的旧路径）


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


# 身份不明时用的占位（graph/server 里"拿不到 principal"的兜底：
# 老路径请求、单元测试、直接调图）。**权限模型上它是"零权限"**，
# 是否拦截由 authz 的 enforce 开关决定（默认 shadow 不拦）。
UNKNOWN = Principal(uid=0, role=None, source=SOURCE_BODY)
