# -*- coding: utf-8 -*-
"""账号冻结 / 解冻、禁言 / 解除禁言、后台账号名录与站内通知的卡面与回执。

批 3a 从 `agent/adminops.py` 按域拆出（原文逐字节保留）。"""

from __future__ import annotations

import re

from agent.adminops_tags import (
    clip,
)
from agent.adminops_target import (
    normalize_target_id,
)

# ── 账号冻结 / 解冻（20260926）───────────────────────────────────────────
# 这一族的措辞有**三条硬要求**（三条都不是文风问题，是"主人点确定之前能不能看懂
# 自己将同意什么"的问题）：
#   ① 账号名**一字不改**地进卡面——主人得能核对"是不是那个人"（同 `_board_ref`）；
#   ② 冻结与解冻**必须不同形**，差异要落在**后果**上而不是动词上（"被踢下线、连
#      登录都进不来" vs "能重新登录了"）；只换动词的两张卡读起来是同一个动作的
#      两个方向，而真正的区别是"有人被踢下线"这件事可不可逆；
#   ③ 解冻那句**不许**写成"撤销冻结 / 恢复原状"——那是假话：后端的 `token_version`
#      **只增不减**（父仓 `scripts/probe_token_revoke.py` 的【三】专门验这个），解冻恢复
#      的只是"能不能登录"。冻结也**不写**「不可撤销」（它可以解冻），要写清的是
#      **哪半边不可逆**：被踢掉的会话不回来。
_ACCOUNT_STATE_CN = {True: "已冻结", False: "正常"}
# 卡面/问句里的后果句（`render_account_action` 用）
_ACCOUNT_CONSEQ = {
    True: "他当前所有登录会话立刻失效，在他被解冻之前连登录都进不来",
    False: "他能重新登录了——冻结期间被踢下线的会话不会自动恢复，需要他自己重新登录",
}


def account_state_cn(frozen) -> str:
    """`status` → 人话状态词。**读不出（None）不当成"正常"**：那是把"没读到"说成
    事实（同 tools.base._account_frozen 那条纪律）。"""
    if frozen is None:
        return "状态未知"
    return _ACCOUNT_STATE_CN[bool(frozen)]


def _account_row(users, username):
    """账号名录快照里按**账号名**找回那一行；快照不在/名字不在 → None。

    `users` = `{uid: 行}`（tools.base._user_directory 的产物），**可能整个是 None**
    （读不到名录）——调用方必须把"快照是 None"与"快照在手、名字不在里面"分开渲染，
    这两件事给主人的信息不一样（一个是"没核对上"，一个是"后台根本没有这个账号"）。
    """
    if not users:
        return None
    want = str(username or "").strip()
    if not want:
        return None
    for row in users.values():
        if isinstance(row, dict) and str(row.get("username") or "").strip() == want:
            return row
    return None


def _account_row_by_id(users, user_id):
    """账号名录快照里按 **uid** 找回那一行（id 通道的对应物）；取不到 → None。

    三态与 `_account_row` 逐字同源（快照是 None / 快照在手但没这一行 / 找到了）。
    **id 认不出（`normalize_target_id` 返 None）也返回 None**：那种情况下渲染方
    连"是哪一行"都说不出来，只能如实标注没核对上——绝不退回去按名字猜。
    """
    if not isinstance(users, dict) or not users:
        return None
    uid = normalize_target_id(user_id)
    if uid is None:
        return None
    row = users.get(uid)
    return row if isinstance(row, dict) else None


def _quota_target_row(tool: str, a: dict, users):
    """额度族的目标 → `(快照行, 显示用名字)`；id 通道与名字通道在这里合流。

    目标通道按**工具**分（`reset_user_quota` 走账号名、批准/驳回走 uid），与
    `_confirm_one` 那一支逐字同源——两处判据必须同源，否则会出现"卡面说批的是
    Alice、这里的判据却在看另一个账号"的分裂（那正是 `reached_specs` 头注警告的形态）。
    行取不到时返回 `(None, 名字)`，由各臂自己决定"判不了"还是"如实说一句"。
    """
    if tool == "reset_user_quota":
        name = str(a.get("name") or "").strip()
        return _account_row(users, name), name
    row = _account_row_by_id(users, a.get("user_id"))
    shown = str(row.get("username") or "").strip() if isinstance(row, dict) else ""
    return row, shown


def _board_row_by_id(boards, talk_id):
    """留言清单快照里按 **talkId** 取那一行（id 通道的问句渲染）；取不到 → None。

    与 `tools.base._find_board_comment_by_id` 同一判据（清单里有没有这个 talkKey），
    但**只用于渲染**：None 只表示"问句里写不出具体是哪一条"（快照读不到 / id 认不出
    / 清单里没这一条）。真正的"能不能动"仍由规划轮的目标预检与工具侧的复核各判一次。
    """
    if not isinstance(boards, dict) or not boards:
        return None
    tid = normalize_target_id(talk_id)
    if tid is None:
        return None
    row = boards.get(tid)
    return row if isinstance(row, dict) else None


def render_account_action(username: str, frozen: bool, users=None) -> str:
    """`冻结账号「guest5」（账号 id=126，现在：正常。他当前所有登录会话立刻失效，
    在他被解冻之前连登录都进不来）`——**卡面、问句、跨轮待办的目标**共用这一行。

    `users` 三态（与全表同一条纪律：读不到就少说，**不因此不弹窗**）：
      · 快照在手、名字在里面 → 印 `账号 id=…，现在：<现状>`；
      · 快照在手、名字不在 → 印「（后台账号列表里没有叫这个名字的账号）」——主人
        点确定**之前**就该看到，而不是点完才被告知没做成（也**不再报后果**：一件
        做不成的事的后果说了只会误导）；
      · 快照没有（读不到名录）→ 只印名字 + 后果。
    """
    verb = "冻结" if frozen else "解冻"
    row = _account_row(users, username)
    if users and row is None:
        return f"{verb}账号「{username}」（后台账号列表里没有叫这个名字的账号）"
    where = ""
    if row is not None:
        where = f"账号 id={row.get('id')}，现在：{account_state_cn(_row_frozen(row))}。"
    return f"{verb}账号「{username}」（{where}{_ACCOUNT_CONSEQ[bool(frozen)]}）"


def _row_frozen(row):
    """名录行 → 是否冻结（True/False/None，判据 = `status != 0`，同 Rust 侧）。"""
    try:
        return int(row.get("status")) != 0
    except (TypeError, ValueError):
        return None


def account_change_phrase(frozen: bool, changed: bool) -> str:
    """回执的 `change` 摘要：**必须区分"刚改的"与"本来就是"**。

    "状态本来就是冻结，本次未发生变更"与"已冻结"对主人是两句不同的话（前者意味着
    他这一下什么也没做成、后端走了 no-op 分支）；短路成一句会让回执行读成一个动作。
    """
    if not changed:
        return f"状态本来就是{'冻结' if frozen else '正常'}，本次未发生变更"
    return "已冻结" if frozen else "已解冻"


def render_account_status(username: str, uid, frozen: bool, changed: bool = True,
                          before_frozen=None) -> str:
    """冻结/解冻成功后的回执行（工具 side 用；与卡面同源同事实）。"""
    verb = "冻结" if frozen else "解冻"
    if not changed:
        return (f"账号「{username}」（账号 id={uid}）**本来就是"
                f"{account_state_cn(before_frozen)}状态**，这次没有发生任何变更"
                f"（没有重复{verb}）")
    done = {
        True: "他当前所有登录会话**已全部失效**，在他被解冻之前连登录都进不来",
        False: "他现在**能重新登录**了（冻结期间被踢下线的会话不会自动恢复，"
               "需要他自己重新登录）",
    }[bool(frozen)]
    return (f"已{verb}账号「{username}」（账号 id={uid}）：{done}"
            f"（后台已复核：名录里这个账号现在就是{account_state_cn(frozen)}状态）")


# ── 禁言 / 解除禁言（20261002 内容风控下放给 agent）──────────────────────
# 与冻结族**同一条名字通道**（目标 = 后台名录里的账号名、都要印 id 与现状），但有一条
# **硬要求**：后果句**一个字都不许复用冻结族**。禁言从不 bump `token_version`
# （Rust `set_user_muted` 头注），"会话已全部失效 / 需要重新登录"那两句话搬过来就是
# 彻头彻尾的假话——当事人会以为自己被踢下线了，而真相是他照常登录浏览、只是发不出
# 评论与留言（这正是「消息壳架空判据」那一族坑：**壳**先说了假话，判据再好也白搭）。
# Rust 侧 `mute_change_body` 为同一件事单独写了一份正文，两侧纪律同源；
# tests/test_account_mute.py 用「不许出现 失效/重新登录」把这条钉死。
#
# 第二处独有：**期限必须印在卡上**。冻结是一个开关（现状二选一，印出来就够），
# 禁言还多一个"到什么时候"——主人签的若是「永久」，而他想的是"关他三天"，这一格就是
# 他唯一能发现的地方（`_MUTE_SPAN_*` 由同一份归一结果渲染，卡面与实际写下去的期限
# 同源）。
MUTE_FOREVER = "9999-12-31 23:59:59"   # 与 Rust `authz::MUTE_FOREVER` 同源（跨语言契约）
_MUTE_STATE_CN = {True: "禁言中", False: "正常"}
_MUTE_CONSEQ = {
    True: "他照常登录、浏览文章、与你对话，只是发不出评论与留言",
    False: "他马上能重新发布评论与留言",
}
# 回执行里的结局句（与冻结族的 `_ACCOUNT_DONE` 刻意不同形，理由见本节头注）。
# tools/base.py 的 `_set_account_muted` **不另抄一份**：它调 `render_account_mute_status`
# 渲染回执，措辞只有这一处（两处各写一遍，改一处必然漏另一处）。
_MUTE_DONE_PHRASE = {
    True: "他**仍然可以正常登录、浏览文章、与你对话**，只是暂时发不出评论与留言",
    False: "他现在**可以正常发布评论与留言**了",
}


def mute_state_cn(muted) -> str:
    """现算的 `muted` → 人话状态词。**读不出（None）不当成"正常"**（同
    `account_state_cn` 那条纪律：把"没读到"说成事实是最坏的错法）。"""
    if muted is None:
        return "状态未知"
    return _MUTE_STATE_CN[bool(muted)]


def mute_until_raw(row) -> str:
    """名录行里 `mutedUntil` 的原样串；键缺席 / 为 null / 读不出 → **空串**。

    ⚠️ 空串**不是**"现在没被禁言"——那是 `muted` 那一格的判据（到期后值还留着）。
    这一格的唯一用途是区分"这一次刚写下的值"与"库里本来就有的值"（`_set_account_muted`
    的 `changed` 判据，与 Rust 的 no-op 判据 `muted_until.is_none()` 同源）。
    """
    try:
        value = row.get("mutedUntil")
    except AttributeError:
        return ""
    return str(value).strip() if value else ""


def mute_until_cn(until_raw) -> str:
    """期限串 → 人话（`永久` / `至 2026-10-04 12:00`）。哨兵比较与 Rust
    `mute_until_text` 的 `until >= mute_forever_at()` 同义（同格式串的比较）。"""
    raw = str(until_raw or "").strip()
    if not raw:
        return "期限未读到"
    if raw >= MUTE_FOREVER:
        return "永久"
    return f"至 {raw[:16]}"


def mute_span_cn(hours) -> str:
    """归一后的小时数 → 卡面上那一格（`永久` / `24 小时`）。"""
    return "永久" if hours is None else f"{int(hours)} 小时"


def _cn_number(text: str) -> int | None:
    """纯中文整数（`一`/`三`/`十`/`十五`/`二十`/`二十三`）→ int；认不出 → None。

    **只认 0–99 的整数**，形态刻意收窄到"十位各一次"：禁言时长是"几个小时/几天"，
    不存在「三小时半」这种输入，而多认一位就多一处能算错的地方——算错的代价落在
    真人账号上（即使卡面还会让他核对，也不该把"算得对不对"变成他的活）。

    为什么值得为中文数字单独写一段：`三天` 是中文里最常见的说法之一，不收它 ⇒
    主人说「禁他三天」得到的是"要禁多久？"的追问，而**它本来是一句完全清楚的话**。
    宁可为常见形态多算一段，也不让最常见的那句话掉进"认不出"。
    """
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    if not text or any(ch not in digits and ch != "十" for ch in text):
        return None
    if "十" not in text:
        return digits[text] if len(text) == 1 else None
    if text.count("十") != 1:
        return None                      # 「十十」「二十三十」这类一律不猜
    head, _, tail = text.partition("十")
    if head and len(head) != 1:
        return None                      # 「一二十三」不猜
    if tail and len(tail) != 1:
        return None
    tens = digits[head] if head else 1
    ones = digits[tail] if tail else 0
    return tens * 10 + ones


def normalize_mute_hours(value) -> tuple[bool, int | None]:
    """主人/模型给的禁言时长 → `(认得出吗, 小时数)`；`None` = **永久**。

    认得出来的四种：**没给**（None / 空串）⇒ 永久（与后端 `{hours: null}` 同义，
    卡面会把"永久"印出来让他核对）；**「永久」这类说法** ⇒ 永久；正整数（含数字串）
    ⇒ 那么多小时；**带单位的常见写法**（`3天` / `72小时` / `24h` / `三天` / `两个小时`）
    ⇒ 换算成小时（阿拉伯数字与中文数字各一条，见 `_cn_number`；**光一个中文数字不带
    单位不算**——「三」是小时还是天没人知道，那种歧义必须问回去）。

    **认不出的字面一律 `(False, None)`**：调用方必须零写并如实问，**绝不许静默按
    永久办**——把「禁他三天」办成永久禁言，而卡面与实际写下去的值同源（都走这一份
    归一），两边都看不出来（同 `normalize_role` 那条"认不出 ≠ 自己挑一个顶上"）。
    """
    if value is None:
        return True, None
    if isinstance(value, bool):          # bool 是 int 的子类，先挡掉
        return True, None
    if isinstance(value, int):
        return (True, value) if value > 0 else (True, None)
    text = str(value).strip().strip("「」\"'")
    if not text:
        return True, None
    if text in ("永久", "永久禁言", "长期", "无限期", "一直"):
        return True, None
    m = re.fullmatch(r"(\d+)\s*(?:小时|个小时|钟头|h|H|hour|hours)", text)
    if m:
        return (True, int(m.group(1))) if int(m.group(1)) > 0 else (True, None)
    m = re.fullmatch(r"(\d+)\s*(?:天|日|d|D|day|days)", text)
    if m:
        return (True, int(m.group(1)) * 24) if int(m.group(1)) > 0 else (True, None)
    # 中文数字只在**带单位**时认（`三天` / `两个小时`）：光一个「三」说的是小时还是天
    # 没人知道，那种歧义必须问回去（同"认不出"那一支），绝不替主人挑一个量纲。
    m = re.fullmatch(r"([一二三四五六七八九十两]+)\s*(?:天|日)", text)
    if m:
        n = _cn_number(m.group(1))
        return (True, n * 24) if n else (False, None)
    m = re.fullmatch(r"([一二三四五六七八九十两]+)\s*(?:小时|个小时|钟头)", text)
    if m:
        n = _cn_number(m.group(1))
        return (True, n) if n else (False, None)
    try:
        n = int(text)
    except ValueError:
        return False, None
    return (True, n) if n > 0 else (True, None)


def _row_muted(row):
    """名录行 → 现在是否禁言（True/False/None）。判据 = 后端现算的 `muted` 字段，
    与 `tools.base._account_muted` 逐字同源（两处判据分叉 ⇒ 卡面说"现在禁言中"、
    工具侧却按"正常"复核，`reached_specs` 头注警告的正是这个形态）。"""
    try:
        value = row.get("muted")
    except AttributeError:
        return None
    if value is None:
        return None
    return bool(value)


def render_account_mute_action(username: str, muted: bool, hours=None, users=None) -> str:
    """`禁言账号「guest5」（账号 id=126，现在：正常。他照常登录、浏览文章、与你对话，
    只是发不出评论与留言；本次 **永久**…）`——**卡面、问句、跨轮待办的目标**共用这一行。

    `users` 三态与 `render_account_action` 逐字同源（读不到就少说，**不因此不弹窗**）：
    快照在手、名字不在 → 只印名字 +「后台账号列表里没有叫这个名字的账号」，且
    **不再报后果**（一件做不成的事的后果说了只会误导）；快照读不到 → 只印名字 + 后果。

    **期限只在禁言方向印**（解禁没有期限这回事）：那一格是主人核对"我让关三天、
    卡上是不是写着三天"的唯一地方（见本节头注第二处）。
    """
    row = _account_row(users, username)
    if users and row is None:
        if muted:
            return f"禁言账号「{username}」（后台账号列表里没有叫这个名字的账号）"
        return f"解除账号「{username}」的禁言（后台账号列表里没有叫这个名字的账号）"
    where = ""
    if row is not None:
        where = f"账号 id={row.get('id')}，现在：{mute_state_cn(_row_muted(row))}。"
    if muted:
        ok_hours, span = normalize_mute_hours(hours)
        tail = (f"本次禁言 **{mute_span_cn(span)}**"
                if ok_hours else "**没听懂要禁多久**")
        return (f"禁言账号「{username}」（{where}{_MUTE_CONSEQ[True]}；"
                f"{tail}——要提前解除，再单独说一次解禁）")
    return (f"解除账号「{username}」的禁言（{where}"
            f"{_MUTE_CONSEQ[False]}）")


def account_mute_change_phrase(muted: bool, changed: bool, until_raw=None) -> str:
    """回执的 `change` 摘要：**必须区分"刚改的"与"本来就是"**（同冻结族那条）。

    禁言方向那一格带上期限：同一行里"已禁言"与"已禁言（至 10-04 12:00）"对主人是
    两句不同的话（后者能核对他要的那个时长）。
    """
    if muted:
        if not changed:
            return "状态本来就是禁言中，本次未发生变更"
        return f"已禁言（{mute_until_cn(until_raw)}）"
    if not changed:
        return "本来就没有被禁言，本次未发生变更"
    return "已解除禁言"


def render_account_mute_status(username: str, uid, muted: bool, until_raw=None,
                               changed: bool = True, before_muted=None) -> str:
    """禁言/解禁成功后的回执行（工具 side 用；与卡面同源同事实）。"""
    if not changed:
        # 只可能是解禁方向（Rust 对"库里本来就没值"那一支是真 no-op）。
        return (f"账号「{username}」（账号 id={uid}）**本来就没有被禁言**，"
                f"这次没有发生任何变更（没有重复解除）")
    if muted:
        return (f"已禁言账号「{username}」（账号 id={uid}）：{_MUTE_DONE_PHRASE[True]}；"
                f"本次期限 **{mute_until_cn(until_raw)}**"
                f"（后台已复核：名录里这个账号现在就是禁言中）")
    return (f"已解除账号「{username}」（账号 id={uid}）的禁言：{_MUTE_DONE_PHRASE[False]}"
            f"（后台已复核：名录里这个账号现在没有被禁言）")



# 与冻结族**同一族**（目标都是后台名录里的一个账号名、都要印 id 与现状），但有三处
# 与它刻意不同形，都是"这两件事不是一件事"的直接后果：
#   ① **卡面必须印出"从什么身份 → 什么身份"**。冻结只动一个开关（现状是二选一，
#      印出来就够），而身份是四档里的一个值——只印目标身份的话，主人核对不了
#      "他原来是管理员吗"（那正是这一下最需要他看的一格）；
#   ② **后果句随目标身份变**（改成杂鱼与改回普通用户是两种晚年）。
#      写成一句通用的「会改身份」等于没说——他要判断的是"把这个人改成杂鱼到底
#      意味着什么"；
#   ③ 措辞里**不写**任何权限档位的规则（"管理员只能改两档"那类）——那是后端
#      `authz::check_role_change` 的话，agent 侧复述一遍就会在政策变更那天变成假话
#      （同 `_policy_post` 那条"逐字转述后端原话"的纪律）。
ROLE_CN = {"superadmin": "超级管理员", "admin": "管理员",
           "user": "普通用户", "zako": "杂鱼"}
# 说法 → 身份码。**四档都认**，包括 superadmin：认出来之后由后端按政策拒
# （「超级管理员身份不能在这里指派」是一句**真话**，而"认不出这个身份"是一句错话）。
# 只认这两档的写法**不是**这里的判据——那是 `authz::check_role_change` 的事。
_ROLE_ALIASES = {
    "superadmin": "superadmin", "超级管理员": "superadmin", "超管": "superadmin",
    "admin": "admin", "管理员": "admin", "管理员账号": "admin",
    "user": "user", "普通用户": "user", "普通账号": "user", "普通": "user",
    "zako": "zako", "杂鱼": "zako", "杂鱼酱": "zako",
}
# 目标身份的**后果句**。这一格与 `_ACCOUNT_CONSEQ`（冻结族）同一条纪律：差异要落在
# **后果**上，不能只换动词——「会改成杂鱼」与「会改成普通用户」读起来是同一个动作的
# 两个方向，而真正的区别是"他还能不能使唤 agent"。
_ROLE_CONSEQ_TAIL = "他当前所有登录会话立刻失效，必须重新登录一次"
_ROLE_CONSEQ = {
    "zako": "他当场被踢下线；再登录进来就是**杂鱼**了——和泠月喵说话时对方只会闲聊，"
            "任何站内操作都不会替他做",
    "user": "他当场被踢下线；再登录进来就恢复成普通访客的权限"
            "（能干的事与刚注册时一样）",
}
_ROLE_CONSEQ_OTHER = "他当场被踢下线，再登录进来就是新的身份了"


def role_cn(role) -> str:
    """身份码 → 人话。**未登记的身份原样回显**（不编一个中文名）：卡面要如实，
    而"认不出的编码"正是最该让主人看见的东西。"""
    key = str(role or "").strip()
    return ROLE_CN.get(key, key or "未知身份")


def normalize_role(value) -> str | None:
    """主人/模型给的身份说法 → 身份码；认不出 → **None**（调用方零工具 + 如实问）。

    两份词表合一（英文码与中文名）：后台名录里的 `role` 是英文码，而技能描述与主人
    的话都是中文——只认一份会让另一份在展开层被当成"认不出来"（一句错话：站内明明
    有这个身份）。
    """
    key = str(value or "").strip().strip("「」\"'")
    if not key:
        return None
    return _ROLE_ALIASES.get(key) or _ROLE_ALIASES.get(key.lower())


def render_account_role(username: str, to_role, users=None) -> str:
    """`把账号「guest5」（账号 id=126，现在：普通用户）的身份改成**杂鱼**（…后果…）`
    ——**卡面、问句、跨轮待办的目标**共用这一行。

    `users` 三态与 `render_account_action` 逐字同源（读不到就少说，**不因此不弹窗**）：
    快照在手但名字不在 → 只印名字 + 「后台账号列表里没有叫这个名字的账号」，且
    **不再报后果**（做不成的事说后果只会误导）；快照读不到 → 只印名字 + 后果。

    **现状那一格取自名录**（不是取模型填的值）：模型填的 `from` 是它自己说的，
    而卡面要印的是**后台此刻的事实**——两者不一致时主人该看见的是后者。
    """
    want = normalize_role(to_role)
    shown = role_cn(want) if want else str(to_role or "").strip() or "（没有给出身份）"
    head = f"把账号「{username}」的身份改成**{shown}**"
    row = _account_row(users, username)
    if users and row is None:
        return f"{head}（后台账号列表里没有叫这个名字的账号）"
    where = ""
    if row is not None:
        where = f"账号 id={row.get('id')}，现在：{role_cn(row.get('role'))}。"
    conseq = _ROLE_CONSEQ.get(want or "", _ROLE_CONSEQ_OTHER)
    return f"{head}（{where}{conseq}；{_ROLE_CONSEQ_TAIL}）"


def account_role_change_phrase(to_role, changed: bool) -> str:
    """回执的 `change` 摘要：**必须区分"刚改的"与"本来就是"**（同冻结族那条）。"""
    label = role_cn(to_role)
    if not changed:
        return f"身份本来就是{label}，本次未发生变更"
    return f"已改为{label}"


def render_account_role_status(username: str, uid, to_role, before_role=None,
                               changed: bool = True) -> str:
    """变更身份成功后的回执行（工具 side 用；与卡面同源同事实）。"""
    label = role_cn(to_role)
    if not changed:
        return (f"账号「{username}」（账号 id={uid}）**本来就是{label}身份**，"
                f"这次没有发生任何变更（没有重复变更）")
    conseq = _ROLE_CONSEQ.get(str(to_role or "").strip(), _ROLE_CONSEQ_OTHER)
    return (f"已把账号「{username}」（账号 id={uid}）的身份从"
            f"「{role_cn(before_role)}」改为「{label}」：{conseq}；{_ROLE_CONSEQ_TAIL}"
            f"（后台已复核：名录里这个账号现在就是{label}身份）")


# ── 读后台账号名录（20261006）───────────────────────────────────────────
# 为什么单立一份"读"：`account_freeze` / `account_unfreeze` / `account_set_role` /
# `mute_account` / `unmute_account` / `notice_send` / `quota_reset` 这七件**写**技能的
# 参数契约都写着「账号名**必须能在后台账号列表里看到**」，而在 20261006 之前**没有任何
# 一个技能读得出那份列表**——`_user_directory` 是写工具内部的私有 helper
# （`GET /api/temp-users`），planner 看不见它。于是"先确认这个账号存在"这条前提
# **住在模型够不着的地方**，它只能去抓一个近邻：trace `20261006T082437` 里主人说
# 「给本本恢复身份」，planner 点了 `admin_notes`（**文章**清单）、关键词还传了空串
# = 要整张后台清单——它保住的是 `list_admin_notes` 描述教的那条动作（"要改某篇文章…
# 先用它拿到**确切的 id**"），只是把领域换成了账号。同族的先例是 `article_status` /
# `article_tags`：那两件的描述里**点名**了 `admin_notes`，文章族就从来没出过这个错。
# 这一屏的价值全在**账号名怎么写**：写通道只按名字指认（没有按 id 的通道，见
# tools/base.py 那一族的头注），差一个字就是"后台没有这个账号"。所以账号名印在最前、
# 加「」，且**不印昵称**——不是漏了：`/api/temp-users` 就没有这个字段（Rust 的
# `TempUserInfo`），后台账号列表页看的也是同一份。主人用昵称称呼时，planner 只能拿
# 它当名字去比，比不中就走 `_find_named_user` 那条"后台没有这个账号 + 名字最接近的
# 候选"的路——而不是在这里替它编一个昵称，也不该拿昵称去反推账号名。
_ROSTER_TAIL = ("（上面印的是**账号名**：写操作按它指认，**一个字都不能差**；"
                "超级管理员的账号不在这份名单里）")


def render_account_roster(rows) -> str:
    """后台账号名录快照 → 给 planner 看的清单（**纯渲染**，IO 与拼装在 tools/base.py）。

    `rows` = `/api/temp-users` 的原样数组（`_user_directory` 的产物）。**空数组是一个
    事实**（后端确实一个都没给），照实说"一个都没有"。**读不到不走这里**：那时
    `_user_directory` 已经返回 `unavailable`，调用方原样透传，绝不喂进本函数渲染成
    "零个账号"——「缺键绝不编 0」，"读不到"与"没有"是两件事。
    """
    items = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("username") or "").strip()
        if not name:
            continue  # 没有账号名的行对写通道零用处（写通道只按名字指认）
        uid = normalize_target_id(row.get("id"))
        # **不印 `账号 id=None`**：那看起来像一个编号，模型会照着抄（同
        # `render_quota_requests` 里 uid 那一格的取舍）。
        gap = f"（账号 id={uid}）" if uid is not None else "（读不到账号 id）"
        items.append((
            float(uid) if uid is not None else float("inf"),
            f"- 「{name}」{gap}身份={role_cn(row.get('role'))}，"
            f"账号状态={account_state_cn(_row_frozen(row))}，"
            f"禁言状态={mute_state_cn(_row_muted(row))}"))
    # **按 id 升序**：同一份名录在两轮之间要逐字节相同。后端给的顺序不保证稳定，而这份
    # 文本要进 planner 上下文——本仓刚量过"输入里任何逐轮/逐分钟变的东西都会让对照失效"
    # （见 docs/param-tuning-20261006.md 里 `page_ctx` 台账年龄串那条）。
    items.sort(key=lambda it: it[0])
    body = "\n".join(line for _, line in items)
    if not body:
        return f"后台账号名录：**后台一个账号都没有**。{_ROSTER_TAIL}"
    return (f"后台账号名录：共 {len(items)} 个账号（按账号 id 升序）：\n"
            f"{body}\n{_ROSTER_TAIL}")


# ── 给单个账号发通知（20260926）─────────────────────────────────────────
# 这一族的卡面有**一条硬要求**（不是文风问题）：正文**全文**印出来。理由是这件事的
# 性质——正文由模型按主人的意思**整理**（用户拍板的方向，见 `_send_user_notice` 头注），
# 而它是一段以主人名义发给**第三方**、**发出后没有撤回通道**的话。整理过的东西加上
# 收不回，唯一的人眼复核点就只剩这张卡：主人看到全文才能判断"这是不是我要说的那句话"。
# 截断（哪怕只截尾）会让主人核对的文本与实际发出去的不是同一份 —— 同 `notice.rs`
# 那条"超长在校验层拒、不静默截断"的取舍。
_NOTICE_CONSEQ = ("（这条通知会出现在**对方的个人中心**，且**发出后没有撤回的通道**——"
                  "站内没有删除已发通知的功能）")


def render_notice_action(name: str, content: str, title: str = "", users=None) -> str:
    """`给账号「guest5」（账号 id=126）发一条**站内通知**（标题「站内通知」，正文：
    「请尽快补齐资料」…）` —— **卡面、问句、跨轮待办的目标**共用这一行。

    `users` 三态与 `render_account_action` 逐字同源（读不到就少说，**不因此不弹窗**）：
    名录在手且名字在里面 ⇒ 印 `账号 id=…`；名录在手但名字不在 ⇒ 如实标注（主人点
    确定**之前**就该看到，而不是点完才被告知没发成）；名录读不到 ⇒ 只印名字。
    """
    row = _account_row(users, name)
    if users and row is None:
        return f"给账号「{name}」发一条**站内通知**（后台账号列表里没有叫这个名字的账号）"
    # 账号 id 只在名录里查得到时才印（同 render_account_action 的三态）
    who = f"（账号 id={row.get('id')}）" if row is not None else ""
    t = str(title or "").strip() or "站内通知"
    body = str(content or "").strip() or "（没有写正文）"
    return (f"给账号「{name}」{who}发一条**站内通知**"
            f"（标题「{t}」，正文：**「{body}」**）{_NOTICE_CONSEQ}")


def render_notice_status(username: str, uid, title: str, content: str) -> str:
    """发通知成功后的回执行（工具 side 用；与卡面同源同事实）。

    正文在这里**截断**（卡面不许截、回执可以）：回执经 `__EXEC__` 落 execution_log，
    整行有 300 字的列宽上限，而正文可以到 1000 字。截断要**如实标注节选**——回执是
    下一轮 narrator 唯一的取值来源，一段没有标注的截断会被当成"全部"照念。
    """
    body = str(content or "").strip()
    shown = clip(body, 60)
    more = "" if len(body) <= 60 else f"（节选，共 {len(body)} 字）"
    t = str(title or "").strip() or "站内通知"
    return (f"已把通知发给账号「{username}」（账号 id={uid}）：标题「{t}」，"
            f"正文「{shown}」{more}。{_NOTICE_CONSEQ}")
