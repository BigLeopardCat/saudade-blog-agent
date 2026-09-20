"""权限模型（scope manifest）——"秘书能做什么"的唯一事实来源（20260920）。

设计取向（与全仓一致）：**能力用声明表达，判据在一个确定性点上**。
  工具清单是 `tools/base.py::_TOOL_REGISTRY`（业务唯一数据源），这里给它配一张
  「工具 → 所需 scope」的声明表；角色 → scope 的授予表也在这里。运行时唯一的
  判据点是 `check(principal, tool)`，由 graph.execute_node 在**调用工具之前**执行
  （与断连检查、参数引用解析同一层：确定性、无 LLM、无一例外）。

现在**默认不拦**（shadow 模式，`AGENT_AUTHZ_ENFORCE=0`）：
  决策照算、照进 trace，但不改变行为。这是为秘书功能做的前置测绘——真实流量里
  跑一段，看"谁在什么时候被拒"，用证据校准授予表，再打开开关。理由同
  `agent_require_assertion` 的滚动上线：先观测、后收口，别拿在途请求做实验。

失败取向（enforce 打开后）：
  - 未知角色（role=None / 不在 KNOWN_ROLES）→ **零权限**。从不"默认放行"，
    也从不"默认当管理员"。
  - 未声明的工具 → **拒绝**（fail-closed），并由 test_authz.py 的完备性断言在
    CI 层拦住——新增工具忘了声明是工程疏漏，不该靠运行时宽容。
  - 拒绝的形态复用既有 blocked 链路（`__ERROR__` 帧 + `scope_denied` 原因码）：
    planner 如实收尾、reflector 受限复盘，不新增决策分支，也不静默吞掉。

跨语言契约：角色名与 scope 名与 Rust 侧 `src/authz.rs` 必须一致（roles 是
`user.role` 列的取值域，scope 是两边的共同词汇表）。改一侧须同步另一侧 +
两侧各自的单测（agent: test_authz.py / rust: authz.rs 尾部 #[cfg(test)]）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent.principal import KNOWN_ROLES, ROLE_ADMIN, ROLE_SECRETARY, ROLE_USER, Principal

# ── scope 词汇表 ─────────────────────────────────────────────────────
# 命名 = <动作>.<对象>。对象轴现在只有「谁的」这一维（public / own / any），
# 第二维（哪类资源）等真有第二个消费者再加——先够表达"秘书比访客多什么"。
SCOPE_READ_PUBLIC = "read.public"      # 公开内容：文章/标签/分类/留言/说说/公告/站点信息
SCOPE_READ_OWN = "read.own"            # 自己的私有数据：自己的会话历史、自己的设备
SCOPE_READ_ANY = "read.any"            # 他人的私有数据（秘书的核心增量）
SCOPE_WRITE_PAGE = "write.page"        # 作用于访客自己看到的页面：导航/特效/夜间模式
SCOPE_WRITE_DEVICE = "write.device"    # 物理世界写操作：IoT 设备（当前唯一：屏幕刷字）
SCOPE_WRITE_CONTENT = "write.content"  # 代用户写站点内容（留言/说说/文章）——尚未有工具
SCOPE_ADMIN_CONSOLE = "admin.console"  # 后台管理面（Rust auth_guard 的那道门）

ALL_SCOPES = frozenset({
    SCOPE_READ_PUBLIC, SCOPE_READ_OWN, SCOPE_READ_ANY,
    SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE, SCOPE_WRITE_CONTENT,
    SCOPE_ADMIN_CONSOLE,
})

# 写操作：留给后续"人在回路确认"挂钩（见 docs/secretary.md 的前置需求 ③）
WRITE_SCOPES = frozenset({SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE, SCOPE_WRITE_CONTENT})

# ── 角色 → 授予 ──────────────────────────────────────────────────────
# 纪律：**授予表必须覆盖该角色当前用得到的全部工具**，否则 shadow 期给出的拒
# 绝会是我们自己配错，而不是真实越权。user 一档刻意保留 write.device——
# 设备归属由 device-service 按 uid 校验（tools/base.py 现签用户 JWT），这是既有
# 事实；本表的问题不是"是否该给"，而是"给的时候有没有被写下来"。
_ROLE_SCOPES: dict[str, frozenset[str]] = {
    ROLE_USER: frozenset({
        SCOPE_READ_PUBLIC, SCOPE_READ_OWN, SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE,
    }),
    ROLE_SECRETARY: frozenset({
        SCOPE_READ_PUBLIC, SCOPE_READ_OWN, SCOPE_READ_ANY,
        SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE, SCOPE_WRITE_CONTENT,
    }),
    ROLE_ADMIN: ALL_SCOPES,
}

# ── 工具 → 所需 scope（**完备性是硬要求**：见 test_authz.py）───────────
# 键 = 工具名（与 _TOOL_REGISTRY 一一对应）；值 = 单个 scope。
# 一个工具要多个 scope 的情况现在没有，出现时改成 tuple 并同步 check()——
# 不为想象中的需求先把判据复杂化。
TOOL_SCOPE: dict[str, str] = {
    # 公开只读（数据来自公开网页接口）
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
    "search_knowledge_base": SCOPE_READ_PUBLIC,
    "get_current_time": SCOPE_READ_PUBLIC,
    "get_weather": SCOPE_READ_PUBLIC,
    # 自己的私有数据（服务端按 uid 过滤，见 tools/base.py）
    "get_chat_history": SCOPE_READ_OWN,
    "list_devices": SCOPE_READ_OWN,
    # 作用于访客自己的页面
    "navigate_to": SCOPE_WRITE_PAGE,
    "toggle_effect": SCOPE_WRITE_PAGE,
    "toggle_dark_mode": SCOPE_WRITE_PAGE,
    # 物理世界写操作
    "device_oled_display": SCOPE_WRITE_DEVICE,
}


def scopes_for(role: str | None) -> frozenset[str]:
    """角色 → 授予的 scope 集。未知角色（含 None）→ 空集。"""
    return _ROLE_SCOPES.get(role or "", frozenset())


def required_scope(tool: str) -> str | None:
    """工具所需的 scope；未声明 → None（enforce 下按拒绝处理）。"""
    return TOOL_SCOPE.get(tool)


def is_write(tool: str) -> bool:
    """是否写操作（留给"人在回路确认"的挂钩，现在只用于观测/标注）。"""
    return TOOL_SCOPE.get(tool) in WRITE_SCOPES


def manifest_gaps(tool_names) -> list[str]:
    """哪些工具没在 TOOL_SCOPE 里声明（完备性断言用，CI 层拦新增工具的疏漏）。"""
    return sorted(n for n in tool_names if n not in TOOL_SCOPE)


def manifest_stale(tool_names) -> list[str]:
    """TOOL_SCOPE 里声明了但注册表里已没有的工具（改名/下线后残留）。"""
    known = set(tool_names)
    return sorted(n for n in TOOL_SCOPE if n not in known)


# ── 判据 ─────────────────────────────────────────────────────────────
REASON_OK = "ok"
REASON_UNKNOWN_ROLE = "unknown_role"   # 身份不明 → 零权限
REASON_NO_MANIFEST = "no_manifest"     # 工具未声明 scope（fail-closed）
REASON_DENIED = "denied"               # 角色已认，但这个 scope 没授予


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
    """唯一的权限判据：这个 principal 能不能调这个工具。

    纯函数（不看 settings、不看时间）——enforce 与否由调用方决定（`enforcing()`），
    这样"算出来的决策"可以在 shadow 模式下被记录、被回归测试，而行为不变。
    """
    scope = required_scope(tool)
    if scope is None:
        return Decision(False, REASON_NO_MANIFEST, None, tool)
    role = principal.known_role if principal else None
    if role is None:
        return Decision(False, REASON_UNKNOWN_ROLE, scope, tool)
    if scope in scopes_for(role):
        return Decision(True, REASON_OK, scope, tool)
    return Decision(False, REASON_DENIED, scope, tool)


def enforcing() -> bool:
    """是否真的拦（默认 False = shadow：只算不拦）。"""
    from config.settings import settings
    return bool(getattr(settings, "authz_enforce", False))


def denial_frame(decision: Decision, principal: Principal | None) -> str:
    """拒绝时的 __ERROR__ 帧文本。形态与参数引用失败同族，带原因码——
    planner 据此如实告知（不是"系统故障"，是"你的身份不允许"）。"""
    who = f"uid={principal.uid} role={(principal.role if principal else None) or '未知'}"
    return (f"__ERROR__: 权限不足[{decision.reason}] —— {who} 无权调用 {decision.tool}"
            f"（需要 {decision.scope}，身份声明不含它；不要换工具绕，如实告知用户）")


_SCOPE_ERR_RE = re.compile(r"权限不足\[([a-z_]+)\]")


def scope_error_reason(text: str) -> str | None:
    """从 __ERROR__ 帧文本里取回权限拒绝的原因码（execute 产帧 → checker 判 reason）。

    与 refs.ref_error_reason 同形（帧格式：`__ERROR__: 权限不足[<原因码>] —— …`），
    让拒绝在受阻链路里带上可判读的原因，而不是笼统的 error_frame。
    """
    m = _SCOPE_ERR_RE.search(text or "")
    return m.group(1) if m else None
