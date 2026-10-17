# -*- coding: utf-8 -*-
"""技能注册表的**字面量层**：47 条 `Workflow(...)` 按域分住六个模块，这里保序拼回。

**为什么按域拆**：注册表字面量占了 `agent/workflows.py` 的 36%（1691 行），是「改动不断
叠进同一个文件」的典型现场——加一条技能得在 1700 行里找位置。按域拆之后，每条技能只跟
同域的那几十行住一起。

**`WORKFLOWS` 的顺序是一份契约**：它驱动 `WORKFLOW_MAP`、planner 菜单的排列、以及
`visible_workflows` 的产出顺序——**顺序变了就是提示词字节变了**，那是要 golden A/B 才
敢做的事，而批 2 是纯结构改动（无行为变更，所以不跑 golden）。所以六个域模块的选取标准
不是「主题好看」，是**它们在原序列里各自连续**：导航/特效/夜间/设备（1–5）、公开读（6–7）、
报表与看板（8–14）、管理助手内容面（15–28）、主人自己的数据与待办（29–36）、账号/配额/
变更集（37–46）。第 47 条 `chat` 没有域（它是「其余全部」的兜底技能，展开层里那一支也是
「谁都不匹配就落 chat」），就地写在下面——塞进任何一个域模块都得给那个模块起一个它不配的名字。

拼回之后**逐条相同**：判据读的是 `WORKFLOWS` 本身（`tests/test_registry_sync.py`、
`test_workflows.py`），不读源码文本，所以「顺序没动」这件事靠名单本身守，不靠注释。

本模块还承接注册表的**派生件**：写技能的靶通道表与四个桶、写名单、可见性判据。它们都只
读 `WORKFLOWS` 那一张表，与六个域模块同层、同住这个包。"""

from __future__ import annotations

from agent.authz import SCOPE_WRITE_CONSOLE, SCOPE_WRITE_OWN, TOOL_SCOPE
from agent.principal import CHAT_ONLY_ROLES
from agent.workflow_defs import (account_write, content_write, navigation, own_todo,
                                 public_read, reports)
from agent.workflow_model import Workflow
from tools.base import IOT_ENABLED

__all__ = [
    "WORKFLOWS",
    "WORKFLOW_MAP",
    "WRITE_WORKFLOW_NAMES",
    "visible_workflows",
]


# ── 写技能的**靶通道**：一处声明，写名单与四个桶都从它派生（20261015 批 1）────────
#
# 从前这里有**两份必须手工同步的表**：写技能名单（`WRITE_WORKFLOW_NAMES`，30 个名字手抄）
# 与"这个技能的靶出自哪个通道"（四个 frozenset，28 个名字手抄；剩下两件硬写在展开链的
# `else` 里）。漂移的后果不是"多打几个字"，是**静默的**：
#   · 新写技能没进名单 ⇒ 它根本不进 `instantiate_plan` 的写分支（那条分支的入口就是
#     名单），于是落进通用模板分支，把 `{"ids": null}` 这种"零范围"实参原样展开成一次写；
#   · 靶通道漏了 ⇒ 落进 `_expand_write_workflow` 的尾部兜底，零工具零写还不报错，主人只
#     看到"什么都没办"。
#
# 现在只有**一处声明**：下面这张 `{技能名: 靶通道}`。写名单由"这个技能的计划里有没有
# 一个写作用域的工具"派生（`authz.TOOL_SCOPE` 是作用域的单一事实源，见
# `WRITE_WORKFLOW_NAMES` 那条注），四个桶由通道值派生。加一件写技能 = 在注册表里写它的
# `plan` + 在这张表里声明通道；**漏了后者 ⇒ 展开器响亮拒绝**（`instantiate_plan` 里
# `_chan is None` 那一支），不再静默零写。
#
# 通道的判据是"**靶从哪来**"，不是"写什么"：
#   name      靶是主人原话里**点名叫出来的一个站内名字**（标签/分类/公告标题/账号名/
#             留言正文片段），由工具对着站内实时字典核对；名字不在名录里 ⇒ 零写 + 如实说。
#   ledger    靶是**现场台账里那一行的编号**（`talkId:<n>` / `账号 id=<n>`）——主人说的
#             是「全部批准」，那一行是谁只有系统摊开的那份台账说得清。比 name 更强：
#             可验证、不可能编造（校验在 `graph._ledger_target_refusal`）。
#   own       靶是主人**自己账号里**的数据，scope 是 write.own 而不是 write.console、
#             roles 不设限，且有"无范围就零工具"这条硬判据（`_expand_own_workflow` 头注）。
#   free_text 靶是**一段自由文本**（待办正文 / 排期日）——站内没有东西可核对，判据只有
#             "正文在不在、日期说清楚没有"。
#   article   靶是 article_id，但**由系统从上下文取**而不是主人的原话片段（"把这篇
#             设为私密"），scope 也是 write.console（管理助手面）。
_WRITE_TARGET_CHANNEL: dict[str, str] = {
    # ── name：站内既有名字（标签 / 分类 / 公告标题 / 账号名 / 留言正文片段）──────
    "tag_create": "name",
    "tag_update": "name",
    "tag_delete": "name",
    "category_create": "name",
    "category_update": "name",
    "category_delete": "name",
    "announcement_create": "name",
    "announcement_update": "name",
    # 公告认**标题**（不是正文）——工具侧对着文章/公告名录核对。
    "announcement_delete": "name",
    # 留言删除（20260922 第六轮）：留言没有名字/标题，主人说的就是那句话本身 ⇒ 名字通道的
    # 留言版（靶参数 quote，唯一子串命中在工具侧判）。它**没有**跟复核那件一起改成按
    # talkId（20260929 批 H）：已通过/已驳回的留言不在待办台账里，只认编号会让"删掉那条
    # 老留言"可能连编号都拿不到，见 `tools.base.delete_board_comment` 的 docstring。
    "board_delete": "name",
    # 账号冻结/解冻（20260926 第九轮）：planner 写名字，工具对着后台账号名录解析
    # （`tools.base._find_named_user`）。**刻意不开编号通道**：后台列表不列超管那一行
    # （src/routes/temp_user.rs），这条防线只在"定位必须经过列表"时成立——给一个 user_id
    # 参数就等于从第二扇门把"冻结一个看不见的超管"打开。
    "account_freeze": "name",
    "account_unfreeze": "name",
    # 给单个账号发通知（20260926 第十一轮）：同一条通道（`_find_named_user`）。与
    # `notice_read`（标记**主人自己**的通知已读，走 own）**不是一对**——那一件动的是自己
    # 账号里的数据，这一件动的是**发给别人的一段话**；名字近形，别把两个技能名读成一族。
    # 正文虽是自由文本，但它**不是这个技能的靶**（靶是收件人）⇒ 不进 free_text。
    "notice_send": "name",
    # 主动重置额度（20260929）：同一条通道。**只有这一件留在名字通道**——它不需要对方
    # 申请过（"你替他决定"），所以靶不在任何队列里、没有系统印的编号可抄；同一族的
    # quota_approve / quota_reject 治的是"申请表里等着办的那一行"，见 ledger。
    # 三件而不是一件带方向的参数：方向必须写进动作本身（批准=清零且不可撤销 / 驳回=额度
    # 不变但会通知他 / 重置=不需要他申请过），卡面才不可能与真正执行的方向相反。
    "quota_reset": "name",
    # 变更账号身份（20261002）：靶同样是后台名录里的一个账号名。与冻结族有一处不同：
    # 方向不在技能名里，而在参数的**取值**（`role`）——所以卡面必须把"从什么身份 →
    # 什么身份"印出来（那不是锦上添花，是这一支唯一的复核点，见 adminops `_ROLE_*` 头注）。
    "account_set_role": "name",
    # 禁言 / 解除禁言（20261002）：靶同样是后台名录里的一个账号名。与冻结族唯一的结构
    # 差别是禁言方向多一个**取值参数** `hours`（禁多久）；它**不是**"要写进去的自由文本"，
    # 而是一次归一（`adminops.normalize_mute_hours`：主人说的「三天」→ 72），归不出的字面
    # **零工具 + 问清**——绝不许静默按永久办（那会把"关他三天"办成永久禁言，卡面与实际
    # 同源、两边都看不出来）。
    "mute_account": "name",
    "unmute_account": "name",
    # ── ledger：靶是现场台账里那一行的**编号** ────────────────────────────────
    # 河灯留言复核（20260929 批 H）：靶 talk_id，编号印在台账帧 `talkId:<n>` 与审核状况
    # 明细上。
    "board_audit": "ledger",
    # 额度申请的两件（20260929 批 H）：靶 user_id，编号印在台账帧 `账号 id=<n>` 与
    # `list_quota_requests` 的行上（quota_reset 不在这一桶，理由见上）。
    "quota_approve": "ledger",
    "quota_reject": "ledger",
    # 审核队列的**变更集**（20260929 批 F；批 H 交回模型）：一次点头办 N 件，可跨族
    # （留言复核 + 额度批准）。它的 `params` 是**逐条调用清单**
    # （`{"calls":[{"tool","args"}]}`，与 `content_query.calls` 同构，见
    # `_expand_change_set`），因此展开函数**不是** `_expand_write_workflow`。它的 `plan`
    # 声明的是**允许出现在变更集里的工具全集**（`_confirm_grant_plan` 按它逐条核对
    # "技能与工具对得上"），不是一套固定序列模板——它同时是跨族安全性的来源：别的写
    # 工具名拼不进 `calls`。
    "review_inbox": "ledger",
    # ── own：靶是主人**自己账号里**的数据 ─────────────────────────────────────
    # 收藏两件（article_id）+ 标记已读（通知 id 列表 / "全部"），共用 `_expand_own_workflow`
    # ——"无范围就零工具"那条硬判据写在它的头注里。
    "favorite_add": "own",
    "favorite_remove": "own",
    "notice_read": "own",
    # 站内信标记已读（20260923 批 8）：与 notice_read 同一形状、另一个物件。
    "message_read": "own",
    # ── free_text：靶是一段**自由文本**，站内没有东西可核对 ───────────────────
    # 后台首页待办 / 日程（20260926 第八轮起三件）。⚠️ 这一桶**不止一个展开函数**
    # （第十轮起两件、批 G 起三件）：通道只说"靶是自由文本"，而"加一条 / 勾一条 / 改一条
    # 的排期"是三件不同的事 ⇒ `instantiate_plan` 里按**技能名**三分。漏了那一步的后果是
    # 静默的："勾完成"会被 `_expand_todo_workflow` 展开成 `create_dashboard_todo(...)`，
    # **多记一条待办**（比零工具危险得多：主人看到成功的回执，列表里却悄悄多了一行）；
    # "改排期"更隐蔽——展开成 `create_dashboard_todo({'text':…,'date':…})`，于是"把简历
    # 挪到 10 月 8 号"变成**又记一条简历**，而主人以为日期改好了。
    # `dashboard_todo_list` **不在**这里：它是读（admin.console），把它写进来会让它落进
    # 写分支的参数展开，产出一次不成形的写。
    "dashboard_todo_add": "free_text",
    "dashboard_todo_done": "free_text",
    "dashboard_todo_reschedule": "free_text",
    # ── article：靶是 article_id，但**由系统从上下文取** ──────────────────────
    # 两件**就地展开**（不走 `_expand_write_workflow`）：它们的目标是 article_id、另有
    # "点名即据"的判据，且 `article_tags` 要加的标签站内还没有时会先 `create_tag`
    # （条件工具，见 `Workflow.extra_plan_tools`）。
    # 这一桶从前的名字**长在展开链里**（`article_status` / `article_tags` 两个字符串硬写在
    # fail-closed 那句排除项里），20261015 批 1 起显式成桶。
    "article_status": "article",
    "article_tags": "article",
}


def _channel(*want: str) -> frozenset[str]:
    """靶通道的值 → 技能集合（下面四个桶共用；桶的成员**只能**从那张表来）。"""
    return frozenset(k for k, v in _WRITE_TARGET_CHANNEL.items() if v in want)


# 四桶 = 上面那张表的**视图**。名字与成员资格与从前逐名相同——`graph.py` 的可见性闸与
# 八个套件的锁都读它们（`tests/test_todo_schedule.py` 直接断言 free_text 那一桶）。
_WRITE_NAME_TARGET_WORKFLOWS = _channel("name")
_LEDGER_TARGET_WRITE_WORKFLOWS = _channel("ledger")
_OWN_WRITE_WORKFLOWS = _channel("own")
_FREE_TEXT_WRITE_WORKFLOWS = _channel("free_text")


# ---------------------------------------------------------------------------
# 保序拼回：**顺序是一份契约**（见文件头注）
# ---------------------------------------------------------------------------
# 六个域模块在原序列里各自连续，所以「按域展开」与「逐条抄一遍」产出同一份名单。
WORKFLOWS: list[Workflow] = [
    *navigation.WORKFLOWS,      # 1–5   导航 / 特效 / 夜间 / 设备
    *public_read.WORKFLOWS,     # 6–7   公开读
    *reports.WORKFLOWS,         # 8–14  报表与看板
    *content_write.WORKFLOWS,   # 15–28 管理助手内容面（后台清单 + 十三件写）
    *own_todo.WORKFLOWS,        # 29–36 主人自己的数据与待办
    *account_write.WORKFLOWS,   # 37–46 账号 / 配额 / 变更集
    # 47    兜底技能（无域，见文件头注）
    Workflow(
        name="chat",
        capability="闲聊、陪你说话",
        description="闲聊、问候、情感交流、纯文字问答（不需要任何工具）时使用。",
        inputs={},
        plan=[],
        complete_when="给出回答",
        reply_contract="直接回答",
        chat=True,
    ),
]


WORKFLOW_MAP: dict[str, Workflow] = {s.name: s for s in WORKFLOWS}


# ── 写技能名单 = **派生**，不再手抄（20261015 批 1）───────────────────────────────
#
# 判据 = "这个技能的计划里有没有一个**写作用域**的工具"（`authz.TOOL_SCOPE` 是作用域的
# 单一事实源）。手抄名单的病不是"多打几个字"，是**漏掉的后果是静默的**：新写技能没进
# 名单 ⇒ 它根本不进展开器的写分支（那条分支的入口就是名单）⇒ 落进通用模板分支，把
# `{"ids": null}` 这种"零范围"实参原样展开成一次写。派生之后这条洞按构造不存在。
#
# ⚠️ 刻意**不用**广义的 `authz.is_write()`：那个谓词还含 `write.page`（导航/特效/夜间）
# 与 `write.device`（屏幕显示）——那四件动的是主人的**浏览器**、不是站内数据，混进写分支
# 反会被写族的"缺参零工具"纪律拦成办不成。实测：广义谓词多出 navigate / effect /
# darkmode / device_display 四件；收窄到这两个 scope 才与从前的手抄名单**逐名相等**
# （30 件，`tests/test_write_channel.py` 有锁）。`extra_plan_tools` 里的写工具也算
# ——`article_tags` 的 `create_tag` 就在那一格。
_WRITE_SCOPES = frozenset({SCOPE_WRITE_CONSOLE, SCOPE_WRITE_OWN})


def _derive_write_workflow_names(workflows: list[Workflow]) -> frozenset[str]:
    """注册表 → 写技能名单（**收一个参数而不是直接读 `WORKFLOWS`**：这样反控能喂它一个
    假的"新写技能"，证明"新技能自动进名单"这条性质真的成立，见
    `tests/test_write_channel.py` ⑧）。"""
    return frozenset(
        s.name for s in workflows
        if any(TOOL_SCOPE.get(n) in _WRITE_SCOPES
               for n in [n for n, _ in s.plan] + list(s.extra_plan_tools)))


WRITE_WORKFLOW_NAMES = _derive_write_workflow_names(WORKFLOWS)


# 系统专用技能：**不由 planner 选、也不对外介绍**的技能名。
# 现在只剩一个：`read_article`——它的 article_id 是 current_url 解析出来的系统数据，
# planner 无参可填。
# （`review_inbox` 批 F 曾在这份名单里，理由是"模型拼不出一份跨技能变更集"；批 H 把
# 它**移出**了：拼不出清单的真相是**系统没把台账摆到它面前**，于是系统只好自己读
# 上一轮散文替它决定——那正是「没有一点 LLM 优势」的机制成因。改成把台账摊开、
# 让模型逐条拼 `calls`，闭集仍由 `_expand_change_set` 的 `plan` 白名单收着。）
# 收成一条名单的理由同 `visible_workflows` 自己：可见性判据只能有一处，两处必然漂移。
_SYSTEM_ONLY_WORKFLOWS = frozenset({"read_article"})

# 物联网平台**没装**时一并不可见的两件（20261002）：`device_display`（让 ESP32
# 屏幕显示文字）与 `device_query`（有哪些设备/在线状态）。平台没装 ⇒ 没有设备可
# 列、没有屏幕可写，留着它们就是两份"能做"的承诺（`capability` 会原样进 narrator
# 的能力清单，planner 也会照选）。收在 `visible_workflows` 这一处，理由同上一行：
# **可见性判据只能有一处**——planner 注入、接口层的 tools schema、narrator 的
# 能力清单三处都从它派生。
# ⚠️ 只影响**可见性**，不动工具注册表：`tools/base.py` 里那两个工具照旧注册着
# （`_TOOL_MAP`、authz 的写操作判定、gate 的具名工具声称核对都还引用它们）。
_IOT_ONLY_WORKFLOWS = frozenset({"device_display", "device_query"})


def visible_workflows(role: str | None, include_system: bool = False) -> list[Workflow]:
    """按角色过滤的技能列表——**角色可见性判据只有这一处**（20260921）。

    为什么收成一条：planner 注入（build_planner_context）与 narrator 的能力清单
    （context.site_guide）此前各写各的可见性——两张表必然漂移，而它们回答的是
    同一个问题（"这个人能用什么"）。165525-165937 同一能力三轮两种答案就是这么来的。

    `include_system=True` 才带上系统专用技能（`_SYSTEM_ONLY_WORKFLOWS`：article_id 是
    current_url 解析的系统数据，planner 无参可填、narrator 也不该对外介绍）。
    `role=None`（身份不明/单测）→ 只剩公开技能，失败取向往保守一侧倒（同 authz）。

    **只准闲聊族**（20261002，`CHAT_ONLY_ROLES`：杂鱼）走的是**否决支**而不是给
    `Workflow.roles` 加值：公开技能（chat / content_query / navigate / …）的 `roles`
    是**空集**，而上面那条判据是"空集 = 对任何角色可见"（`s.roles and …`）——给杂鱼
    配 `roles` 根本拦不住它们。判据用 `s.chat`（Workflow 自己的字段，全仓只有 chat 技能
    为真）而不是 `s.name == "chat"` 字面量：名字会漂，字段不会。

    ⚠️ 这是**软**约束（它管的是"模型看到的菜单"，而 `instantiate_plan` 不校验可见性、
    authz 在生产 shadow 档下只记账不拦）。硬保证在 graph.planner_node 的短路。
    """
    out = []
    for s in WORKFLOWS:
        if s.name in _SYSTEM_ONLY_WORKFLOWS and not include_system:
            continue
        if s.name in _IOT_ONLY_WORKFLOWS and not IOT_ENABLED:
            continue
        if role in CHAT_ONLY_ROLES and not s.chat:
            continue
        if s.roles and role not in s.roles:
            continue
        out.append(s)
    return out
