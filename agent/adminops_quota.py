# -*- coding: utf-8 -*-
"""对话额度重置申请的卡面与回执。

批 3a 从 `agent/adminops.py` 按域拆出（原文逐字节保留）。"""

from __future__ import annotations

from agent.adminops_account import (
    _account_row,
    _account_row_by_id,
)
from agent.adminops_tags import (
    clip,
)
from agent.adminops_target import (
    normalize_target_id,
)

# ── 对话额度的重置申请（20260929）────────────────────────────────────────
# 三个动作的**后果互不相同**，卡面必须分别说清（照 `_ACCOUNT_CONSEQ` 那条纪律：
# 主人点的是「确定」，他有权在点之前看出自己同意的是什么）。三句的差异不是文风：
#   · **批准** = 回应他的申请、把余额恢复满、**不可撤销**（额度是个数，
#     没有"撤回"这个概念——批完再想反悔只能等他哪天真的又用完）；
#   · **驳回** = 他会收到一条站内通知、额度**不变**、**还可以再申请**——
#     三个动作里唯一可逆、唯一"什么都不改变"的一件；
#   · **主动重置** = **不需要他申请过**（这一件与前两件最容易被读混：批准是
#     "回应他"，重置是"你替他决定"）。三者措辞互不相同是硬要求
#     （`tests/test_chat_quota.py` 有一条断言逐句比对）。
#
# **措辞一律说「额度 / 余额」，不说「计数器清零」**（20260929 用户反馈「额度重置
# 确认说的不太对，用户看到的是递减的」）：库里存的确实是**累计已用**、重置做的确实
# 是把它置 0，但主人看的那一面是**余额**（`剩 363/500`，每聊一轮减一）——所以
# "清零"在他读来是"把额度清掉"，与眼前那个数字**往上跳回满格**正好说反。
# 内部判据（`chatQuotaUsed == 0`）不变；变的只是给人看的字。
_QUOTA_CONSEQ = {
    "approve": "（批准后他的额度立刻恢复到上限、马上可以继续提问；**这个动作不可撤销**）",
    "reject": "（驳回后他会收到一条站内通知，额度**不变**，他还可以重新申请）",
    "reset": "（**不需要他申请过**；恢复满额后他马上可以继续提问，**这个动作不可撤销**）",
}
# 三值状态词（与 Rust `quota_request.status` 的取值域同源：0/1/2，见迁移头注）。
_QUOTA_STATUS_CN = {0: "待处理", 1: "已批准", 2: "已驳回"}
# 三个动作在卡面/回执里的动词：**与工具名一一对应**（`approve_quota_request` 的
# 动词就是「批准」），下一个人对照工具名读就该读得通。
_QUOTA_VERB = {"approve": "批准", "reject": "驳回", "reset": "重置"}


def _quota_pair(row) -> str:
    """名录行 → `额度 剩363/500 轮` / `不限额` / `（额度未知）`（**不编 0**）。

    读不出字段就说读不出：把"读不到"写成 `剩 0/0` 会被读成"这个人额度是满的"或
    "这个人不限额"，两句都可能是假话（同 `_account_frozen` 的 `None ≠ False`）。

    读数写**余额**（20260929b，与前端四处、系统上下文行统一口径）：主人看卡面那一眼要
    判断的是"这一下会不会改变什么"，而答案取决于他**还剩**多少（见 `_reached_one` 的
    noop 判据）——`137/500` 要心算一步。判据本身仍然是 `chatQuotaUsed`（内部读数），
    这里只改给人看的字。

    **措辞的单一来源是余额**（20260929 用户反馈后统一，见 `_QUOTA_CONSEQ` 上面那段）：
    这一族（卡面 / 问句 / 回执 / 后台那句话）**一律不说「清零」**——主人眼前那个数是
    递减的余额，"清零"与它照面时说的是反话。
    """
    used = quota_used(row)
    lim = quota_limit(row)
    if used is None or lim is None:
        return "（额度未知）"
    if lim <= 0:
        return "不限额"
    return f"额度 剩{max(0, lim - used)}/{lim} 轮"


def render_quota_action(kind: str, name: str, users=None, quota_requests=None,
                        user_id=None) -> str:
    """`批准账号「Alice」（账号 id=126，现在 额度 500/500 轮）的额度重置申请（…）`
    ——**卡面、问句、跨轮待办的目标**共用这一行。

    `users` 三态与 `render_account_action` 逐字同源（读不到就少说，**不因此不弹窗**）：
    名录在手且名字在里面 ⇒ 印 `账号 id=…，现在：<用量>`；名录在手但名字不在 ⇒ 如实
    标注「后台账号列表里没有叫这个名字的账号」（主人点确定**之前**就该看到）；
    名录读不到 ⇒ 只印名字。

    `quota_requests` = 待处理申请快照（`tools.base._quota_pending_index` 的产物，
    `{uid: 行}`）：在手就把**他的申请理由**印进卡面——主人正在决定批不批，理由是
    申请人自己的诉求，而这是整条链上唯一印得出它的地方；复核时那一行已经不在了
    （比如他刚被处理过）也如实标注，同 `users` 那条三态纪律。

    **两条目标通道**（20260929 批 H · S2）：`user_id` 给得起就走 **id 通道**（批准与
    驳回的参数是台账里的 uid，模型的参数里根本没有名字——名字从名录快照取回来印，
    取不到就只印编号）；给不起就走**名字通道**（主动重置仍按账号名，见 `_expand_write_workflow`
    那一支的注）。两条通道渲染出来的卡面**同形同事实**，只在"名录里没有这一行"时
    措辞各自说准（一个说名字、一个说编号）。
    """
    verb = _QUOTA_VERB.get(kind, kind)
    tail = _QUOTA_CONSEQ.get(kind, "")
    by_id = user_id is not None
    if by_id:
        uid = normalize_target_id(user_id)
        row = _account_row_by_id(users, uid)
        # 名字从名录取回；取不到就退回「账号 id=N」——id 认不出时连编号都说不出来，
        # 那时如实写「没给出账号编号」（渲染不许编一个数）。
        name = (str(row.get("username") or "").strip() if row else "") or (
            f"账号 id={uid}" if uid is not None else "（没有给出账号编号）")
        if users and row is None:
            return (f"{verb}账号「{name}」的对话额度重置申请"
                    f"（后台账号列表里没有这个编号的账号）")
    else:
        row = _account_row(users, name)
        if users and row is None:
            return f"{verb}账号「{name}」的对话额度重置申请（后台账号列表里没有叫这个名字的账号）"
    who = ""
    if row is not None:
        where = _quota_pair(row)
        plain = f"（账号 id={row.get('id')}）"
        who = f"（账号 id={row.get('id')}，现在 {where}）" if where else plain
    entry = _quota_request_of(quota_requests, row)
    if kind == "reset":
        # 主动重置**不是**回应谁的申请（三件的差别里最容易被读混的一条），
        # 所以这一支**不提「申请」两个字**，理由也不印（他没有申请过，或申请
        # 与这一下无关——印出来会让主人以为自己在批那条申请）。
        return f"**主动**把账号「{name}」{who}的对话额度恢复满额{tail}"
    if entry is None:
        if quota_requests is not None:
            # 快照在手而里面没有他 ⇒ 如实标注：主人点确定之前就该知道"他并没有
            # 待处理的申请"，而不是点完再被告知没做成。
            return (f"{verb}账号「{name}」{who}的额度重置申请"
                    f"（他**现在没有待处理的额度申请**）{tail}")
        return f"{verb}账号「{name}」{who}的额度重置申请{tail}"
    reason = str(entry.get("reason") or "").strip()
    return (f"{verb}账号「{name}」{who}的额度重置申请"
            f"（他的申请理由：**「{reason or '（没有填写理由）'}」**）{tail}")


def _quota_request_of(quota_requests, row):
    """待处理申请快照里属于 `row` 的那一行（没有 / 快照不可用 / id 读不出 → None）。"""
    if not isinstance(quota_requests, dict) or not isinstance(row, dict):
        return None
    try:
        uid = int(row.get("id"))
    except (TypeError, ValueError):
        return None
    hit = quota_requests.get(uid)
    return hit if isinstance(hit, dict) else None


def render_quota_status(kind: str, username: str, uid, limit, used) -> str:
    """三个动作成功后的回执行（工具 side 用；与卡面同源同事实）。

    `used` = **写后重读名录**实测到的计数器（工具给不出时是 None）——这一族唯一的
    硬事实来源。三种情形分开说，**一句都不许猜**：
      · `used == 0` ⇒ 读数确认余额已恢复满（这是绝大多数情况）；
      · `used > 0`  ⇒ 他在这之后又聊过了（恢复是真发生了，但读数已不满）——
        如实报实测值，**不许**照抄"已恢复"（那会是一句他自己能证伪的话）；
      · `used is None` ⇒ 端点回了成功但读不回读数：只说端点那一半，明说未复核
        （同 `_set_account_frozen` 的"未确认"取向，**不冒充已核实**）。
    """
    lim = limit if isinstance(limit, int) and limit > 0 else 0
    # 读数写**余额**（20260929b）：这一族的判据仍然是 `chatQuotaUsed`（见 `_quota_readback`），
    # 但那是一个内部判据、不是给主人看的读数。`剩 500/500` 与 `0/500` 是同一件事，
    # 前者一眼就是"满了"，后者要先分辨 0 是哪一栏。`used is None`（读不回读数）与
    # `reject` 两支都不印这一对读数（各自只说端点那一半 / "额度没有变化"），
    # 那里把 None 当 0 只是取个确定的形状。
    _u = used if isinstance(used, int) else 0
    pair = f"剩 {max(0, lim - _u)}/{lim}" if lim else "不限额"
    head = {
        "approve": f"已批准账号「{username}」（账号 id={uid}）的对话额度重置申请",
        "reject": f"已驳回账号「{username}」（账号 id={uid}）的对话额度重置申请",
        "reset": f"已把账号「{username}」（账号 id={uid}）的对话额度恢复满额",
    }.get(kind, f"已处理账号「{username}」（账号 id={uid}）的额度重置申请")
    if kind == "reject":
        return (f"{head}：他的额度**没有变化**（驳回不改变任何额度），"
                f"他还会收到一条站内通知，可以重新申请。")
    if used is None:
        return (f"{head}（后台已受理，但**读不回他的额度读数**，本次结果未复核——"
                f"不要对外声称额度已经恢复，如实说后台已受理即可）。")
    if used == 0:
        return f"{head}：他的额度现在读数是 {pair}，马上可以继续提问了。"
    return (f"{head}：后台已受理，复核时他的额度读数是 **{pair}**"
            f"——恢复满额之后他又聊过了（这是重读到的实测值）。")


# 「这一件 agent 办不了」那一句**只此一份**：额度台账帧（`graph._render_quota_facts`）
# 与 `list_quota_requests` 的清单（本函数）指的必须是**同一件事、同一句话**——两处各写
# 一遍，改一处就会漂移成两种说法（本仓反复吃过这个形状的亏）。`None`/uid 取不出 ⇒ 空串。
_UNREACHABLE_NOTE = ("（⚠ 这个编号不在后台账号名录里——超管账号不列进名录、"
                     "注销过的账号也不在；**agent 的额度写通道办不了这一件**，"
                     "得你到后台处理）")


def _unreachable_note(uid, reachable) -> str:
    """这一行够不够得着 → 空串 / `_UNREACHABLE_NOTE`。`reachable` 为 None 时不调用。"""
    if uid is None or reachable is None or uid in reachable:
        return ""
    return _UNREACHABLE_NOTE


def render_quota_requests(rows, reachable=None) -> str:
    """额度重置申请队列 → 给 planner 看的清单。

    这一屏的价值全在**申请人的账号 id、名字与他的用量**（20260929 批 H · S2 改）：
    批准/驳回的参数就是台账里的 `账号 id=`（uid），所以**这一屏必须印 uid**——
    不印的话"读到了队列"与"能动手"之间就断了一截，planner 只能去猜一个编号，
    而那正是写保护要拒的东西。旧版的「**行 id 刻意不印**……印出来只会诱发 planner
    猜一个编号」讲的是**申请行 id**（`rid`，后端 `quota_requests.id`）：那个键
    到今天仍然不印也不该印——模型永远不需要知道它，`rid` 由系统从
    `_quota_pending_index[uid]` 自己取。两个 id 是两样东西，别把这一条读成
    "行 id 可以印了"。

    印出来的 uid 不是"要求模型相信它"：写之前由 `graph._ledger_target_refusal`
    对着**现场台账**再校验一次（id 必须真实存在且仍在待办态），所以印错/抄错
    不会变成一次错写，只会变成一次如实收尾。

    `reachable` = 名录里够得着的 uid 集合（`tools.base._reachable_uids`）；`None` =
    没查/读不到 ⇒ **一条都不标**。够不着的那几件照样印（它们是**事实**：有人确实在
    等着），只是行末多一句"agent 办不了"——队列里可以出现超管（名录不列超管）与
    注销过的账号（无外键，销号不带走申请行），那几件只有后台办得成。
    """
    lines = [f"额度重置申请共 {len(rows)} 条："]
    for r in rows:
        if not isinstance(r, dict):
            continue
        who = str(r.get("username") or "").strip() or "（账号名读不出）"
        nick = str(r.get("nickname") or "").strip()
        used = r.get("used")
        lim = r.get("limit")
        try:
            # 余额口径（20260929b）：这一屏的用法正是"管理员看完之后说'批准 Alice'"，
            # 而他那一眼要判的就是"他还剩多少"。减法只在这里做一次，判据 `used` 不变。
            pair = ("不限额" if int(lim) <= 0
                    else f"剩{max(0, int(lim) - int(used))}/{int(lim)} 轮")
        except (TypeError, ValueError):
            pair = "（用量未知）"
        st = _QUOTA_STATUS_CN.get(_quota_status_code(r.get("status")), "")
        reason = clip(str(r.get("reason") or "").strip(), 60) or "（没有填写理由）"
        extra = clip(str(r.get("note") or "").strip(), 40)
        # uid 取不出（脏行）就**不印 id 那一格**，不能印一个 `账号 id=None`：
        # 那看起来像一个编号，而模型会照着抄（同 `_board_label` 那条"id 必须带
        # 命名空间"的取向——宁可少印一格，不印一个假编号）。
        uid = normalize_target_id(r.get("userId"))
        head = f"- 「{who}」" + (f"（账号 id={uid}）" if uid is not None else "") \
            + (f"（昵称 {nick}）" if nick else "") \
            + (_unreachable_note(uid, reachable) if reachable is not None else "")
        rest = [f"现在 {pair}"]
        if st:
            rest.append(f"[{st}]")
        rest.append(f"理由：「{reason}」")
        if extra:
            rest.append(f"处理备注：「{extra}」")
        when = str(r.get("createdAt") or "").strip()
        if when:
            rest.append(f"提交于 {when}")
        lines.append(head + "，" + "，".join(rest))
    return "\n".join(lines)


def _quota_status_code(v):
    """申请行的状态 → `0/1/2`；认不出 → None（**不当成待处理**，只是不印状态词）。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n in _QUOTA_STATUS_CN else None


def quota_used(row):
    """名录/申请行 → 已用轮数；读不出 → **None**（同 `_account_frozen` 的判据纪律：
    `None` 不是 0——把"读不到"当成 0 会让"额度已清零"的复核把一次没发生的事判成功）。

    公开名（不带下划线）：`tools/base.py` 的三个写工具**也用这一份**读同一个字段
    （写前预检与写后复核各一次）——两处各写一份取值必然在字段改名那天分叉，
    所以只有这一处实现。"""
    try:
        return int(row.get("chatQuotaUsed"))
    except (TypeError, ValueError, AttributeError):
        return None


def quota_limit(row):
    """名录行 → 额度上限（0 = 不限额）；读不出 → None。"""
    try:
        return int(row.get("chatQuotaLimit"))
    except (TypeError, ValueError, AttributeError):
        return None
