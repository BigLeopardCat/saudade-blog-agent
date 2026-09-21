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
# 排除 admin.console（20260921）与 write.console（同日第二轮）：两者都是**纯新增
# 能力**，user 从来没有过——被拒是设计本身，不是"配错"；本检查要抓的是"把 user
# 今天在用的工具误拒了"。（这条排除清单只服务于本检查，不改变授予表：`_ROLE_SCOPES`
# 里 user 一档一行没动，见 ② 后面的 secretary ⊂ admin 断言。）
USER_TOOLS = [n for n in TOOL_NAMES
              if authz.required_scope(n) not in (authz.SCOPE_READ_ANY, authz.SCOPE_ADMIN_CONSOLE,
                                                 authz.SCOPE_WRITE_CONSOLE)]
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
# 拦截判据点**逐个点名**（20260921 起 3 处）：① execute 的 shadow 记录
# ② execute 的硬拦 ③ _confirm_popup 的"能不能做"前置筛（无权做的写操作**不弹窗**
#    ——弹了就是承诺一件做不到的事，用户点完只会拿到一句拒绝）。
# 这个数字是**有意的**：每加一处都得先回答"它会不会放宽权限"。③ 只会减少
# 弹窗，不会让任何东西被允许（它用的是同一个 decision 与同一个 enforcing）。
check("拦截判据按 scope 取（admin.console 不吃 shadow，见 ⑩）——3 处：shadow/硬拦/弹窗前置筛",
      graph_src.count("authz.enforcing(decision.scope)") == 3,
      str(graph_src.count("authz.enforcing(decision.scope)")))
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
# 20260921 第二轮起**精确集合**：需确认的工具恰好是后台写（此前是"一个都没有"
# ——写工具落地那天这条必须有人看见它变了）。20260921 晚第三轮从三个变八个：标签
# 增/改/删 + 分类增/改/删五件新写工具都是"离开用户眼前、写站点内容"的写。
CONSENT_TOOLS = {n for n in TOOL_NAMES if authz.requires_consent(p(ROLE_ADMIN), n)}
check("需确认的工具恰好是八个后台写（改宽/改窄都要有人看见）",
      CONSENT_TOOLS == {"create_tag", "update_tag", "delete_tag",
                        "create_category", "update_category", "delete_category",
                        "set_article_status", "set_article_tags"},
      str(sorted(CONSENT_TOOLS)))
check("写站点内容属于需确认 scope", authz.SCOPE_WRITE_CONTENT in authz.CONSENT_SCOPES)
check("后台写属于需确认 scope", authz.SCOPE_WRITE_CONSOLE in authz.CONSENT_SCOPES)
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
        authz.CONSENT_SCOPES = frozenset({authz.SCOPE_WRITE_CONTENT, authz.SCOPE_WRITE_CONSOLE})
        authz._CONSENT_PATTERNS = orig_patterns
        del authz.TOOL_SCOPE["_probe_noconsent"]
finally:
    del authz.TOOL_SCOPE["_probe_post"]
check("探针条目清理干净", "_probe_post" not in authz.TOOL_SCOPE
      and "_probe_noconsent" not in authz.TOOL_SCOPE
      and authz.CONSENT_SCOPES == frozenset({authz.SCOPE_WRITE_CONTENT, authz.SCOPE_WRITE_CONSOLE}))

print("⑨c 后台写确认语（命令式判据：判『本轮有没有明确命令』，不是第二次确认）")
# 与 write.content 的差异：那里的判据是一个**词表式**正则（"确认发布"族），这里
# 是一条**判据函数**（_console_command）——因为后台写的命令有无数种说法，但
# "什么算命令"是可判的：必须有动作词 + 目标 + 命令句式，且排除疑问/假设。
# 误判方向不对称：判成"命令"= 直接动生产数据；判成"不是命令"= 多问一句。故 fail-closed。
CONSOLE_POS = [
    "把文章 12 设为私密",
    "把文章 12 设为私密。",
    "帮我把《架构文档》隐藏起来",
    "把文章 12 置顶",
    "取消文章 12 的置顶",
    "请把文章 12 的标签改成 Python 和 架构",
    "帮我建一个标签：Python",
    "新建一级标签「测试」",
    "给文章 12 打上标签 Python",
    "把文章 12 的标签去掉",
    "发布文章 12",
    "把这篇设为草稿",
    # 20260922：后台标签/分类写域的两个说法。活体探针腿⑭ 实测「把分类「X」改名叫
    # 「Y」」**命不中快道**（表里只有「改名为」，没有「改名/改名叫」）⇒ 用户明明下的是
    # 命令却落进弹窗那条路。命令快道是 fail-closed 的（判错只是多问一次），但"常用
    # 说法不在表里"不是保守，是缺口——这两条钉住它。
    "把分类「随笔」改名叫「碎笔」",
    "把标签「Asyncio」改名成「异步」",
    "给标签「Python」改名叫「蟒蛇」",
]
CONSOLE_NEG = [
    "把文章 12 设为私密会有什么影响？",
    "如果我把文章 12 设为私密的话会怎样",
    "把文章 12 设为私密了吗？",
    "怎么把文章置顶呢？",
    "为什么要隐藏文章？",
    "文章 12 是什么状态？",
    "帮我看看文章 12 的标签",
    "隐藏和私密有什么区别",
    "我想要一个标签系统",
    "发布功能是怎么做的？",
    "这些标签是做什么用的",
    "你能置顶文章吗",
]
_bad_pos = [t for t in CONSOLE_POS
            if not authz.consent_granted(p(ROLE_ADMIN), "set_article_status", t)]
_bad_neg = [t for t in CONSOLE_NEG
            if authz.consent_granted(p(ROLE_ADMIN), "set_article_status", t)]
check(f"后台写命令语认得（{len(CONSOLE_POS)} 条）", not _bad_pos, f"漏判: {_bad_pos}")
check(f"疑问/假设/闲聊不是命令（{len(CONSOLE_NEG)} 条）", not _bad_neg, f"误判: {_bad_neg}")
check("命令式判据三种工具共用（同一句对三个工具结论一致）",
      all(authz.consent_granted(p(ROLE_ADMIN), t, "把文章 12 设为私密")
          for t in ("create_tag", "set_article_status", "set_article_tags")))
check("同意看的是本轮消息，与 principal 无关（user 说命令也不放行——权限在更前面拦）",
      not authz.check(p(ROLE_USER), "set_article_status").allowed)

# ⑨d「确认…」短回声（20260921 生产实测）：agent 自己建议的说法、用户照抄一遍，
# 此前一律判 False ⇒ 同意闸不放行 ⇒ planner 追问 ⇒ 用户再打一遍 ⇒ 又 False
# （**没有出口**）。这一支只认句首的「确认 + 写动词」骨架，并排除打听尾。
print("⑨d 「确认…」短回声（agent 建议、用户照抄的那句话必须放行）")
CONFIRM_ECHO = [
    "确认创建标签 测试",
    "确认创建标签 agent标签功能测试",
    "确认一下新建二级标签 测试",
    "确认把文章 12 设为私密",     # 「确认 + 写动词」→ 短回声骨架
    "确认发布文章 12",
    "确认置顶文章 12",
]
_bad_echo = [t for t in CONFIRM_ECHO
             if not authz.consent_granted(p(ROLE_ADMIN), "create_tag", t)]
check(f"短回声判成命令（{len(CONFIRM_ECHO)} 条）", not _bad_echo, f"漏判: {_bad_echo}")
CONFIRM_ECHO_NEG = [
    "确认一下文章 12 是不是私密",     # 打听（是不是）——回声骨架不许把它救活
    "确认一下这个标签能不能删",       # 能不能
    "确认创建标签需要多久",           # 打听尾（多久）
    "我想确认一下文章的状态",         # 无写动词
    "确认收到",                       # 无写动词
]
_bad_echo_neg = [t for t in CONFIRM_ECHO_NEG
                 if authz.consent_granted(p(ROLE_ADMIN), "set_article_status", t)]
check(f"打听不是命令（{len(CONFIRM_ECHO_NEG)} 条）", not _bad_echo_neg, f"误判: {_bad_echo_neg}")

# 生产事故原句：**不是**命令（走弹窗，不走快道）——放宽谓词时最容易顺手连带
# 放宽的一句，单独钉住。
check("生产实测的意图原句仍不是命令（它该走弹窗，不是直接执行）",
      not authz.consent_granted(p(ROLE_ADMIN), "create_tag",
                                "一级标签，名字叫X，使用粉色颜色"))

print("⑨e 弹窗分叉：提问/假设绝不能被读成意图（is_question_like）")
# 这一支的判据比同意闸**更宽**（多一张打听类名词表）：同意闸判错只是"多问一句"，
# 而弹窗分叉判错是**弹出一个确定/取消框**——用户只是问"步骤是什么"，屏幕上却
# 出现一个写操作的确认框，那正是用户拍板不许的"把提问读成意图"。
Q_POS = [
    "文章 12 设为私密的步骤是什么",   # 生产实测漏判（上一版判 False → 弹窗）
    "把文章 12 设为私密会有什么影响？",
    "如果我把文章 12 设为私密的话",
    "文章 12 现在是私密吗",
    "改文章状态有什么风险",
    "这个标签用粉色好看吗",
    "建标签要走什么流程",
]
_bad_q = [t for t in Q_POS if not authz.is_question_like(t)]
check(f"提问/假设认得（{len(Q_POS)} 条）", not _bad_q, f"漏判: {_bad_q}")
Q_NEG = [
    "一级标签，名字叫X，使用粉色颜色",
    "新建一个标签叫 Python，用粉色",
    "确认创建标签 测试",
    "把文章 12 设为私密",
    "标签名字叫 测试",
    "帮我把文章 12 置顶",
]
_bad_q_neg = [t for t in Q_NEG if authz.is_question_like(t)]
check(f"意图陈述不误判成提问（{len(Q_NEG)} 条）", not _bad_q_neg, f"误判: {_bad_q_neg}")
check("空消息保守按提问走（无从判断时不弹窗）", authz.is_question_like("")
      and authz.is_question_like("   ") and authz.is_question_like(None))

print("⑨f 系统消息壳对判据透明（生产消息带 `[当前问题]: ` 前缀）")
# 生产实测（20260921）：server.py:413 给本轮用户消息加锚点 `[当前问题]: `，
# 而这一族的判据全是**锚定**的（句首把/将、句首动词、句首假设词）——带着壳一条
# 都命不中。后果两条都实测到了：**教科书式的明确命令**判 False（弹窗照弹），
# 假设句也判不出提问（弹窗把假设读成了意图）。修法是判据入口先剥壳，这一支锁
# 两件事：① 壳透明（带壳与不带壳判定一致）；② 剥完仍是**对**的判定（只测透明
# 度的话，"两边都判 False"也能过）。
_WRAP = "[当前问题]: "
WRAP_SAMPLES = [
    ("create_tag", "一级标签，名字叫X，使用粉色颜色"),
    ("create_tag", "新建一个标签叫 Python，用粉色"),
    ("create_tag", "确认创建标签 agent标签功能测试"),
    ("set_article_status", "把文章 12 设为私密"),
    ("set_article_status", "帮我把文章 12 置顶"),
    ("set_article_status", "把文章 12 设为私密会有什么影响？"),
    ("set_article_status", "如果我把文章 12 设为私密的话"),
    ("set_article_status", "文章 12 设为私密的步骤是什么"),
    ("set_article_status", "确认一下文章 12 是不是私密"),
]
_diff = []
for _tool, _t in WRAP_SAMPLES:
    _w = _WRAP + _t
    if authz.consent_granted(p(ROLE_ADMIN), _tool, _w) != \
            authz.consent_granted(p(ROLE_ADMIN), _tool, _t):
        _diff.append(f"consent:{_t}")
    if authz.is_question_like(_w) != authz.is_question_like(_t):
        _diff.append(f"question:{_t}")
check(f"带壳与不带壳判定一致（{len(WRAP_SAMPLES)} 条）", not _diff, f"不一致: {_diff}")
check("带壳的明确命令**确实**判成命令（不是两边都 False 的假透明）",
      authz.consent_granted(p(ROLE_ADMIN), "set_article_status",
                            _WRAP + "把文章 12 设为私密")
      and authz.consent_granted(p(ROLE_ADMIN), "set_article_status",
                                _WRAP + "把文章 999999 设为私密")
      and authz.consent_granted(p(ROLE_ADMIN), "create_tag",
                                _WRAP + "确认创建标签 agent标签功能测试"))
check("带壳的提问/假设**确实**判成提问（弹窗不许对着假设弹）",
      authz.is_question_like(_WRAP + "如果我把文章 12 设为私密")
      and authz.is_question_like(_WRAP + "把文章 12 设为私密会有什么影响？")
      and not authz.consent_granted(p(ROLE_ADMIN), "set_article_status",
                                    _WRAP + "如果我把文章 12 设为私密"))
check("剥壳只剥**开头**的方括号注记（句中的括号不动）",
      authz._strip_system_tags("[当前问题]: [系统] 把文章 12 设为私密")
      == "把文章 12 设为私密"
      and authz._strip_system_tags("把文章 12 [注]: 设为私密")
      == "把文章 12 [注]: 设为私密"
      and authz._strip_system_tags("[求助] 把文章 12 设为私密") == "把文章 12 设为私密")

print("⑨b 接线：闸在调用之前，拒绝说得出原因，叙述侧封得住")
check("execute 在调用前算确认", "consent_missing = (authz.requires_consent" in graph_src)
check("未确认时不执行（产帧而非 invoke）", "out = authz.consent_frame(name, principal)" in graph_src)
check("未确认记 trace（consent_required 事件）", '"consent_required"' in graph_src)
check("checker 认得确认原因码", "authz.consent_error_reason(text)" in graph_src)
check("gate 5a 扩了写内容的完成式声称词表（未确认却说已发布 → fallback）",
      "_WRITE_CONTENT_CLAIM_RE.search(reply)" in graph_src)
check("洞①的施事支认写内容动词（零工具轮编造『帮你发布了』）",
      "|发布|发表|投稿|提交" in graph_src)
check("洞①的施事支认清后台写动词（零工具轮编造『帮你置顶了』）",
      "|置顶|取消置顶|隐藏|下架|设为私密|设为公开|设为草稿" in graph_src)

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

print("⑨d 后台写动词的叙述侧声称表（三族成对：5a 词表 / 洞① / 哪一族不挂）")
# 新写动词（置顶/隐藏/下架/设为私密|公开|草稿/建标签/打标签）在**两个场景**各有
# 一张网，两张网都有完成标记与疑问豁免；第三族（_EXECUTION_CLAIM_RE，只在
# content_query 零帧异常轮宽查）**刻意不挂**——它没有这两条纪律，挂上会把合法反问
# 判成谎称，而那两个场景已被前两张网覆盖。下表是这三条的成对锁。
#
# 判据改动依据（20260921）：516 条真实 trace（含归档 .gz）按各自真实 gating 条件
# 复扫，改前改后命中差异 **0**（历史里没有后台写轮，故这一族没有历史实证可依，
# 靠的是"每句都成对写出来"）。
from agent.graph import (_COMPLETION_CLAIM_RE as C5,  # noqa: E402
                         _EXECUTION_CLAIM_RE as E5,
                         _STATE_ACTION_CLAIM_RE as _SAR,
                         _state_action_claim as _state_claim)

_WRITE_CONTENT_CLAIM_PATTERN = W.pattern
_STATE_ACTION_PATTERN = _SAR.pattern

# ① 完成式主张：两个场景**至少**有一张网抓得住（漏拦 = 管理员被"已经改好了"骗过）
CONSOLE_CLAIM_POS = [
    "已经帮你把文章 12 设为私密啦～", "标签已经建好啦", "已经帮你置顶了",
    "帮你把那篇隐藏了", "已经把它设为私密了", "刚刚把标签加上了",
    "成功创建了标签", "文章已经下架了", "已经取消置顶啦", "已经设为草稿了",
    "已经把它改成了公开", "已经帮主人把标签去掉了", "刚刚把它置顶好了",
    "已经把它隐藏了，这样可以吗？",   # 完成态主张 + 征询尾巴：完成在前，仍算主张
]
_bad = [t for t in CONSOLE_CLAIM_POS
        if not (W.search(t) or _state_claim(t))]
check(f"后台写完成式主张都抓得住（{len(CONSOLE_CLAIM_POS)} 条，5a 或 洞①）",
      not _bad, f"漏: {_bad}")

# ② 疑问式：consent 未过时 narrator 的**正解就是反问**，三族都不许判成声称
#    （判成声称 = 整轮换成兜底道歉，管理员拿到的不是问题而是"被主人抓包啦"）
CONSOLE_CLAIM_QUESTION = [
    "您是已经把文章 12 设为私密了吗？", "你是已经把它发布了吗？",
    "文章 12 是已经置顶了吗？", "刚才那个标签是已经建好了吗？",
    "请问文章 12 是不是已经设为私密了？", "标签已经建好了吗？",
    "您是已经帮我把标签建好了吗？", "文章 12 是不是已经下架了呢",
    "标签加上了吧？",
]
_bad_q = [t for t in CONSOLE_CLAIM_QUESTION
          if W.search(t) or _state_claim(t) or C5.search(t) or E5.search(t)]
check(f"反问句三族都不判（{len(CONSOLE_CLAIM_QUESTION)} 条）", not _bad_q, f"误伤: {_bad_q}")

# ③ 非声称：提议/假设/状态陈述/能力清单
CONSOLE_CLAIM_NEG = [
    "把文章 12 设为私密会有什么影响？", "要我帮你把文章 12 置顶吗？",
    "如果我把文章 12 设为私密的话", "我现在就帮你把文章 12 设为私密，请确认",
    "标签功能是怎么做的？", "你上次建的那个标签还在吗", "需要我把标签打上吗？",
    "文章 12 现在是私密状态哦", "设置好了样式，你看这样行不行",
    "草稿箱里那篇我读过啦", "明天下架也可以，先这样吧", "要我现在把标签加上吗？",
    "这篇文章已经置顶了很久没动过",   # 持续时长 = 状态陈述（①支的既有动词不成句）
]
_bad_n = [t for t in CONSOLE_CLAIM_NEG if W.search(t) or _state_claim(t)]
check(f"提议/假设/状态陈述不误伤（{len(CONSOLE_CLAIM_NEG)} 条）", not _bad_n, f"误伤: {_bad_n}")

# ④ 疑问豁免必须挂在**两处**（外层管 ①②③、④支自带一份）：漏一处就会在那一支
#    漏网——这正是改动过程中实际踩到的坑（只挂外层时"文章 12 是已经置顶了吗？"
#    仍被 ④支 判成声称）。
check("疑问豁免在外层（①②③ 支）",
      _WRITE_CONTENT_CLAIM_PATTERN.count("(?![吗呢吧]|[?？])") == 2,
      f"出现 {_WRITE_CONTENT_CLAIM_PATTERN.count('(?![吗呢吧]|[?？])')} 次")

# ⑤ 第三族刻意不挂新写动词（挂了 = 合法反问在 content_query 零帧轮被吞）
check("_EXECUTION_CLAIM_RE 不含后台写动词（宽查族不碰写域）",
      not any(v in E5.pattern for v in ("置顶", "隐藏", "下架", "设为私密", "新建", "创建")),
      E5.pattern)
check("_COMPLETION_CLAIM_RE 由它派生 ⇒ 同样不含",
      not any(v in C5.pattern for v in ("置顶", "隐藏", "下架", "设为私密")))
check("洞① 的 ④支 认后台写动词（零帧轮的另一张网）",
      all(v in _STATE_ACTION_PATTERN for v in ("置顶", "隐藏", "下架", "设为私密", "打上")))


def _console_state(reply: str):
    """后台写轮的最小状态：一个 consent 错误帧 + narrator 的回复。"""
    return {"plan": plan_encode(instantiate_plan("article_status",
                                                 {"article_id": 12, "status": "private"})),
            "done": False, "plan_rounds": 0,
            "messages": [
                HumanMessage(content="把文章 12 设为私密"),
                ToolMessage(content=authz.consent_frame("set_article_status", p(ROLE_ADMIN)),
                            tool_call_id="execute_0", name="set_article_status"),
                AIMessage(content=reply),
            ]}


out = gate_node(_console_state("已经帮你把文章 12 设为私密啦～"))
check("未确认 + 声称已改状态 → fallback（管理员收到的不是这句谎话）",
      bool(out.get("fallback_text")), str(out.get("fallback_text", ""))[:60])
out = gate_node(_console_state("这个操作我需要先跟你确认一下喵～"
                              "您是已经把文章 12 设为私密了吗？"))
check("未确认 + 反问确认 → 放行（正解不许被吞）",
      out.get("done") is True and not out.get("fallback_text"),
      str(out.get("fallback_text", ""))[:60])

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
check("_HARD_SCOPES = 两个后台 scope（改宽了要有人看见）",
      authz._HARD_SCOPES == frozenset({authz.SCOPE_ADMIN_CONSOLE, authz.SCOPE_WRITE_CONSOLE}),
      str(authz._HARD_SCOPES))
check("admin.console 在 ALL_SCOPES 里（否则 admin 也会被拒）",
      authz.SCOPE_ADMIN_CONSOLE in authz.ALL_SCOPES)
check("write.console 在 ALL_SCOPES 里（否则 admin 自己也写不了）",
      authz.SCOPE_WRITE_CONSOLE in authz.ALL_SCOPES)
check("write.console 是写 scope（审计与 is_write 都靠它）",
      authz.is_write("set_article_status") and authz.is_write("create_tag")
      and authz.is_write("set_article_tags"))
check("list_admin_notes 是读 scope（读后台列表不是写）",
      not authz.is_write("list_admin_notes")
      and authz.required_scope("list_admin_notes") == authz.SCOPE_ADMIN_CONSOLE)
check("审计名单只含 write.console（回执带执行身份的那一族）",
      authz.AUDIT_SCOPES == frozenset({authz.SCOPE_WRITE_CONSOLE}), str(authz.AUDIT_SCOPES))

# write.console 的三层（第一层"planner 结构性不可达"在 test_reports.py ⑪；第二层
# 身份过滤在 ⑩ 末段；这里验第三层 = execute 前的硬拦 —— **与 authz_enforce 无关**，
# 因为它是纯新增能力、没有 shadow 观测期可谈）
for tool in ("create_tag", "set_article_status", "set_article_tags"):
    check(f"{tool} 未声明身份 → deny", not authz.check(None, tool).allowed)
    check(f"{tool} 身份不明（role=None）→ deny", not authz.check(UNKNOWN, tool).allowed)
    check(f"{tool} 普通用户 → deny（write.console 是纯新增能力）",
          not authz.check(p(ROLE_USER), tool).allowed)
    check(f"{tool} 秘书 → deny（秘书刻意拿不到后台写）",
          not authz.check(p(ROLE_SECRETARY), tool).allowed)
    check(f"{tool} 管理员 → allow", authz.check(p(ROLE_ADMIN), tool).allowed)
    check(f"{tool} 不吃 shadow（authz_enforce=False 也硬拦）",
          authz.enforcing(authz.required_scope(tool)) is True
          and settings.authz_enforce is False)

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

# ⑨f 改名系说法（20260922 补词）：锁在**真正用的那两个工具**上（同意闸的判据虽然
# 与工具无关，但"这句话对 update_tag 放行"才是产品要的事实）。反向三条是这次补词
# 最容易连带放宽的形态：只是**提到**改名、或在打听改名，都不是命令。
print("⑨f 改名/改名叫 = 命令（标签与分类写共用同一句判据）")
_RENAME_POS = [
    "把分类「随笔」改名叫「碎笔」",
    "把标签「Asyncio」改名叫「异步」",
    "把标签「Asyncio」改名成「异步」",
    "给标签「Python」改名为「蟒蛇」",
]
_bad_rename = [t for t in _RENAME_POS
               if not (authz.consent_granted(p(ROLE_ADMIN), "update_tag", t)
                       and authz.consent_granted(p(ROLE_ADMIN), "update_category", t))]
check(f"改名系命令判成命令（{len(_RENAME_POS)} 条）", not _bad_rename, f"漏判: {_bad_rename}")
_RENAME_NEG = [
    "改名有什么影响",            # 无目标
    "怎么给标签改名呢",          # 打听尾（怎么/呢）
    "改名的步骤是什么",          # 无目标 + 打听
    "标签改名会不会影响文章的链接",  # 会不会（打听）
]
_bad_rename_neg = [t for t in _RENAME_NEG
                   if authz.consent_granted(p(ROLE_ADMIN), "update_tag", t)]
check(f"提到改名/打听改名不是命令（{len(_RENAME_NEG)} 条）", not _bad_rename_neg,
      f"误判: {_bad_rename_neg}")

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
