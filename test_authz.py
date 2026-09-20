# -*- coding: utf-8 -*-
"""权限模型单测（纯函数、零网络、零 LLM，秒级）。

被测 = agent/authz.py（scope manifest + 判据）与 agent/principal.py（身份载体）。

这一层是"秘书类功能"的地基：**能力用声明表达，判据在一个确定性点上**。
所以本测试守的不是"某个工具能不能调"，而是三条结构性质：

  1. **完备性**：注册表里每个工具都在 TOOL_SCOPE 里声明过（新增工具忘了声明
     是工程疏漏，必须在 CI 层拦住——运行时 fail-closed 只是最后一道兜底）；
  2. **授予表覆盖现状**：admin/user 两档对"它们今天用得到的工具"全部放行
     ⇒ shadow 期记录下来的拒绝才可能是真实越权，而不是我们自己配错；
  3. **失败取向**：身份不明（role=None）与未声明工具一律**拒绝**，绝不默认放行。
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import authz  # noqa: E402
from agent.graph import execute_node  # noqa: E402
from agent.principal import (KNOWN_ROLES, ROLE_ADMIN, ROLE_SECRETARY, ROLE_USER,  # noqa: E402
                             SOURCE_ASSERTION, SOURCE_BODY, UNKNOWN, Principal)
from tools.base import _TOOL_REGISTRY  # noqa: E402

FAILS: list[str] = []
TOOL_NAMES = [t.name for t in _TOOL_REGISTRY]


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def p(role, uid=7):
    return Principal(uid=uid, role=role, source=SOURCE_ASSERTION)


print("① 完备性：声明表与注册表一一对应")
gaps = authz.manifest_gaps(TOOL_NAMES)
check(f"注册表 {len(TOOL_NAMES)} 个工具全部声明了 scope", not gaps, f"缺声明: {gaps}")
stale = authz.manifest_stale(TOOL_NAMES)
check("TOOL_SCOPE 无残留（改名/下线后未清）", not stale, f"残留: {stale}")
check("每个 scope 都取自词汇表 ALL_SCOPES",
      set(authz.TOOL_SCOPE.values()) <= authz.ALL_SCOPES)
check("角色授予表只引用词汇表内的 scope",
      all(s <= authz.ALL_SCOPES for s in (authz.scopes_for(r) for r in KNOWN_ROLES)))

print("② 授予表覆盖现状（shadow 期的拒绝必须是真越权，不是配错）")
# 今天 user 用得到的：公开只读 + 自己的会话/设备 + 自己的页面
USER_TOOLS = [n for n in TOOL_NAMES if authz.required_scope(n) != authz.SCOPE_READ_ANY]
denied = [n for n in USER_TOOLS if not authz.check(p(ROLE_USER), n).allowed]
check(f"user 未被拒任何现有工具（{len(USER_TOOLS)} 个）", not denied, f"误拒: {denied}")
denied = [n for n in TOOL_NAMES if not authz.check(p(ROLE_ADMIN), n).allowed]
check(f"admin 全放行（{len(TOOL_NAMES)} 个）", not denied, f"误拒: {denied}")
check("secretary 覆盖 user 的全部 scope",
      authz.scopes_for(ROLE_USER) <= authz.scopes_for(ROLE_SECRETARY))
check("secretary ⊂ admin（秘书进不了后台管理面）",
      authz.scopes_for(ROLE_SECRETARY) < authz.scopes_for(ROLE_ADMIN)
      and authz.SCOPE_ADMIN_CONSOLE not in authz.scopes_for(ROLE_SECRETARY))
check("秘书的核心增量 = read.any（读他人数据）",
      authz.SCOPE_READ_ANY in authz.scopes_for(ROLE_SECRETARY)
      and authz.SCOPE_READ_ANY not in authz.scopes_for(ROLE_USER))

print("③ 失败取向：身份不明一律拒绝（从不默认放行）")
d = authz.check(UNKNOWN, "list_notes")
check("role=None → 拒绝", not d.allowed, str(d))
check("原因码 = unknown_role", d.reason == authz.REASON_UNKNOWN_ROLE, d.reason)
check("principal=None（连身份对象都没有）→ 拒绝", not authz.check(None, "list_notes").allowed)
check("伪造角色名（'root'）→ 拒绝（按未知角色处理）", not authz.check(p("root"), "list_notes").allowed)
check("空字符串角色 → 拒绝", not authz.check(p(""), "list_notes").allowed)
check("Principal.known_role 不认未知角色", Principal(uid=1, role="root").known_role is None)
check("未声明工具 → 拒绝（fail-closed，原因码可辨）",
      not authz.check(p(ROLE_ADMIN), "some_new_tool").allowed
      and authz.check(p(ROLE_ADMIN), "some_new_tool").reason == authz.REASON_NO_MANIFEST)
check("授权角色调自己 scope 内的工具 → 放行", authz.check(p(ROLE_USER), "list_notes").allowed)
check("现有工具无一要求 read.any（秘书的增量尚无消费者——这是事实，不是遗漏）",
      not any(authz.required_scope(n) == authz.SCOPE_READ_ANY for n in TOOL_NAMES))
# DENIED 分支（角色已认、scope 未授予）现在没有真实工具能触发（秘书用得到的都在授予表里），
# 用临时探针条目直接测判据本身——否则这条分支要等真有越权工具才第一次被执行。
authz.TOOL_SCOPE["_probe_console"] = authz.SCOPE_ADMIN_CONSOLE
try:
    d_sec = authz.check(p(ROLE_SECRETARY), "_probe_console")
    d_adm = authz.check(p(ROLE_ADMIN), "_probe_console")
finally:
    del authz.TOOL_SCOPE["_probe_console"]
check("角色已认但 scope 未授予 → denied（与 unknown_role 可分辨）",
      not d_sec.allowed and d_sec.reason == authz.REASON_DENIED, str(d_sec))
check("同一工具对 admin 放行（拒的是权限，不是工具）", d_adm.allowed)
check("清理干净（探针条目未残留）", "_probe_console" not in authz.TOOL_SCOPE)
# 用一个真实存在的 write 工具验证 scope 粒度（现在没有 read.any 工具）：
check("夜间模式（write.page）：user 放行（页面是他自己的）",
      authz.check(p(ROLE_USER), "toggle_dark_mode").allowed)
check("设备刷字（write.device）：user 放行（归属由 device-service 按 uid 校验）",
      authz.check(p(ROLE_USER), "device_oled_display").allowed)

print("④ scope 粒度与写入标注")
check("read.own 工具不是 write", not authz.is_write("get_chat_history"))
check("write.page 工具被判为写操作", authz.is_write("navigate_to"))
check("write.device 工具被判为写操作", authz.is_write("device_oled_display"))
check("WRITE_SCOPES ⊆ ALL_SCOPES", authz.WRITE_SCOPES <= authz.ALL_SCOPES)
check("同一工具只声明一个 scope（多 scope 需同步改 check()，别默默支持）",
      all(isinstance(v, str) for v in authz.TOOL_SCOPE.values()))

print("⑤ 拒绝帧与原因码（走既有 blocked 链路，不新增决策分支）")
d = authz.check(UNKNOWN, "list_notes")
frame = authz.denial_frame(d, UNKNOWN)
check("拒绝帧是 __ERROR__ 形态（checker 会判 BLOCK）", frame.startswith("__ERROR__: 权限不足["))
check("原因码可被取回", authz.scope_error_reason(frame) == authz.REASON_UNKNOWN_ROLE,
      str(authz.scope_error_reason(frame)))
check("用户可读（含中文说明，不是裸错误码）", "无权调用" in frame and "list_notes" in frame)
check("非拒绝帧不误判", authz.scope_error_reason("__ERROR__: 未知工具 x") is None)
check("普通文本不误判", authz.scope_error_reason("权限不足呢") is None)

print("⑥ shadow 默认（未打开开关时行为不变）")
from config.settings import settings  # noqa: E402
check("authz_enforce 默认 False", settings.authz_enforce is False)
check("enforcing() 与设置一致", authz.enforcing() == bool(settings.authz_enforce))

print("⑦ 接线在位（图与 server 真的用了这套判据）")
root = Path(__file__).resolve().parent
graph_src = (root / "agent" / "graph.py").read_text(encoding="utf-8")
server_src = (root / "server.py").read_text(encoding="utf-8")
check("execute_node 调用 authz.check", "decision = authz.check(principal, name)" in graph_src)
check("拒绝只发生在调用之前（denial_frame 产帧而非 invoke）",
      "out = authz.denial_frame(decision, principal)" in graph_src)
check("shadow 只记拒绝不改行为（authz_shadow 事件）",
      '"authz_shadow"' in graph_src and "not authz.enforcing()" in graph_src)
check("checker 认得 scope_denied 原因码", "authz.scope_error_reason(text)" in graph_src)
check("principal 经 config 注入图", "def _principal_of" in graph_src)
check("server 构造 principal（含角色来源标注）", "_resolve_principal" in server_src
      and "SOURCE_ASSERTION" in server_src)
check("回退信任 body 的分支不给角色（role=None）",
      "Principal(uid=body_uid, role=None, source=SOURCE_BODY)" in server_src)
check("角色只来自断言（不读 body 里的任何角色字段）",
      'payload.get("role")' in server_src and "req.role" not in server_src)

print("⑧ 图节点真的收得到 config（**20260920 实测踩过的坑，别再踩**）")
# 背景：`from __future__ import annotations` 会把注解变成字符串，而 langgraph 用
# **对象比较**判断第二个参数是不是 config —— 比对不上 => 节点被当成只收 state 调用
# => config 静默取 None => `_stopped()` 恒 False（断连中断在节点内失效）、
# principal 恒 UNKNOWN。无报错、只有一条没人看的 UserWarning。
# 这里直接拿"构建图时会不会发这条警告"当判据（比断言注解类型更贴近真实机制）。
import warnings  # noqa: E402

from agent.graph import build_graph  # noqa: E402

try:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        build_graph()
    plumbed = True
    detail = ""
except UserWarning as w:
    plumbed = False
    detail = str(w)[:80]
check("graph.py 没有 from __future__ import annotations（否则 config 注入失效）",
      not any(l.strip() == "from __future__ import annotations" for l in graph_src.splitlines()))
check("构建图不触发 langgraph 的 config 注解警告（config 真的会被注入）", plumbed, detail)
check("节点 config 注解是真实类型（不是字符串）",
      not isinstance(inspect.signature(execute_node).parameters["config"].annotation, str),
      repr(inspect.signature(execute_node).parameters["config"].annotation))

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
