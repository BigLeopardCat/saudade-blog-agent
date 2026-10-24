# -*- coding: utf-8 -*-
"""权限模型单测（纯函数、零网络、零 LLM，秒级）。

被测 = agent/authz.py（scope manifest + 判据）与 agent/principal.py（身份载体）。

这一层是身份与权限的地基：**能力用声明表达，判据在一个确定性点上**。
所以本测试守的不是"某个工具能不能调"，而是三条结构性质：

  1. **完备性**：注册表里每个工具都在 TOOL_SCOPE 里声明过（新增工具忘了声明
     是工程疏漏，必须在 CI 层拦住——运行时 fail-closed 只是最后一道兜底）；
  2. **授予表覆盖现状**：admin/user 两档对"它们今天用得到的工具"全部放行
     ⇒ shadow 期记录下来的拒绝才可能是真实越权，而不是我们自己配错；
  3. **失败取向**：身份不明（role=None）与未声明工具一律**拒绝**，绝不默认放行。
"""
import inspect
import re
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))

from agent import authz
from agent.principal import (  # noqa: E402
    ADMIN_ROLES,
    KNOWN_ROLES,
    ROLE_ADMIN,
    ROLE_SUPERADMIN,
    ROLE_USER,
    ROLE_ZAKO,
    SOURCE_ASSERTION,
    SOURCE_BODY,
    UNKNOWN,
    Principal,
)
from tools.base import _TOOL_REGISTRY  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)
TOOL_NAMES = [t.name for t in _TOOL_REGISTRY]


def p(role, uid=7):
    return Principal(uid=uid, role=role, source=SOURCE_ASSERTION)


def test_authz():
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
    # 里 user 一档一行没动，见 ② 后面的 zako ⊂ user 断言。）
    USER_TOOLS = [n for n in TOOL_NAMES
                  if authz.required_scope(n) not in (authz.SCOPE_ADMIN_CONSOLE,
                                                     authz.SCOPE_WRITE_CONSOLE)]
    denied = [n for n in USER_TOOLS if not authz.check(p(ROLE_USER), n).allowed]
    check(f"user 未被拒任何现有工具（{len(USER_TOOLS)} 个）", not denied, f"误拒: {denied}")
    denied = [n for n in TOOL_NAMES if not authz.check(p(ROLE_ADMIN), n).allowed]
    check(f"admin 全放行（{len(TOOL_NAMES)} 个）", not denied, f"误拒: {denied}")
    check("zako ⊂ user（杂鱼进不了站内操作面）",
          authz.scopes_for(ROLE_ZAKO) < authz.scopes_for(ROLE_USER)
          and authz.SCOPE_WRITE_OWN not in authz.scopes_for(ROLE_ZAKO))
    check("user ⊂ admin（普通用户进不了后台管理面）",
          authz.scopes_for(ROLE_USER) < authz.scopes_for(ROLE_ADMIN)
          and authz.SCOPE_ADMIN_CONSOLE not in authz.scopes_for(ROLE_USER))
    check("admin ⊂ superadmin（超管的增量只在对人操作那一档）",
          authz.scopes_for(ROLE_ADMIN) <= authz.scopes_for(ROLE_SUPERADMIN))

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
    # DENIED 分支（角色已认、scope 未授予）用临时探针条目直接测判据本身——
    # 否则这条分支要等真有越权工具才第一次被执行。
    authz.TOOL_SCOPE["_probe_console"] = authz.SCOPE_ADMIN_CONSOLE
    try:
        d_zak = authz.check(p(ROLE_ZAKO), "_probe_console")
        d_adm = authz.check(p(ROLE_ADMIN), "_probe_console")
    finally:
        del authz.TOOL_SCOPE["_probe_console"]
    check("角色已认但 scope 未授予 → denied（与 unknown_role 可分辨）",
          not d_zak.allowed and d_zak.reason == authz.REASON_DENIED, str(d_zak))
    check("同一工具对 admin 放行（拒的是权限，不是工具）", d_adm.allowed)
    check("清理干净（探针条目未残留）", "_probe_console" not in authz.TOOL_SCOPE)
    # 用一个真实存在的 write 工具验证 scope 粒度：
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

    print("⑨ 写操作的「人在回路」确认（前置需求 ③：权限之后还有一次同意）")
    # 权限判"这个人能不能做"，确认判"这一次他到底要不要做"。只有**离开用户眼前**的
    # 写入（写站点内容：对外可见、收不回）要确认；页面/设备写的效果用户立刻看得见。
    # 20260921 第二轮起**精确集合**：需确认的工具恰好是后台写（此前是"一个都没有"
    # ——写工具落地那天这条必须有人看见它变了）。20260921 晚第三轮从三个变八个：标签
    # 增/改/删 + 分类增/改/删五件新写工具都是"离开用户眼前、写站点内容"的写。
    CONSENT_TOOLS = {n for n in TOOL_NAMES if authz.requires_consent(p(ROLE_ADMIN), n)}
    # 20260923 批 7 从十三个到十六个：加进来的是 write.own 那三个（收藏/取消收藏/标记
    # 已读）。它们**不满足**"离开用户眼前"那句措辞（只写自己账号里、不外显），但满足
    # 措辞背后的真判据——"这一次到底要不要做"需要一个确定性答案（agent 自己判断"这篇
    # 值得收藏"就替用户收藏，是以用户名义往他账号里写状态）。详见 authz 的 CONSENT_SCOPES。
    # 20260923 批 8 到十七个：同族再加一件 `read_messages`（标记**信**已读）。
    # 20260926 到十八个：加的是 `create_dashboard_todo`（往主人后台首页的待办里加一条）。
    # 它进这张表**不是**因为后果更重（他随时能删能改），而是因为它是唯一一件"目标由主人
    # 随口描述、站内没有东西可核对"的写——命令式措辞与内容描述在这句话里同形，那个
    # `_console_command` 判据本来也答不了"他是在让我记、还是在跟我聊这件事"（详见 authz
    # 里 `_ALWAYS_CONFIRM_TOOLS` 的注）。它是**一律弹窗**族的一员 ⇒ 这一条也是它的锁。
    # 20260926 到二十个：同一天再加 `freeze_account` / `unfreeze_account`——它们动的
    # **不是主人的东西**（后果落在第三方的登录能力上，解冻也换不回被踢的会话），
    # 用户拍板"每次都弹卡、不留任何捷径"（详见 authz 里 `_ALWAYS_CONFIRM_TOOLS` 的注）。
    # 20260926 到二十一个：再加 `complete_dashboard_todo`（把某一条待办勾成完成）——
    # 与加待办同一张表、同一条理由（目标是主人随口说的一句话，`_console_command` 判不出
    # "他是在让我勾、还是在跟我说这件事办完了"），而漏判的代价更贵：勾错的那一行在列表上
    # 和勾对的一模一样（没有 id 也没有日志给他看）。
    # 20260926 到二十二个：同一天再加 `send_user_notice`（给单个账号发一条站内通知）——
    # 它动的**不是主人的东西**、也不是仓库里的内容，而是**以主人的名义发给第三方的一段话**，
    # 且站内没有删除已发通知的通道（发出去收不回）。与冻结族同一条理由：正文由 agent 替
    # 主人**整理**（用户拍板允许），人眼复核点只剩卡面 ⇒ 一律弹卡、不留任何捷径。
    # 20260929 到二十五个：再加对话额度三件（`approve_quota_request` / `reject_quota_request`
    # / `reset_user_quota`）——批准等于**再给 500 轮**、驳回是**关于第三方**的裁决（且会给他
    # 发一条收不回的通知）、主动重置**不需要他申请过**。三件动的都不是主人的东西，且没有
    # 可改判的余地 ⇒ 一律弹卡（详见 authz 里 `_ALWAYS_CONFIRM_TOOLS` 的三条各自注）。
    # 20260929 到二十六个：待办族的第三条最小通道 `reschedule_dashboard_todo`（改某一条的
    # 排期日）。与勾完成逐字同族——两条通道都**只动主人后台首页那一行**、判据都是"正文逐字
    # 相等"，而错格子的代价更隐蔽：勾错了看得见（列表上多一个勾），日子改错了**没有任何
    # 痕迹**，主人要等到那天没被提醒才发现。同族的 `create_dashboard_todo` /
    # `complete_dashboard_todo` 已在名单里，这一条不与它们分家。
    # 20261002 到二十七个：批 J 的 `set_account_role`（变更一个后台账号的身份）。与冻结族
    # 同一条理由——它动的**不是主人的东西**，而后果比冻结更"换档"：降成杂鱼 = 那个账号以后
    # 什么站内操作都做不了、只能闲聊，升回普通用户 = 把能力还给他。用户拍板"每次都弹卡"，
    # 卡面必须印全「从哪个身份 → 到哪个身份」（详见 authz 里 `_ALWAYS_CONFIRM_TOOLS` 的注）。
    # 20261002 到二十九个：内容风控下放的禁言两件（`account_mute` / `account_unmute`）。
    # 与冻结族同一条理由，但后果是它的**反面**（禁言不踢人下线，只是让他发不出评论与留言）
    # ⇒ 名额进得来，措辞一个字都不许共用（详见 authz 里那两条 why 与 `adminops._MUTE_*`）。
    # 20261019 到三十一个：文章评论的两件（`audit_comment` / `delete_comment`）。与留言那两件
    # 逐条同族（访客写下的内容、复核可改判、删除没有回收站），但**两族的 why 一个字都不许
    # 共用**——说成「留言」会让主人去翻错的那个后台页（两张表、两个页签）。
    check("需确认的工具恰好是三十一个（二十七个后台写 + 四个用户自己的写）",
          CONSENT_TOOLS == {"create_tag", "update_tag", "delete_tag",
                            "create_category", "update_category", "delete_category",
                            "create_announcement", "update_announcement",
                            "delete_announcement",
                            "audit_board_comment", "delete_board_comment",
                            "audit_comment", "delete_comment",
                            "create_dashboard_todo", "complete_dashboard_todo",
                            "reschedule_dashboard_todo",
                            "freeze_account", "unfreeze_account",
                            "send_user_notice",
                            "approve_quota_request", "reject_quota_request",
                            "reset_user_quota",
                            "set_account_role",
                            "account_mute", "account_unmute",
                            "set_article_status", "set_article_tags",
                            "add_favorite", "remove_favorite", "read_notifications",
                            "read_messages"},
          str(sorted(CONSENT_TOOLS)))
    # 公告三件**永不走"同轮命令即确认"**（用户点名要求：内容必须弹窗等管理员确认）。
    # 这条锁的形态是"恒 False"而不是"某句话判不出来"：只要有人把捷径重新打开，
    # 无论用户那句话写得多像命令，这里都会红。
    _ANN_CMDS = ["发个公告说今晚 23 点维护", "把《维护通知》删了",
                 "新建一个公告叫维护通知，内容今晚维护", "确认发布公告"]
    check("公告三件即使写成明确命令也判不出确认（捷径结构性关闭）",
          all(not authz.consent_granted(p(ROLE_ADMIN), t, m)
              for t in ("create_announcement", "update_announcement", "delete_announcement")
              for m in _ANN_CMDS),
          str([m for m in _ANN_CMDS
               if authz.consent_granted(p(ROLE_ADMIN), "create_announcement", m)]))
    # 同一句「新建一个…叫 X」对标签/分类仍判成命令——收窄只落在公告三件上。
    # （删除类命令本来就不在动作词表里（"删了"这种词形没进表），那是既有收窄方向：
    # 判不出来就弹窗问一次，不放开。20261015 批 5 起这条"不放开"是 `_CONSOLE_TOOL_ACTIONS`
    # 里 `delete_*` 两行的显式空位，不再依赖"词表恰好没长那几个词"。）
    check("同一句话对**别的**后台写仍算命令（收窄只落在公告三件上）",
          authz.consent_granted(p(ROLE_ADMIN), "create_tag", "新建一个标签叫 探针标签")
          and authz.consent_granted(p(ROLE_ADMIN), "create_category", "新建一个分类叫 随笔"))
    # 删留言（20260922 第六轮）进"一律弹窗"：动的是**访客写下的内容**、且没有回收站。
    # 与公告三件同一取向，只是理由不同（那三件是"以主人名义对全体访客说话"）。
    _BOARD_DEL_CMDS = ["把 12 号那条留言删了", "删掉那条留言",
                       "确认删除留言 12", "驳回并删除 id=12 的留言"]
    check("删留言即使写成明确命令也判不出确认（捷径结构性关闭）",
          all(not authz.consent_granted(p(ROLE_ADMIN), "delete_board_comment", m)
              for m in _BOARD_DEL_CMDS),
          str([m for m in _BOARD_DEL_CMDS
               if authz.consent_granted(p(ROLE_ADMIN), "delete_board_comment", m)]))
    # 审核（通过/驳回）**20260929 起改判为"一律弹窗"**（批 H · S2）。旧理由（"它可改判、
    # 命令式措辞可免问"）随目标改走**台账编号**而失效：编号是模型从帧里抄的，系统判不出
    # "主人这句话是不是在命令它办那一件"。而两向都是**以主人名义对访客留言的裁决**（还有
    # 一条以主人名义发出去的额度通知），误判方向的代价不对称 ⇒ 与公告/删留言同族。
    # 这条锁的是**别看它长得像 set_article_status 就把它挪出去**。
    check("留言审核在「一律弹窗」表里（目标由模型抄台账编号，系统判不出命令语）",
          "audit_board_comment" in authz._ALWAYS_CONFIRM_TOOLS)
    check("后台写属于需确认 scope", authz.SCOPE_WRITE_CONSOLE in authz.CONSENT_SCOPES)
    check("写自己的私有数据属于需确认 scope", authz.SCOPE_WRITE_OWN in authz.CONSENT_SCOPES)
    check("页面/设备写不需要确认（效果就在用户眼前）",
          authz.SCOPE_WRITE_PAGE not in authz.CONSENT_SCOPES
          and authz.SCOPE_WRITE_DEVICE not in authz.CONSENT_SCOPES)
    check("server 侧写工具（若将来有）只由声明驱动，不需改函数",
          "CONSENT_SCOPES" in inspect.getsource(authz.requires_consent))
    # 用注册在册的 write.own 工具把真实分支跑一遍。**不造探针条目**：write.own 的判据
    # 是按工具查家族表的（未登记的工具恒 False，fail-closed），所以判据本身就要求用真名。
    sec = p(ROLE_USER)
    check("需确认工具：无确认语 → 未获同意",
          authz.requires_consent(sec, "add_favorite")
          and not authz.consent_granted(sec, "add_favorite", "帮我看下这篇文章写了啥"))
    check("需确认工具：明确命令语 → 获准",
          authz.consent_granted(sec, "add_favorite", "把文章 12 收藏一下"))
    check("确认语看的是**本轮消息**，空消息/None 一律不认",
          not authz.consent_granted(sec, "add_favorite", "")
          and not authz.consent_granted(sec, "add_favorite", None))
    check("普通聊到『收藏』不算确认（须是明确的命令说法）",
          not authz.consent_granted(sec, "add_favorite", "收藏功能是怎么做的？")
          and not authz.consent_granted(sec, "add_favorite", "你上次收藏的那篇写得不错"))
    check("确认是**按工具**判的：命令了收藏不放行取消收藏（防误靶写）",
          authz.consent_granted(sec, "add_favorite", "把文章 12 收藏一下")
          and not authz.consent_granted(sec, "remove_favorite", "把文章 12 收藏一下"))
    check("确认是用户的事，与 principal 无关（无声明 scope 的工具不受影响）",
          not authz.requires_consent(sec, "list_notes")
          and authz.consent_granted(sec, "list_notes", "把文章 12 收藏一下") is False)
    frame = authz.consent_frame("add_favorite", sec)
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
    # **存原值再还原**，别写死一张集合（20260923 踩到）：这段探针此前在 finally 里
    # 硬编码一张两元素的 frozenset 还原，于是后面新增一个需确认的 scope 时，这段
    # 代码会**把新成员悄悄抹掉**——而它自己的"清理干净"断言比对的也是那张写死的
    # 集合，所以抹掉这件事在测试里完全看不出来（表现是本文件下面所有用
    # consent_granted 的段落突然全红）。还原就该还原成"进来时的样子"。
    orig_consent = authz.CONSENT_SCOPES
    authz.CONSENT_SCOPES = frozenset({fake_scope})     # 临时换一组"需确认但没配语表"
    try:
        check("需确认却没配确认语表 → fail-closed（不默认放行）",
              authz.requires_consent(sec, "_probe_noconsent")
              and not authz.consent_granted(sec, "_probe_noconsent", "把文章 12 收藏一下"))
    finally:
        authz.CONSENT_SCOPES = orig_consent
        authz._CONSENT_PATTERNS = orig_patterns
        del authz.TOOL_SCOPE["_probe_noconsent"]
    check("探针条目清理干净", "_probe_noconsent" not in authz.TOOL_SCOPE
          and authz.CONSENT_SCOPES == orig_consent)

    print("⑨c 后台写确认语（命令式判据：判『本轮有没有明确命令』，不是第二次确认）")
    # 与 write.own 的差异（20260923）：那里的判据是一个**谓词**（按工具查家族表），这里
    # 是一条**判据函数**（_console_command）——因为后台写的命令有无数种说法，但
    # "什么算命令"是可判的：必须有动作词 + 目标 + 命令句式，且排除疑问/假设。
    # 误判方向不对称：判成"命令"= 直接动生产数据；判成"不是命令"= 多问一句。故 fail-closed。
    #
    # 20261015 批 5：判据从"共享词表 + 工具无关"改成**按工具名判**（每个工具声明自己认
    # 哪些动作词，`_CONSOLE_TOOL_ACTIONS`）。所以正例表改成 **(工具, 句子)** 对——一句
    # 话只对它自己那个工具成立，这正是本批要的性质（旧版任何工具撞上任何动作词都算命令，
    # 「把分类「X」改名叫「Y」」对 delete_category 也判成立 = 误靶写）。
    CONSOLE_POS = [
        ("set_article_status", "把文章 12 设为私密"),
        ("set_article_status", "把文章 12 设为私密。"),
        ("set_article_status", "帮我把《架构文档》隐藏起来"),
        ("set_article_status", "把文章 12 置顶"),
        ("set_article_status", "取消文章 12 的置顶"),
        ("set_article_status", "发布文章 12"),
        ("set_article_status", "把这篇设为草稿"),
        ("set_article_tags", "请把文章 12 的标签改成 Python 和 架构"),
        ("set_article_tags", "给文章 12 打上标签 Python"),
        ("set_article_tags", "把文章 12 的标签去掉"),
        ("create_tag", "帮我建一个标签：Python"),
        ("create_tag", "新建一级标签「测试」"),
        # 20260922：后台标签/分类写域的两个说法。活体探针腿⑭ 实测「把分类「X」改名叫
        # 「Y」」**命不中快道**（表里只有「改名为」，没有「改名/改名叫」）⇒ 用户明明下的是
        # 命令却落进弹窗那条路。命令快道是 fail-closed 的（判错只是多问一次），但"常用
        # 说法不在表里"不是保守，是缺口——这两条钉住它。
        ("update_category", "把分类「随笔」改名叫「碎笔」"),
        ("update_tag", "把标签「Asyncio」改名成「异步」"),
        ("update_tag", "给标签「Python」改名叫「蟒蛇」"),
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
    _bad_pos = [(t, m) for t, m in CONSOLE_POS
                if not authz.consent_granted(p(ROLE_ADMIN), t, m)]
    _bad_neg = [m for m in CONSOLE_NEG
                if authz.consent_granted(p(ROLE_ADMIN), "set_article_status", m)]
    check(f"后台写命令语认得（{len(CONSOLE_POS)} 条，各自判给自己的工具）",
          not _bad_pos, f"漏判: {_bad_pos}")
    check(f"疑问/假设/闲聊不是命令（{len(CONSOLE_NEG)} 条）", not _bad_neg, f"误判: {_bad_neg}")
    # 本批的**主线**（取代旧的「三种工具共用」那条锁——那条钉的正是要改掉的性质）：
    # 同一句命令，写对了工具才成立，写错了工具必须判 False（否则 planner 误靶选工具时，
    # scope 级同意会把它放过去直接写生产数据）。
    check("判据按工具判：『把文章 12 设为私密』只对 set_article_status 成立",
          authz.consent_granted(p(ROLE_ADMIN), "set_article_status", "把文章 12 设为私密")
          and not authz.consent_granted(p(ROLE_ADMIN), "create_tag", "把文章 12 设为私密")
          and not authz.consent_granted(p(ROLE_ADMIN), "set_article_tags", "把文章 12 设为私密")
          and not authz.consent_granted(p(ROLE_ADMIN), "delete_category", "把文章 12 设为私密"))
    check("判据按工具判：『把分类「随笔」改名叫「碎笔」』不喂给 delete_category/delete_tag",
          authz.consent_granted(p(ROLE_ADMIN), "update_category", "把分类「随笔」改名叫「碎笔」")
          and not authz.consent_granted(p(ROLE_ADMIN), "delete_category", "把分类「随笔」改名叫「碎笔」")
          and not authz.consent_granted(p(ROLE_ADMIN), "delete_tag",
                                         "把分类「随笔」改名叫「碎笔」"))
    # 判据问的是"这句话是不是**这个工具**的命令"，少了工具那一半等于回到那张共享词表
    # ⇒ 不知道工具就判不了（fail-closed，走弹窗），与 `_own_command` 同一取向。
    check("不知道是哪个工具 ⇒ 判不了命令（tool=None 恒 False，不回退到共享词表）",
          authz._console_command("把文章 12 设为私密") is False)
    # 完备性锁（**派生**，不是手抄对照）：够得着快道的工具 = write.console − 一律弹窗那批，
    # 而它必须与"声明了动作词的工具"**逐一相等**。新加一件后台写工具时，要么进
    # `_ALWAYS_CONFIRM_TOOLS`（每次弹卡），要么必须有人写一行"它认哪些动作词"的声明——
    # 旧版没有这条：新工具静默继承整张共享词表，没人确认过它该认什么。
    check("动作词声明的键 == 够得着快道的工具（新工具不声明就红）",
          set(authz._CONSOLE_TOOL_ACTIONS) == set(authz.console_fastpath_tools()),
          f"多: {sorted(set(authz._CONSOLE_TOOL_ACTIONS) - set(authz.console_fastpath_tools()))} "
          f"少: {sorted(set(authz.console_fastpath_tools()) - set(authz._CONSOLE_TOOL_ACTIONS))}")
    check("一律弹窗的工具不在动作词声明里（给它们声明 = 声明一条走不到的路径）",
          authz.console_fastpath_tools().isdisjoint(set(authz.always_confirm_tools()))
          and not set(authz.always_confirm_tools()) & set(authz._CONSOLE_TOOL_ACTIONS))
    check("同意看的是本轮消息，与 principal 无关（user 说命令也不放行——权限在更前面拦）",
          not authz.check(p(ROLE_USER), "set_article_status").allowed)

    # ⑨d「确认…」短回声（20260921 生产实测）：agent 自己建议的说法、用户照抄一遍，
    # 此前一律判 False ⇒ 同意闸不放行 ⇒ planner 追问 ⇒ 用户再打一遍 ⇒ 又 False
    # （**没有出口**）。这一支只认句首的「确认 + 写动词」骨架，并排除打听尾。
    # 判据形状与 ⑨c 同一把尺子：(工具, 句子) 对——回声里那个动词也得是**这个工具的**。
    print("⑨d 「确认…」短回声（agent 建议、用户照抄的那句话必须放行）")
    CONFIRM_ECHO = [
        ("create_tag", "确认创建标签 测试"),
        ("create_tag", "确认创建标签 agent标签功能测试"),
        ("create_tag", "确认一下新建二级标签 测试"),
        ("set_article_status", "确认把文章 12 设为私密"),     # ① 把字结构
        ("set_article_status", "确认发布文章 12"),            # ② 动词紧跟确认
        ("set_article_status", "确认置顶文章 12"),
    ]
    _bad_echo = [(t, m) for t, m in CONFIRM_ECHO
                 if not authz.consent_granted(p(ROLE_ADMIN), t, m)]
    check(f"短回声判成命令（{len(CONFIRM_ECHO)} 条）", not _bad_echo, f"漏判: {_bad_echo}")
    check("短回声也按工具判：『确认把文章 12 设为私密』对 create_tag 不成立",
          not authz.consent_granted(p(ROLE_ADMIN), "create_tag", "确认把文章 12 设为私密"))
    CONFIRM_ECHO_NEG = [
        "确认一下文章 12 是不是私密",     # 打听（是不是）——回声骨架不许把它救活
        "确认一下这个标签能不能删",       # 能不能
        "确认创建标签需要多久",           # 打听尾（多久）
        "我想确认一下文章的状态",         # 无写动词
        "确认收到",                       # 无写动词
    ]
    _bad_echo_neg = [t for t in CONFIRM_ECHO_NEG
                     if authz.consent_granted(p(ROLE_ADMIN), "set_article_status", t)]
    check(f"打听不是命令（{len(CONFIRM_ECHO_NEG)} 条）",
          not _bad_echo_neg, f"误判: {_bad_echo_neg}")
    # 删除族的连写说法（「删掉标签」）刻意**不判命令**、走弹窗——旧版靠"词表恰好没长这几个
    # 词"达成，本批把它做成**声明里看得见**的空位（`delete_*` 的 lead/echo 是空元组）。
    check("删除族的连写说法仍不判命令（刻意走弹窗，不是缺口）",
          not authz.consent_granted(p(ROLE_ADMIN), "delete_tag", "把标签 Asyncio 删掉")
          and not authz.consent_granted(p(ROLE_ADMIN), "delete_tag", "删了 Asyncio 那个标签")
          and not authz.consent_granted(p(ROLE_ADMIN), "delete_category", "把分类「随笔」删掉"))

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
        # 20260925 句式化：表里的名词**裸着出现**时是内容里的普通词，不是疑问锚。
        # 生产事故 trace 20260924T234402 的原句在下面第一条（它让公告连着四轮没有
        # 执行途径，附「薛定谔的猫」那句编造在同一次诊断里）。
        "小猫咪替我发一个公告要求全体用户今晚不许熬夜，以你的口吻声明",
        "小猫咪替我发个公告，要求全体用户今晚务必早睡！",
        "帮我发个公告，提醒大家注意身体",
        "帮我发个公告说明今晚更新，注意提前保存",
        "发个公告，把流程和注意事项写进去，要求大家看完",
    ]
    _bad_q_neg = [t for t in Q_NEG if authz.is_question_like(t)]
    check(f"意图陈述不误判成提问（{len(Q_NEG)} 条）", not _bad_q_neg, f"误判: {_bad_q_neg}")

    # 句式化的**反面**（20260925）：锚必须落在句法位置上，所以下面这几条仍是提问——
    # 「的<名词>」「什么/哪些<名词>」都把名词放进了疑问短语里。这一支与上面那一支是同一
    # 条判据的两半，缺了它，"裸词全删"也能过（那会把真提问读成意图、白弹一张卡）。
    Q_POS_FORM = [
        "python 异步都有哪些注意事项和实践技巧",   # 生产实测（20260915T020047）
        "改文章状态的后果是什么",
        "设为私密的步骤有哪些",
    ]
    _bad_form = [t for t in Q_POS_FORM if not authz.is_question_like(t)]
    check(f"名词挂在句法位置上仍是提问（{len(Q_POS_FORM)} 条）",
          not _bad_form, f"漏判: {_bad_form}")

    # 两张表必须都在（20260925）：宽表只剩 `_own_command` 在用，它判错的方向是**多一次
    # 点击**，与弹窗分叉（少一张卡）相反——所以句式化只动了一支，别顺手把另一支也收窄。
    check("弹窗分叉用句式化表、写自己数据用宽表（两支的错向相反，不共用一张表）",
          authz._CONSOLE_INQUIRY_RE is not authz._CONSOLE_INQUIRY_BROAD_RE
          and not authz._CONSOLE_INQUIRY_RE.search("提醒大家注意身体")
          and bool(authz._CONSOLE_INQUIRY_BROAD_RE.search("提醒大家注意身体"))
          # 宽表放行的真命令仍必须放行（不是"两边都 False"的假分开）
          and authz._own_command("收藏这篇", "add_favorite"))
    check("空消息保守按提问走（无从判断时不弹窗）", authz.is_question_like("")
          and authz.is_question_like("   ") and authz.is_question_like(None))

    print("⑨f 系统消息壳对判据透明（生产消息带 `[当前问题]: ` 前缀）")
    # 生产实测（20260921）：`server.py` 的 `_build_messages` 给本轮用户消息加锚点 `[当前问题]: `，
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

    print("⑨g write.own（20260923）：写自己的私有数据（收藏/通知已读）")
    # 与其它写 scope 的取向差异（见 authz.py 的三处注释）：三档角色都授予、不进
    # _HARD_SCOPES（它写的是调用者自己的东西，且本来就能在页面上做 ⇒ 满足 shadow 的
    # "观测既有流量"前提）、不进 _ALWAYS_CONFIRM_TOOLS（弹窗一律问会挡掉日常的"收藏这篇"）。
    check("write.own 是词汇表成员", authz.SCOPE_WRITE_OWN in authz.ALL_SCOPES
          and authz.SCOPE_WRITE_OWN in authz.WRITE_SCOPES)
    check("write.own 属于需确认 scope（写要有一句明确的命令）",
          authz.SCOPE_WRITE_OWN in authz.CONSENT_SCOPES)
    check("write.own **不**进 _HARD_SCOPES（自己账号里的写，真流量里本来就有）",
          authz.SCOPE_WRITE_OWN not in authz._HARD_SCOPES)
    check("write.own 的能力两档角色都有（角色轴在这件事上没有信息量）",
          # 显式列这两档而不是遍历 `KNOWN_ROLES`（20261002）：杂鱼是**刻意的零授予**，
          # 用 `all(... for r in KNOWN_ROLES)` 会把"杂鱼不该有工具"这条产品意图写成
          # "它也该有 write.own"。判据要表达的是"两档网站用户角色在这件事上同权"。
          all(authz.SCOPE_WRITE_OWN in authz.scopes_for(r)
              for r in (ROLE_USER, ROLE_ADMIN)))
    check("write.own 的确认语是**函数**（与 write.console 同族），不是词表",
          callable(authz._CONSENT_PATTERNS[authz.SCOPE_WRITE_OWN]))
    check("未获确认的说明写清了『写的是用户自己账号里的状态』",
          "自己" in authz._CONSENT_WHY[authz.SCOPE_WRITE_OWN][0])
    # 家族映射的**完备性**：TOOL_SCOPE 里声明 write.own 的工具必须登记家族名，
    # 否则 `_own_command` 一律判 False（fail-closed）——那是"能力静默不可用"，
    # 属于工程疏漏，必须在 CI 层被看见。两个方向都锁：声明了却没登记家族（判不出命令），
    # 以及登记了家族却没声明 scope（家族表里的死条目）。
    _OWN_TOOLS = sorted(n for n, s in authz.TOOL_SCOPE.items() if s == authz.SCOPE_WRITE_OWN)
    _unmapped = [n for n in _OWN_TOOLS if n not in authz._OWN_TOOL_FAMILY]
    check(f"write.own 工具都登记了动作家族（当前 {len(_OWN_TOOLS)} 个）", not _unmapped,
          f"未登记: {_unmapped}")
    check("动作家族表里没有死条目（登记的工具都真声明了 write.own）",
          set(authz._OWN_TOOL_FAMILY) <= set(_OWN_TOOLS),
          str(sorted(set(authz._OWN_TOOL_FAMILY) - set(_OWN_TOOLS))))
    check("未登记家族的工具 → 判不出命令（fail-closed，绝不『反正都是 own』就放行）",
          not authz._own_command("收藏这篇文章", "_probe_own_notmapped")
          and not authz._own_command("收藏这篇文章", None))
    # 刻意保留的边界（20260923 批 8）：标记**信**已读与标记**通知**已读是同一个动作
    # 家族，`_own_command` **认不出物件**——「把通知都标记为已读」对 read_messages 也返回
    # True。这不是遗漏而是分工：consent 只回答"这一轮主人下令了没有"，"下的是哪一件"
    # 由 planner 选技能决定（选错族由 golden 的术语分辨用例钉，见
    # own_message_read_not_logged_in 的 _note）。写成断言是为了让这条边界**可见**：
    # 哪天想把物件也纳入判据，这条会红，改的人得先想清楚为什么。
    check("同一动作家族覆盖两个工具（判据只认动作、不认物件——物件由 planner 选技能决定）",
          authz._OWN_TOOL_FAMILY["read_messages"] == authz._OWN_TOOL_FAMILY["read_notifications"]
          and authz._own_command("把通知都标记为已读", "read_messages")
          and authz._own_command("把私信都标记为已读", "read_notifications"))

    # 判据矩阵：正例（命令）/ 反例（提问、假设、陈述、名词用法、跨工具误靶）。
    _OWN_ADD, _OWN_RM, _OWN_RD = "add_favorite", "remove_favorite", "read_notifications"
    # 标记**信**已读（20260923 批 8）：与 read_notifications **同一个动作家族**
    # （"把 X 标记已读"）——物件（通知 / 信）不进判据，那是 planner 选技能的活；
    # consent 只回答"这一轮主人到底下令了没有"。这条边界是刻意的，下面有断言锁着。
    _OWN_MSG = "read_messages"
    OWN_POS = [
        (_OWN_ADD, "收藏这篇文章"),
        (_OWN_ADD, "收藏一下这篇"),
        (_OWN_ADD, "帮我收藏《架构文档》"),
        (_OWN_ADD, "把文章 12 收藏一下"),
        (_OWN_ADD, "把这篇文章加入收藏"),
        (_OWN_ADD, "收藏 12"),
        (_OWN_ADD, "麻烦把这篇收藏起来"),
        (_OWN_RM, "取消收藏文章 12"),
        (_OWN_RM, "把文章 12 取消收藏"),
        (_OWN_RM, "把这篇文章从收藏里去掉"),
        (_OWN_RM, "把这篇从收藏里移出去"),
        (_OWN_RM, "把收藏里的文章 12 删掉"),
        (_OWN_RM, "移除收藏《架构文档》"),
        (_OWN_RD, "把通知都标记为已读"),
        (_OWN_RD, "标记已读"),
        (_OWN_RD, "全部标记为已读"),
        (_OWN_RD, "帮我标记为已读"),
        (_OWN_RD, "把未读的都标为已读"),
        (_OWN_RD, "未读的都标记为已读"),
        # 信族（20260923 批 8）：同一动作家族，**叫法要收全**（少了「私信」这句最自然的
        # 说法就漏判、落回弹窗；本批实测过一次）。物件不参与判据——那一层由 planner
        # 选技能决定（见下面那条"家族覆盖两件"的断言）。
        (_OWN_MSG, "把私信标记为已读"),
        (_OWN_MSG, "我的私信都标成已读"),
        (_OWN_MSG, "站内信都标成已读"),
        (_OWN_MSG, "信件都标为已读"),
    ]
    OWN_NEG = [
        # 提问/打听（**"我收藏了哪些文章" 是最容易误判的一条**：有动作词、没有问号、
        # 也没有 吗/呢——量词表与结构判据各拦一半，单列出来）
        (_OWN_ADD, "我收藏了哪些文章"),
        (_OWN_ADD, "我的收藏在哪"),
        (_OWN_ADD, "收藏文章有什么用"),
        (_OWN_ADD, "收藏功能怎么用"),
        (_OWN_ADD, "收藏夹是干嘛的"),
        (_OWN_RM, "怎么取消收藏"),
        (_OWN_RD, "有未读通知吗"),
        (_OWN_RD, "我有几条未读"),
        (_OWN_RD, "未读通知在哪看"),
        (_OWN_RD, "通知怎么标记已读"),
        # 假设 / 陈述
        (_OWN_ADD, "如果我把文章 12 收藏了会怎样"),
        (_OWN_ADD, "我已经把这篇收藏了"),
        (_OWN_ADD, "这篇文章我收藏了"),
        # 名词用法（"收藏"是页面名，不是动词）
        (_OWN_ADD, "帮我看看收藏"),
        (_OWN_ADD, "收藏夹里有哪些"),
        (_OWN_RD, "给我看看未读通知"),
        (_OWN_RD, "把通知标记一下"),
        # 跨工具误靶：命令的是**另一个** own 动作 ⇒ 这一个不许判成命令
        (_OWN_ADD, "把通知都标记为已读"),
        (_OWN_RM, "标记已读"),
        (_OWN_RD, "收藏这篇文章"),
        (_OWN_RD, ""),
        # 信族的提问/陈述/名词用法（与通知族同形）
        (_OWN_MSG, "有未读私信吗"),
        (_OWN_MSG, "我信箱里有几封信"),
        (_OWN_MSG, "我的私信怎么看"),
        (_OWN_MSG, "我已经把私信标记为已读"),
        (_OWN_MSG, "把信标记一下"),
    ]
    _bad_own_pos = [f"{t}:{m}" for t, m in OWN_POS if not authz._own_command(m, t)]
    _bad_own_neg = [f"{t}:{m}" for t, m in OWN_NEG if authz._own_command(m, t)]
    check(f"own 写命令认得（{len(OWN_POS)} 条）", not _bad_own_pos, f"漏判: {_bad_own_pos}")
    check(f"提问/假设/陈述/名词/误靶不是命令（{len(OWN_NEG)} 条）", not _bad_own_neg,
          f"误判: {_bad_own_neg}")
    # 下面这几条走 `consent_granted`（判据的**入口**），要工具在 TOOL_SCOPE 里查得到 scope。
    # 20260923 批次 7 起这里走**真实声明**——三个工具已在 agent/authz.py 里登记 write.own，
    # 此前那段"临时探针条目 + finally 还原"的脚手架已删（它的存在只是因为当时工具还没
    # 落地；留着就会变成一层"测量的是探针而不是产品"的假绿灯）。
    _OWN_TOOL_NAMES = (_OWN_ADD, _OWN_RM, _OWN_RD)
    check("三个 own 工具就地声明为 write.own（不是靠探针挂上去的）",
          all(authz.required_scope(n) == authz.SCOPE_WRITE_OWN for n in _OWN_TOOL_NAMES),
          str({n: authz.required_scope(n) for n in _OWN_TOOL_NAMES}))
    check("三个 own 工具都判得出『这句要不要确认』",
          all(authz.requires_consent(p(ROLE_USER), n) for n in _OWN_TOOL_NAMES))
    # 这一条是本轮**新加的那道判据**（一个 scope 挂多个工具时才有意义）：同一句话，
    # 对"它命令的那个工具"放行、对"它没命令的工具"拦住——否则 planner 填错工具时，
    # scope 级同意会照样放行（误靶写）。
    check("同一句话对不同 own 工具结论**相反**（同意闸看得到工具名）",
          authz.consent_granted(p(ROLE_USER), _OWN_RD, "把通知都标记为已读")
          and not authz.consent_granted(p(ROLE_USER), _OWN_ADD, "把通知都标记为已读")
          and authz.consent_granted(p(ROLE_USER), _OWN_ADD, "收藏这篇文章")
          and not authz.consent_granted(p(ROLE_USER), _OWN_RD, "收藏这篇文章"))
    check("write.own 的确认与角色无关（三个角色看到同一结论）",
          all(authz.consent_granted(p(r), _OWN_ADD, "收藏这篇文章") for r in KNOWN_ROLES)
          and all(not authz.consent_granted(p(r), _OWN_ADD, "我收藏了哪些文章")
                  for r in KNOWN_ROLES))
    # 匿名：**同意闸刻意与 principal 无关**（它判的是"那句话是不是命令"，见
    # requires_consent 的头注），所以匿名时它也照样返回 True——拦住匿名的是**下一道**
    # 门（权限）与工具层的 `uid ≤ 0 一个请求都不发`。这条锁的正是这个分工：谁拦谁，
    # 不能靠"同意闸顺手把匿名也拦了"这种巧合（那会让 require/consent 两层的语义糊在一起）。
    check("匿名拦在权限那道门（不是靠同意闸顺手拦下的）",
          all(not authz.check(p(None), n).allowed for n in _OWN_TOOL_NAMES)
          and all(authz.check(p(ROLE_USER), n).allowed for n in _OWN_TOOL_NAMES))
    check("系统消息壳对 own 判据同样透明",
          authz.consent_granted(p(ROLE_USER), _OWN_ADD, _WRAP + "收藏这篇文章")
          and not authz.consent_granted(p(ROLE_USER), _OWN_ADD,
                                        _WRAP + "我收藏了哪些文章"))
    check("未确认帧写得出这次要确认的是什么（own 版本）",
          "自己" in authz.consent_frame(_OWN_ADD, p(ROLE_USER))
          and authz.consent_error_reason(authz.consent_frame(_OWN_ADD, p(ROLE_USER)))
          == authz.REASON_CONSENT)

    # 未确认帧的文案按**工具族**取（20260925）：按 scope 取的那三张是文章族口径
    # （"哪一篇、从什么变成什么"），而一律弹窗族里的公告根本不是"改哪一篇"。生产 trace
    # 20260924T234402 的代价实测到了：公告被挡时模型收到的正是那段文章族措辞，读成"内容
    # 不合规"，连着四轮改写公告正文。这一支锁三件事：公告族拿到的是公告的话、文章族措辞
    # 没被覆盖掉、这批工具**每个**都登记了文案（漏登记会静默退回文章族措辞）。
    check("一律弹窗族按工具族给文案（不是文章族那套「哪一篇」措辞）",
          "公告" in authz.consent_frame("create_announcement", p(ROLE_ADMIN))
          and "哪一篇" not in authz.consent_frame("create_announcement", p(ROLE_ADMIN))
          and "公告" in authz.consent_frame("delete_announcement", p(ROLE_ADMIN))
          and "留言" in authz.consent_frame("delete_board_comment", p(ROLE_ADMIN)))
    check("公告族文案把「不要重写」讲成下一步动作（否则重写正文仍是模型的默认出路）",
          "不要再改内容" in authz.consent_frame("create_announcement", p(ROLE_ADMIN)))
    check("一律弹窗的工具每个都登记了文案（漏登记会静默退回文章族措辞）",
          all(t in authz._CONSENT_WHY_TOOL for t in authz._ALWAYS_CONFIRM_TOOLS),
          str(sorted(authz._ALWAYS_CONFIRM_TOOLS - set(authz._CONSENT_WHY_TOOL))))
    check("文章族措辞没被覆盖掉（按 scope 取的那三张原样生效）",
          "哪一篇" in authz.consent_frame("set_article_status", p(ROLE_ADMIN))
          and "自己" in authz.consent_frame(_OWN_ADD, p(ROLE_USER)))

    print("⑨h 句首称呼壳对判据透明（20260930 同族第 4 例：主人开口就叫「小猫咪」）")
    # 第二层壳，**不是系统加的、是主人的说话习惯**：人设让它叫「小猫咪/泠月喵」，主人就真
    # 这么叫——全量生产语料 1110 条消息里 **252 条（22.7%）** 以称呼开头。底下判据全是
    # 句首锚定/整串相等型，称呼一在就恒不命中。实测翻正 6 条（同意闸）+ 8 条（弹窗分叉，
    # 全是「小猫咪！」这种只叫名字的轮次），其中 5 条是「小猫咪把…收藏了」——那 5 轮 trace
    # 里主人随后都点了「确定」把收藏办成了，即**此前是判据判不出、拿弹窗问一次**。
    # 称呼表只有一份（`authz._VOCATIVE_RE`）：导航快道与短应答里各内联过一份，都不含裸
    # 「猫咪」，同一句话在三处口径不同。
    _VOC_SAMPLES = [
        ("add_favorite", "小猫咪把这篇文章收藏了"),
        ("add_favorite", "小猫咪把我当前在读的文章收藏了"),
        ("add_favorite", "猫咪，把这篇文章收藏了"),
        ("read_notifications", "猫咪我的未读信息全部就标记为已读"),
        ("read_notifications", "小猫咪把未读通知标记为已读"),
        ("set_article_status", "小猫咪，把文章 12 设为私密"),
        ("create_tag", "小猫咪，新建一个标签叫 Python，用粉色"),
        ("set_article_status", "小猫咪，如果我把文章 12 设为私密呢"),
        ("read_notifications", "猫咪，我有几条未读"),
    ]
    _VOC_ADDR = re.compile(r"^\s*(?:(?:小|大|傻|臭|笨|乖)?(?:猫咪|猫猫)|泠月喵|泠月|喵喵|喵|主人)"
                           r"(?:\s*[，,、:：!！~～。.…]+\s*|\s+)?")
    _vdiff = []
    for _tool, _t in _VOC_SAMPLES:
        _s = _VOC_ADDR.sub("", _t, count=1)          # 剥称呼后的同一句话——判据必须给出同一结论
        for _name, _f in (("consent", lambda x: authz.consent_granted(p(ROLE_ADMIN), _tool, x)),
                          ("question", authz.is_question_like),
                          # 判据按工具判（20261015 批 5）：这一臂必须带上 `_tool`，否则
                          # `_console_command(x)` 恒 False、两边都是 False —— 一条**空转**的
                          # 透明性检查（本仓「假透明」老坑：两个臂都全绿不等于判据在动）。
                          ("console", lambda x: authz._console_command(x, _tool))):
            if _f(_t) != _f(_s):
                _vdiff.append(f"{_name}:{_t}")
    check(f"带称呼与不带称呼判定一致（{len(_VOC_SAMPLES)} 条 × 3 个判据）", not _vdiff,
          f"不一致: {_vdiff}")
    check("带称呼的明确命令**确实**判成命令（不是两边都 False 的假透明）",
          authz.consent_granted(p(ROLE_ADMIN), "add_favorite", "小猫咪把这篇文章收藏了")
          and authz.consent_granted(p(ROLE_ADMIN), "read_notifications",
                                    "猫咪我的未读信息全部就标记为已读")
          and authz.consent_granted(p(ROLE_ADMIN), "set_article_status",
                                    "小猫咪，把文章 12 设为私密"))
    check("带称呼的提问/假设/陈述**照旧**不是命令（剥壳不放宽任何一关）",
          not authz.consent_granted(p(ROLE_ADMIN), "read_notifications", "猫咪，我有几条未读")
          and not authz.consent_granted(p(ROLE_ADMIN), "add_favorite",
                                        "小猫咪，如果我把这篇收藏了呢")
          and not authz.consent_granted(p(ROLE_ADMIN), "read_notifications",
                                        "小猫咪，我已经把通知标记为已读")
          and not authz.consent_granted(p(ROLE_ADMIN), "add_favorite",
                                        "小猫咪，我收藏了哪些文章"))
    check("带称呼的假设仍判成提问（弹窗不许对着假设弹）",
          authz.is_question_like("小猫咪，如果我把文章 12 设为私密呢"))
    check("只叫名字的轮次按『无从判断不弹窗』走（剥完为空 ⇒ 提问）",
          authz.is_question_like("小猫咪！") and authz.is_question_like("小猫咪")
          and not authz.is_question_like("小猫咪，把文章 12 设为私密"))
    check("剥称呼只剥**开头**一处（句中/别处的名字不动）",
          authz._strip_address_shell("小猫咪，把 小猫咪 这篇收藏")
          == "把 小猫咪 这篇收藏"
          and authz._strip_address_shell("把文章 12 收藏") == "把文章 12 收藏")
    check("裸「猫咪」也算称呼（老表只有「小猫咪」——三处口径不同是这次的修因）",
          authz._strip_address_shell("猫咪去留言板") == "去留言板")
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

    print("⑨i 「在问我自己那份数据」的问句判据（20261003，gate 第 4b 节的输入）")
    # 现场（uid=1 会话 320，trace `20261003T194144`）：主人问「我有哪些未读通知呀」，planner 落
    # chat 零工具，narrator 回了一段关于上一轮话题的**真话** ⇒ gate 的判据族（全是"这句话真不
    # 真"型）全绿放行。这条判据补的是那半：**这轮答的是不是主人刚问的那件事**——只判"该不该
    # 去取数"，不管落到哪个技能（那是 planner 的事，同 `_replan_note` 的纪律）。
    # 射程由全量语料定（见 `authz.is_own_read_question` 头注）：uid>0 的零帧放行轮 299 轮命中
    # 2 条（一条是上面那次事故原句，一条是 20260923 的纠正轮）。下面这张成对表就是它的一部分。
    _ORQ_TRUE = [
        "小猫咪！我有哪些未读通知呀",        # 事故原句（称呼壳 + 量词型打听，两处都是坑）
        "我有哪些未读通知？",                # golden `own_unread_not_logged_in` 同一句
        "我信箱里有谁给我写过信？",          # golden `own_messages_not_logged_in` 同一句
        "我还有几条没读的消息呢",            # 量词型打听（问句表认不出，靠 `_OWN_INQUIRY_RE`）
        "我的收藏里有哪些文章",
        "我有哪些私信",
        "未读通知有多少条",                  # 无"我"：未读/通知自带自指（personal 档）
    ]
    _ORQ_FALSE = [
        ("你能发私信吗", "能力问句：如实答「不能」就是出路"),
        ("你可以单独把一个消息标记已读吗", "能力问句（中间隔 11 字）"),
        ("确定不能走系统通知吗", "能力问句（「不能…吗」）"),
        ("会不会有人给我发私信", "能力/假设问句：归假设那一支"),
        ("能不能替我发个公告", "能力问句 + 写意图（整句没有「吗」）"),
        ("猫咪我的未读信息全部就标记为已读", "写命令：归写侧（洞⑨ 那族）"),
        ("把通知都标记成已读", "写命令"),
        ("站内最近有什么公告吗？", "公开面：golden `data_announcements` 同形，不是我自己那份"),
        ("读取站内通知读不到吗", "「通知」是通用词、句里没有自指"),
        ("给xinguan什么的用户发个测试通知", "写意图，且问的是别人的数据"),
        ("小猫咪你会做蛋糕吗", "闲聊"),
        ("小猫咪按你想法来吧", "闲聊"),
        ("", "空消息"),
    ]
    _ORQ_MISS = [t for t in _ORQ_TRUE if not authz.is_own_read_question(t)]
    check(f"问自己数据的问句判成「该去取数」（{len(_ORQ_TRUE)} 条）", not _ORQ_MISS,
          f"漏判: {_ORQ_MISS}")
    _ORQ_BAD = [f"{t}（{why}）" for t, why in _ORQ_FALSE if authz.is_own_read_question(t)]
    check(f"不该去取数的句子放行（{len(_ORQ_FALSE)} 条）", not _ORQ_BAD, f"误判: {_ORQ_BAD}")
    # 称呼壳透明（同 ⑨h 第 4 例）：判据入口只剥一次壳，带不带「小猫咪」必须同结论。
    _ORQ_VOC = [(t, authz.is_own_read_question(t), authz.is_own_read_question("小猫咪，" + t))
                for t in ("我有哪些未读通知呀", "我的收藏里有哪些文章")]
    check("称呼壳对这条判据透明（带不带你叫它，判定一致）",
          all(a == b for _t, a, b in _ORQ_VOC),
          str([t for t, a, b in _ORQ_VOC if a != b]))
    # 与**写侧**同源但方向相反：同一句话在两侧的结论必须互斥（否则一句写命令会被读成问句，
    # 白白打回一轮，或者反过来——写请求被这条路放过去）。
    _ORQ_SAME = [t for t in _ORQ_TRUE
                 if any(authz._own_command(t, tool) for tool in authz._OWN_TOOL_FAMILY)]
    check("两侧互斥：判成「问」的句子不会被写侧同时判成命令", not _ORQ_SAME, str(_ORQ_SAME))

    print("⑨j 「话题落在站内语料上却零检索」的问句判据（20261004，gate 第 4c 节的输入）")
    # 要治的形状：主人问站内有些什么，narrator 回「我这边**没有工具**可以帮你查具体的文章标题和
    # 链接」——站里备着 `rag_search`/`search_notes`/`list_notes`。判据只判"该不该去检索一次"，
    # 不管落到哪个技能。
    # 射程（两遍，都是量出来的）：判据词面在 uid>0 的零帧放行轮上命中 **1**（就是下面那句）；
    # 而 **gate 里真正的射程是 0** ——那一条是**杂鱼**（uid=9），计划 status 是 `wrapped`，
    # 被调用侧 `status == "answer_only"` 守卫结构性排除。⚠️ 上线前这条判据的现场被写成
    # "杂鱼轮上 planner 偷懒没检索"，**那是读出来的误判**（更正说明见 `agent/authz.py` 的头注与
    # `tests/test_gate_replan.py` ⑥d/⑥e）：引用 trace 当现场必须连**走的哪条路径**一起核。
    # ⚠️ 下面这张表里**必须**留着 `followup_entity_slot_category` 的原句当反例——它与"真的要
    # 检索"字面完全同形（top1 都是 10.33），少一道「指代型」守卫就会直接打红一条现在绿的用例。
    _SCQ_TRUE = [
        "博客有哪些文章呢",                          # ★ 现场原句（**注意：生产里问这句的是杂鱼**——
                                                     #   本表判的是**判据词面**，不是"gate 会开火"）
        "站内有没有写 ESP32 的文章？",               # golden `multi_step_search_then_read_top` 同句
        "有没有 Docker 部署博客的教程？",            # golden `rag_noise_docker`
        "小猫咪，测试文章 TEST8 里画的是什么呀",       # golden `rag_test4_cover`
        "站内有讲量子力学的文章吗",  # 语料里没有 ⇒ **照开火**：该查一次再如实说没有
    ]
    _SCQ_FALSE = [
        ("编程那个分类下面有几篇文章？",
         "指代/取值型：按 rule 6b 照抄跨轮摘要，零工具才对"
         "（golden `followup_entity_slot_category` 原句，现在判绿）"),
        ("那个分类下面有几篇文章？", "同上（golden `followup_entity_slot_ambiguous`）"),
        ("我有哪些未读通知呀", "自指数据：归 `is_own_read_question` 那一族"),
        ("读取站内通知读不到吗", "自指数据（上一轮的读取结果，不是站内语料）"),
        ("你能编辑文章吗", "能力问句：如实答「不能」就是出路"),
        ("你用哪些工具读文章", "元问句：问的是「你有什么」，不是「给我一份数据」"),
        ("好猫猫，亲一个", "闲聊"),
        ("把这篇文章收藏了", "写命令：归写侧"),
        ("", "空消息"),
    ]
    _SCQ_MISS = [t for t in _SCQ_TRUE if not authz.is_site_corpus_question(t)]
    check(f"站内语料问句判成「该去检索」（{len(_SCQ_TRUE)} 条）",
          not _SCQ_MISS, f"漏判: {_SCQ_MISS}")
    _SCQ_BAD = [f"{t}（{why}）" for t, why in _SCQ_FALSE if authz.is_site_corpus_question(t)]
    check(f"不该去检索的句子放行（{len(_SCQ_FALSE)} 条）", not _SCQ_BAD, f"误判: {_SCQ_BAD}")
    # 与私有面那一族互斥：同一句不许两边都开火（都开火只会让第一次打回指向两个方向）。
    _SCQ_BOTH = [t for t in _SCQ_TRUE if authz.is_own_read_question(t)]
    check("两面互斥：判成「站内语料」的句子不会被私有面同时判成「我自己那份」",
          not _SCQ_BOTH, str(_SCQ_BOTH))
    # 称呼壳透明（同 ⑨i）：判据入口只剥一次壳。
    _SCQ_VOC = [(t, authz.is_site_corpus_question(t),
                 authz.is_site_corpus_question("小猫咪，" + t))
                for t in ("博客有哪些文章呢",)]
    check("称呼壳对这条判据透明（带不带你叫它，判定一致）",
          all(a == b for _t, a, b in _SCQ_VOC),
          str([t for t, a, b in _SCQ_VOC if a != b]))

    print()
    assert not FAILS, f"{len(FAILS)} 条未过：" + "；".join(FAILS)
    print("全部通过")


if __name__ == "__main__":
    test_authz()
