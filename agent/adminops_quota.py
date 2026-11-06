# -*- coding: utf-8 -*-
"""对话额度重置申请的卡面与回执。"""

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
    """格式化已知额度；无限额、未知和零剩余额度分别表示，不补造零值。"""
    used = quota_used(row)
    lim = quota_limit(row)
    if used is None or lim is None:
        return "（额度未知）"
    if lim <= 0:
        return "不限额"
    return f"额度 剩{max(0, lim - used)}/{lim} 轮"


def render_quota_action(kind: str, name: str, users=None, quota_requests=None,
                        user_id=None) -> str:
    """按账号编号、当前额度和操作影响生成确认说明。"""
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
        如实报读数，**不许**照抄"已恢复"（那会是一句他自己能证伪的话）；
      · `used is None` ⇒ 端点回了成功但读不回读数：只说端点那一半，明说未复核
        （同 `_set_account_frozen` 的"未确认"取向，**不冒充已核实**）。
    """
    lim = limit if isinstance(limit, int) and limit > 0 else 0
    # 读数写**余额**：这一族的判据仍然是 `chatQuotaUsed`（见 `_quota_readback`），
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
# 一遍，改一处就会漂移成两种说法。`None`/uid 取不出 ⇒ 空串。
_UNREACHABLE_NOTE = ("（⚠ 这个编号不在后台账号名录里——超管账号不列进名录、"
                     "注销过的账号也不在；**agent 的额度写通道办不了这一件**，"
                     "得你到后台处理）")


def _unreachable_note(uid, reachable) -> str:
    """这一行够不够得着 → 空串 / `_UNREACHABLE_NOTE`。`reachable` 为 None 时不调用。"""
    if uid is None or reachable is None or uid in reachable:
        return ""
    return _UNREACHABLE_NOTE


def render_quota_requests(rows, reachable=None) -> str:
    """按编号列出额度申请与已知账号信息；缺失额度保持未知。"""
    lines = [f"额度重置申请共 {len(rows)} 条："]
    for r in rows:
        if not isinstance(r, dict):
            continue
        who = str(r.get("username") or "").strip() or "（账号名读不出）"
        nick = str(r.get("nickname") or "").strip()
        used = r.get("used")
        lim = r.get("limit")
        try:
            # 余额口径：这一屏的用法正是"管理员看完之后说'批准 Alice'"，
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
