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
# 今天 user 用得到的：公开只读 + 自己的会话/设备 + 自己的页面。
# 排除 admin.console（20260921）：那四个是**纯新增能力**，user 从来没有过——它被拒
# 是设计本身，不是"配错"；本检查要抓的是"把 user 今天在用的工具误拒了"。
USER_TOOLS = [n for n in TOOL_NAMES
              if authz.required_scope(n) not in (authz.SCOPE_READ_ANY, authz.SCOPE_ADMIN_CONSOLE)]
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
      '"authz_shadow"' in graph_src and "not authz.enforcing(decision.scope)" in graph_src)
check("拦截判据按 scope 取（admin.console 不吃 shadow，见 ⑩）",
      graph_src.count("authz.enforcing(decision.scope)") == 2)
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

print("⑨ 写操作的「人在回路」确认（前置需求 ③：权限之后还有一次同意）")
# 权限判"这个人能不能做"，确认判"这一次他到底要不要做"。只有**离开用户眼前**的
# 写入（写站点内容：对外可见、收不回）要确认；页面/设备写的效果用户立刻看得见。
check("现有工具没有一个是需确认的 scope（今天行为零变化）",
      not any(authz.requires_consent(p(ROLE_ADMIN), n) for n in TOOL_NAMES),
      str([n for n in TOOL_NAMES if authz.requires_consent(p(ROLE_ADMIN), n)]))
check("写站点内容属于需确认 scope", authz.SCOPE_WRITE_CONTENT in authz.CONSENT_SCOPES)
check("页面/设备写不需要确认（效果就在用户眼前）",
      authz.SCOPE_WRITE_PAGE not in authz.CONSENT_SCOPES
      and authz.SCOPE_WRITE_DEVICE not in authz.CONSENT_SCOPES)
check("server 侧写工具（若将来有）只由声明驱动，不需改函数",
      "CONSENT_SCOPES" in inspect.getsource(authz.requires_consent))

# 用临时探针工具把真实分支跑一遍（现在没有 write.content 工具）
authz.TOOL_SCOPE["_probe_post"] = authz.SCOPE_WRITE_CONTENT
try:
    sec = p(ROLE_SECRETARY)
    check("需确认工具：无确认语 → 未获同意",
          authz.requires_consent(sec, "_probe_post")
          and not authz.consent_granted(sec, "_probe_post", "帮我在留言板发一条：你好呀"))
    check("需确认工具：明确确认语 → 获准",
          authz.consent_granted(sec, "_probe_post", "确认发布"))
    check("确认语看的是**本轮消息**，空消息/None 一律不认",
          not authz.consent_granted(sec, "_probe_post", "")
          and not authz.consent_granted(sec, "_probe_post", None))
    check("普通聊到『发布』不算确认（须是明确的确认说法）",
          not authz.consent_granted(sec, "_probe_post", "发布功能是怎么做的？")
          and not authz.consent_granted(sec, "_probe_post", "你上次发布的那篇写得不错"))
    check("确认是用户的事，与 principal 无关（无声明 scope 的工具不受影响）",
          not authz.requires_consent(sec, "list_notes")
          and authz.consent_granted(sec, "list_notes", "确认发布") is False)
    frame = authz.consent_frame("_probe_post", sec)
    check("未确认帧是 __ERROR__ 形态（checker 判 BLOCK、gate 5a 生效）",
          frame.startswith(f"__ERROR__: 待确认[{authz.REASON_CONSENT}]"))
    check("原因码可被取回且与权限拒绝可分辨",
          authz.consent_error_reason(frame) == authz.REASON_CONSENT
          and authz.scope_error_reason(frame) is None)
    check("权限拒绝帧不会被误读成确认拒绝",
          authz.consent_error_reason(authz.denial_frame(authz.check(UNKNOWN, "list_notes"), UNKNOWN)) is None)
    check("帧文案要求去问用户、不得声称完成",
          "未执行" in frame and "确认" in frame)
    # fail-closed：需要确认的 scope 若没配确认语表，绝不默认放行
    authz.CONSENT_SCOPES  # frozenset，只读
    fake_scope = "write.probe"
    authz.TOOL_SCOPE["_probe_noconsent"] = fake_scope
    orig_patterns = authz._CONSENT_PATTERNS
    authz.CONSENT_SCOPES = frozenset({fake_scope})     # 临时换一组"需确认但没配语表"
    try:
        check("需确认却没配确认语表 → fail-closed（不默认放行）",
              authz.requires_consent(sec, "_probe_noconsent")
              and not authz.consent_granted(sec, "_probe_noconsent", "确认发布"))
    finally:
        authz.CONSENT_SCOPES = frozenset({authz.SCOPE_WRITE_CONTENT})
        authz._CONSENT_PATTERNS = orig_patterns
        del authz.TOOL_SCOPE["_probe_noconsent"]
finally:
    del authz.TOOL_SCOPE["_probe_post"]
check("探针条目清理干净", "_probe_post" not in authz.TOOL_SCOPE
      and "_probe_noconsent" not in authz.TOOL_SCOPE
      and authz.CONSENT_SCOPES == frozenset({authz.SCOPE_WRITE_CONTENT}))

print("⑨b 接线：闸在调用之前，拒绝说得出原因，叙述侧封得住")
check("execute 在调用前算确认", "consent_missing = (authz.requires_consent" in graph_src)
check("未确认时不执行（产帧而非 invoke）", "out = authz.consent_frame(name, principal)" in graph_src)
check("未确认记 trace（consent_required 事件）", '"consent_required"' in graph_src)
check("checker 认得确认原因码", "authz.consent_error_reason(text)" in graph_src)
check("gate 5a 扩了写内容的完成式声称词表（未确认却说已发布 → fallback）",
      "_WRITE_CONTENT_CLAIM_RE.search(reply)" in graph_src)
check("洞①的施事支认写内容动词（零工具轮编造『帮你发布了』）",
      "|发布|发表|投稿|提交)" in graph_src)

# 写内容声称词表的正负例（隔着"帮你"两个字也要认；转述用户过往动作不许误伤）
from agent.graph import _WRITE_CONTENT_CLAIM_RE as W  # noqa: E402

for _p in ("已经帮你发布好啦～", "帮你把留言发出去了", "已经发布了", "留言成功提交啦"):
    check(f"写内容完成式声称：认得「{_p}」", bool(W.search(_p)))
for _n in ("你已经提交过河灯啦", "要我现在发布吗？你回复「确认发布」就好啦",
           "发布功能是怎么做的？", "这条还没发出去喵",
           "要我帮你把留言发出去吗？", "我已经帮你发布的那篇文章里有错别字"):
    check(f"写内容完成式声称：不误伤「{_n}」", not W.search(_n))

# 端到端：未获确认的写操作 + 叙述说"已发布" → gate 必须兜住
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from agent.graph import gate_node, plan_encode  # noqa: E402
from agent.skills import instantiate_plan  # noqa: E402


def _write_state(reply: str):
    return {"plan": plan_encode(instantiate_plan("content_query", {"calls": []})),
            "done": False, "plan_rounds": 0,
            "messages": [
                HumanMessage(content="帮我在留言板发一条：今天天气真好"),
                ToolMessage(content=authz.consent_frame("_probe_post", p(ROLE_SECRETARY)),
                            tool_call_id="execute_0", name="_probe_post"),
                AIMessage(content=reply),
            ]}


out = gate_node(_write_state("已经帮你发布好啦～"))

check("未确认 + 声称已发布 → fallback（用户收到的不是这句谎话）",
      bool(out.get("fallback_text")), str(out.get("fallback_text", ""))[:50])
out = gate_node(_write_state("这条还没发出去喵，要我现在发布吗？你回复「确认发布」就好啦"))
check("未确认 + 如实说『还没发、要确认』 → 放行（不许误伤诚实收尾）",
      out.get("done") is True and not out.get("fallback_text"),
      str(out.get("fallback_text", ""))[:50])

print("⑩ 管理助手 admin.console（20260921）：硬拦 + 身份过滤双层")
# 这一批是"纯新增能力"：历史流量里一条都没有 ⇒ 没有 shadow 观测期可谈，硬拦。
# 三层结构，本节点验后两层（第一层"结构性不可达"在 test_reports.py ⑪）：
#   ② 身份：非 admin 的 planner 上下文里看不到这三个技能 ⇒ 选不出来；
#   ③ 判据：execute 前的 authz.check + enforcing(scope) 硬拦。
ADMIN_TOOLS = ["get_server_status", "get_service_health", "get_moderation_status", "get_user_stats"]
ADMIN_SKILLS = ["ops_report", "moderation_report", "user_report"]

check("admin.console 是硬拦（不随 authz_enforce 走）",
      authz.enforcing(authz.SCOPE_ADMIN_CONSOLE) is True and settings.authz_enforce is False,
      f"scope={authz.enforcing(authz.SCOPE_ADMIN_CONSOLE)} switch={settings.authz_enforce}")
check("别的 scope 仍跟随全局开关（没顺手把整表改成硬拦）",
      all(authz.enforcing(s) == bool(settings.authz_enforce)
          for s in authz.ALL_SCOPES - authz._HARD_SCOPES))
check("无参调用 = 旧语义（不知道 scope 的调用点行为不变）",
      authz.enforcing() == bool(settings.authz_enforce))
check("_HARD_SCOPES 只含 admin.console（改宽了要有人看见）",
      authz._HARD_SCOPES == frozenset({authz.SCOPE_ADMIN_CONSOLE}), str(authz._HARD_SCOPES))
check("admin.console 在 ALL_SCOPES 里（否则 admin 也会被拒）",
      authz.SCOPE_ADMIN_CONSOLE in authz.ALL_SCOPES)

for tool in ADMIN_TOOLS:
    check(f"{tool} 未声明 → deny", not authz.check(None, tool).allowed)
    check(f"{tool} 身份不明（role=None）→ deny", not authz.check(UNKNOWN, tool).allowed)
    check(f"{tool} 普通用户 → deny", not authz.check(p(ROLE_USER), tool).allowed)
    check(f"{tool} 秘书 → deny（秘书拿不到运维面，这是刻意的）",
          not authz.check(p(ROLE_SECRETARY), tool).allowed)
    check(f"{tool} 管理员 → allow", authz.check(p(ROLE_ADMIN), tool).allowed)
    check(f"{tool} 拒绝原因码是 scope_denied（走既有 blocked 链路）",
          authz.check(p(ROLE_USER), tool).reason == authz.REASON_DENIED)
check("秘书一档**没有**被顺手放开（_ROLE_SCOPES 未动）",
      not any(authz.check(p(ROLE_SECRETARY), t).allowed for t in ADMIN_TOOLS))

from agent.skills import SKILL_MAP, build_planner_context  # noqa: E402

for name in ADMIN_SKILLS:
    sk = SKILL_MAP.get(name)
    check(f"技能 {name} 声明了 roles={{admin}}", sk is not None and sk.roles == frozenset({ROLE_ADMIN}),
          str(sk and sk.roles))
for role in (None, ROLE_USER, ROLE_SECRETARY):
    ctx = build_planner_context(role)
    check(f"planner 上下文（role={role}）不含管理助手技能",
          all(n not in ctx for n in ADMIN_SKILLS))
check("planner 上下文（admin）含全部三个管理助手技能",
      all(n in build_planner_context(ROLE_ADMIN) for n in ADMIN_SKILLS))
check("公开技能对任何角色都还在（过滤没写宽）",
      all(n in build_planner_context(None) for n in ("chat", "content_query", "navigate"))
      and all(n in build_planner_context(ROLE_USER) for n in ("chat", "content_query", "navigate")))

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
