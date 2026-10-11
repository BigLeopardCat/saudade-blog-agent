"""调用者身份（principal）——身份与权限的地基（20260920）。

背景（为什么要单独造这个类型）：
  今天 agent 从 Rust 只拿到一个**裸 uid**（server._resolve_user_id → user_id），
  它进图之前就已经丢了"这个人是谁、他能让我做什么"。于是每个工具自己想办法：
  数据工具读公开网页、设备工具拿 uid 现签一张 JWT、"谁能做什么"无处表达——
  这就是"以某人的名义、按授予的范围办事"落不了地的第一个卡点。

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
# superadmin —— 超级管理员：博主本人的账号（今天 uid=1）。在**权限**上它是管理员的
#              超集，在**被管理**上它是谁都不能动的那一个（不能被冻结/降级/删除，
#              也不出现在后台账号列表里——那两条规则的后端实现见 src/authz.rs 的
#              账号管理策略一节；agent 侧只有"目标不能是超管"这半边，见
#              graph._FREEZE_ALLOWED_TARGETS）
# admin —— 管理员，后台全权（middleware.rs auth_guard 认它和超管两个）
# user —— 普通访客/体验账号：只能读公开内容、操作自己的设备与自己的页面
# zako —— 杂鱼（20261002）：**零工具**身份，只闲聊（口吻是与别人完全不同的一档）。
#         它不是"权限还没配"的普通角色，而是"结构上不可能调用工具"的结论——见
#         下面的 CHAT_ONLY_ROLES
#
# ⚠️ 曾经还有一档 `secretary`（20260920–20261011）。它撤掉了：那一档的两项独占
# scope（read.any / write.content）一个工具都没挂上，工具集与 user **逐字节相同**
# （31 = 31，双向差集为空），生产上也从未被使用。撤掉的方式是"从 KNOWN_ROLES 里
# 拿掉"——于是 is_known_role 判假 ⇒ 后台列表/可指派范围同时失效，这是刻意的收口。
ROLE_SUPERADMIN = "superadmin"
ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLE_ZAKO = "zako"

KNOWN_ROLES = (ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_USER, ROLE_ZAKO)

# "管理员族"：能进后台管理面的那几个角色。**判据只有这一处**——技能可见性、planner
# 的人设分档、管理工具菜单都从它派生，不许在别处写 `role == ROLE_ADMIN` 这种字面量
# 比较（写一次就漏超管一次，而漏了是**静默**的：超管提权后 agent 眼里零权限 +
# 拿到访客的人设，博主自己反而用不了管理助手）。
ADMIN_ROLES = frozenset({ROLE_ADMIN, ROLE_SUPERADMIN})

# "只准闲聊族"（20261002，目前只有杂鱼）。**判据只此一处**，三条派生：
#   ① skills.visible_skills —— 只剩 `chat`（`s.chat` 为真的那个技能），planner 菜单 /
#      native schema / narrator 能力清单三处同源跟随；
#   ② prompts.audience_block —— 走 AUDIENCE_ZAKO 那一档口吻；
#   ③ graph.planner_node —— 顶部短路，连 planner LLM 都不调（**这一条才是硬保证**：
#      技能可见性约束的是"模型看到的菜单"，而 instantiate_plan 不校验可见性、authz 在
#      shadow 档下只记账不拦，只有短路让 execute 节点在本请求里一次都不会被进入）。
CHAT_ONLY_ROLES = frozenset({ROLE_ZAKO})

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
