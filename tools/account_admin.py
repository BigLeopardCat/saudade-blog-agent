# -*- coding: utf-8 -*-
"""Legacy account tools retained for the explicit graph compatibility entry.

Native account changes use agent.domains.account_actions and the versioned backend
transaction service. This legacy adapter resolves full usernames through the old
account directory. Backend policy applies independently to both names and IDs;
directory visibility is not an authorization boundary. Shared helpers stay on
_base so existing compatibility callers and mocked transports use the same state.
"""

from typing import Annotated

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, empty, fact, not_found, ok, tgt, unavailable

# `__all__` = 本模块**所有不以 `_` 开头**的模块级名字（社区惯例，不留例外）：
# 七个 `@tool` 是工具本体。三个上限常量、名录与解析帮手是下划线名，由 `tools/base.py`
# 按原名显式 re-export 供给（显式 import 不受 `__all__` 约束）。
__all__ = ["account_mute", "account_unmute", "freeze_account", "list_accounts",
           "send_user_notice", "set_account_role", "unfreeze_account"]


def _user_directory(config: RunnableConfig) -> dict[int, dict] | ToolResult:
    """读后台**账号名录** → `{uid: 行}`；读不到 → `ToolResult`（失败，不是 None）。

    ⚠️ **不要把它"统一"回 `_admin_get`**（本节最容易改错的一处）：
    `GET /api/temp-users` 是全站唯一**不回 `ApiResponse` 信封**的 `/api/protected`
    接口（Rust 侧直接 `Json<Vec<TempUserInfo>>`，src/routes/temp_user.rs），而
    `_principal_get` 里取业务码的那一行（`body.get("code")`）在**裸 list** 上会抛
    `AttributeError`——异常冒到 execute 的兜底，表现成过程行「执行出错」，看起来像
    服务挂了。也**不要反过来**去改后端把它包成信封：前端 `Users/index.tsx` 按
    `Array.isArray(res?.data)` 读、父仓 `scripts/probe_token_revoke.py` 直接迭代裸数组，
    包信封会同时打断这两处。所以这里自带一次请求，其余形状照抄 `_principal_get`
    （同一把 60 秒代签 JWT、同一条 `/api/protected` 前缀、同一套 fail-closed），
    **唯一**的差别就是最后认的是"顶层是不是 list"。

    失败返回 `ToolResult` 而不是 `None`：本族的调用方（按名字解析）在**任何**读不到
    的情况下都必须零写，原样往外传一个带人话的 ToolResult 比 `None` 更好用
    （`_tag_index` 那套 `None` 是给"读不到也要出报表"的只读场景用的）。
    """
    # ⚠️ 传进来的**必须是 config**（20260930 加的响亮失败）。本函数的调用方里有一族
    #    `_find_*(name, config, index=…)` 的形状——"读好的快照"是**第三个**参数。一旦
    #    有人把快照当 config 传进来，下面读出的是 uid=0，函数会回一句「无法获取当前
    #    用户身份」，而**身份其实好着**：那是一句假话，调用方还会照着它零写收场，谁也
    #    看不出哪里错了。生产实证：额度批准/驳回整条通道自 4cb1f93（20260930）起一次都
    #    没成功过，而离线套件全绿——桩写成 `lambda config: …`，把参数整个忽略了。
    #    所以这里不猜、也不返回一句含糊的"读不到"：形状不对直接抛，让错误以
    #    `__ERROR__: TypeError` 进 trace —— **这是编程错误，不是一次业务失败**。
    if not isinstance(config, dict) or "configurable" not in config:
        raise TypeError(
            f"_user_directory 收到的东西不是 config（{type(config).__name__}）——"
            f"调用方很可能把名录快照或别的 dict 当 config 传了；快照是第三个形参 "
            f"`index=`（见 _find_named_user / _find_user_by_id 的签名）")
    uid = _base._device_get_user_id(config)
    if uid <= 0:
        return unavailable("无法获取当前用户身份，后台账号列表不可用")
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer "
               + _base._sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _base._client.get(f"{_base.ADMIN_BASE}/api/temp-users", headers=headers, timeout=15)
    except Exception as exc:
        _base.logger.error("user directory call failed: %s", exc)
        return unavailable(f"读后台账号列表的请求失败: {exc}")
    if resp.status_code in (401, 403):
        return unavailable("当前身份无权访问后台账号列表（该功能仅管理员可用）")
    if resp.status_code != 200:
        return unavailable(f"后台账号列表返回 HTTP {resp.status_code}")
    try:
        rows = resp.json()
    except Exception:
        return unavailable("后台账号列表返回的不是 JSON")
    if not isinstance(rows, list):
        # 信封形状 = 后端换了契约。**不当成台账**：宁可如实说读不懂，也不让一次契约
        # 变更变成"名录里一个账号都没有"（下一步就是零写，而"没有这个账号"是假话）。
        return unavailable("后台账号列表返回的形状不对（不是数组），读不出账号名录")
    out: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            rid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        row["id"] = rid            # id 就地归一成 int：写后复核直接按 id 找回那一行
        out[rid] = row
    return out


@tool
def list_accounts(config: RunnableConfig) -> str:
    """查看**后台账号名录**：每个账号一行，给出**账号名**、账号 id、身份（超级管理员/
    管理员/普通用户/杂鱼）、是否已冻结、是否禁言中。超级管理员的账号不在这份
    名单里。

    **要冻结/解冻某个账号、改某个账号的身份、给他禁言、给他发站内通知、重置他的对话
    额度之前，先用它确认那个账号存在、并拿到它的**账号名**。**那一族写工具只按
    **账号名**指认目标（没有编号通道），名字差一个字就是「后台没有这个账号」。

    只列出**账号名**，不列昵称：后台接口里就没有昵称这个字段，而主人用昵称称呼时
    应当如实说明找不到、或请他给账号名，不要拿昵称去猜。需要管理员身份。"""
    from agent import adminops_account as _account
    rows = _base._user_directory(config)
    if isinstance(rows, ToolResult):
        # **原样透传失败**：读不到名录 ≠ 名录是空的。渲染成"一个账号都没有"会让下一步
        # 的零写看起来是有依据的（同 `_find_named_user` 那条 fail-closed）。
        return rows
    listed = [r for r in (rows or {}).values() if isinstance(r, dict)]
    if not listed:
        # 空名录是**事实**（后端确实一个都没给），照实说、照常进回执（同
        # `list_admin_notes` 的"后台文章列表是空的"那一支）。**读不到不走这里**——
        # 上面已经原样透传了 `unavailable`。
        return empty(_account.render_account_roster([]))
    return ok(_account.render_account_roster(listed),
              meta={"count": len(listed),
                    "usernames": [str(r.get("username") or "").strip()
                                  for r in listed]})


def _account_frozen(row) -> bool | None:
    """名录里那一行"是不是冻结状态" → True/False；读不出这个字段 → **None**。

    判据 = `status != 0`，与 Rust `authz::is_frozen` 同一条（取值域见 `authz.rs`）。
    `None` **不是 False**：把"读不到"读成"正常"会让写后复核把一次失败的冻结判成功
    ——同 `_tag_index` 的 `None ≠ 空表` 那条纪律。
    """
    try:
        return int(row.get("status")) != 0
    except (TypeError, ValueError, AttributeError):
        return None


def _account_muted(row) -> bool | None:
    """名录里那一行"**现在**是不是处于禁言期" → True/False；读不出 → **None**。

    判据 = 后端**现算**的 `muted` 字段（Rust `authz::is_muted(muted_until, now)`：
    到期那一刻自然为假，没有定时任务去清那一列）。**不要**拿 `mutedUntil` 自己在
    agent 侧算一次——两套钟面/两处判据必然分叉，而这一格的消费方是弹卡幂等
    （"现在就是禁言中 ⇒ 不重复弹卡"）：判错的方向是**把一次真实的禁言当成已达成**
    ⇒ 不弹卡 ⇒ 主人永远办不成这件事（同 `_account_frozen` 那条方向）。

    `None` **不是 False**：键缺席或为 null 都算"读不出"（同「缺键绝不编 0」那条纪律
    ——把"没读到"读成"没被禁言"，写后复核就会把一次失败的禁言判成功）。
    """
    try:
        value = row.get("muted")
    except AttributeError:
        return None
    if value is None:
        return None
    return bool(value)


def _find_named_user(name, config, index=None):
    """按**账号名**在后台账号名录里找一个账号 → `(行, None)` 或 `(None, 拒绝文本)`。

    四种结局与 `_find_named_tag` 同取向（唯一命中才动手；同名多个如实说分不清、
    **不替主人挑一个**；查无此名如实说没有并附名字最接近的候选；名录读不到单独
    一种说法——"读不到" ≠ "没有"），**但有一处刻意的方向差异**：

      名录读不出来时这里**一个字节都不发**（调用方直接零写收场），而
      `graph._write_target_refusal` 在同一个情形下是"读不到就不拦"（放行给工具）。
      两边不是不一致，是**定位方式不同**：那边台账只用来印证一个已知的名字，读不到
      只损失一次预检；这边按名字定位是**唯一**的定位方式，读不到就没有任何可执行的
      落点——放行等于让 planner 拿一个没核过的名字去冻结一个活人。
      （不写这句，下一个人会把它当不一致"修"齐。）

    `index` 是可选的外部快照（一次操作要读同一份名录两回时用，同 `_find_named_tag`）。
    """
    if index is None:
        index = _base._user_directory(config)
    if isinstance(index, ToolResult):
        return None, str(index)
    want = str(name or "").strip()
    if not want:
        return None, "账号名为空，本次未改动"
    hits = [r for r in index.values()
            if str(r.get("username") or "").strip() == want]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        cands = "、".join(f"id={r.get('id')}" for r in hits)
        return None, (f"后台有 {len(hits)} 个账号都叫「{want}」（{cands}）："
                      f"无法确定要动的是哪一个，本次未改动——请让主人说清是哪一个"
                      f"（id 是后台账号列表里那一行的编号）")
    near = _base._near_miss_names(want, [(r.get("id"), str(r.get("username") or ""))
                                   for r in index.values()])
    if near:
        # 近失（同 `_near_miss_names` 长注）：没有完全同名的，但名字接近——把候选
        # 摆出来请主人点名，而不是丢一句"没有这个账号"（他记得后台里有）。
        cands2 = "、".join(f"id={cid}「{nm}」" for cid, nm in near)
        return None, (f"后台账号列表里没有叫「{want}」的账号（完全同名的一个都没有）；"
                      f"名字最接近的是 {cands2}。本次未改动——若就是其中一个，"
                      f"请照它的**完整账号名**再说一遍，我按那个名字动手")
    return None, f"后台账号列表里没有叫「{want}」的账号，本次未改动"


def _find_user_by_id(user_id, config, index=None):
    """按 **uid**（台账里的「账号 id=」）在后台账号名录里取那一行
    → `(行, None)` / `(None, 拒绝文本)`。

    额度批准/驳回的**第二条目标通道**（20260929 批 H · S2），与按账号名的
    `_find_named_user` 并列：目标来自系统摆上桌的待办台账，模型抄的是 uid。

    **仍然要过一次名录**（不是直接拿 uid 去发请求）：名录是"这个账号还在不在"的
    唯一事实源，也是复核基线（`limit`/写前用量）的来源；id 查不到就如实说"名录里
    没有这个编号"，而不是让后端回一句更含糊的拒绝。uid 认不出（不是正整数）同样
    零写——**不退回去按名字猜**。

    ⚠️ 与 `_find_named_user` 那处"方向差异"的注**不是同一条**：那里的读不到是指
    按名字定位无从落地；这里 uid 已经给了，读不到名录就只是"没核对上"，所以这条
    通道的读不到同样是零写（同 `_pre_read_fail` 的取向）。
    """
    from agent import adminops_target as _target
    if index is None:
        index = _base._user_directory(config)
    if isinstance(index, ToolResult):
        return None, str(index)
    uid = _target.normalize_target_id(user_id)
    if uid is None:
        return None, (f"「{str(user_id or '').strip()[:40]}」不是一个账号编号，"
                      f"本次未改动——请把待办台账里那个申请人的账号 id 原样抄过来")
    row = index.get(uid)
    if not isinstance(row, dict):
        # ⚠️ 这句话要**说准名录是什么**（20260930）：名录只列 `authz::is_listable_role`
        #    的账号——**超管不在里面**（账号族那道"按名字解析必须经过列表"的既有防线），
        #    注销过的账号同样不在。而额度队列是另一份数据：超管自己提交的申请照样在
        #    `?status=pending` 里列着。于是"队列里有这一行"与"名录里有这个人"可以是
        #    **两件事**，此时说一句光秃秃的「没有这个账号」会让人以为账号不存在。
        return None, (f"后台账号名录里没有编号为 id={uid} 的账号（名录不列超管账号、"
                      f"注销过的账号也不在），本次未改动")
    return row, None


def _reachable_uids(config: RunnableConfig):
    """后台**够得着**的账号 id 集合（读不到名录 → `None`，含义是"不知道"）。

    为什么单独立一条：额度写通道要求申请人在名录里（`_find_user_by_id`），而队列里
    可以出现名录够不着的人——**超管**（名录按 `is_listable_role` 过滤，超管不列）与
    **注销过的账号**（`quota_request` 无外键，销号不带走申请行）。这种行"等着办"却
    永远办不成：工具会如实拒绝，但只拒绝不标注，模型每轮都会去挑它、每轮白跑一次。

    所以读队列的地方顺手标一句"这一件 agent 办不了"——标的是**事实**（谁够得着），
    不是替模型作决定：模型仍可以选择把它报给主人。读不到名录 ⇒ `None` ⇒ 什么都不标
    （同"读不到 ≠ 没有"：不知道就别说话）。
    """
    idx = _base._user_directory(config)
    if isinstance(idx, ToolResult):
        return None
    return {uid for uid in idx if isinstance(uid, int)}


# 冻结/解冻每个方向的**结局句**——工具自己发音时用（回执）。卡面问句用的是
# adminops 的 `_ACCOUNT_CONSEQ`：两份措辞必须以同一件事为真（"会话不回来"这半边
# 两个方向都要说清），所以改一处必须看一眼另一处。
_ACCOUNT_DONE = {
    True: "他当前所有登录会话**已经全部失效**，在他被解冻之前连登录都进不来",
    False: "他现在**能重新登录**了（冻结期间被踢下线的会话不会自动恢复，需要他自己重新登录）",
}


def _set_account_frozen(name, frozen: bool, config: RunnableConfig) -> ToolResult:
    """旧图冻结/解冻兼容实现，两个工具分别表达操作方向。

    照 `delete_tag` 的五段式：① 读名录 → ② 解析出唯一一行 → ③ 写 → ④ 写后重读
    **同一份名录**复核 → ⑤ 出口只有 `ok` / `not_found` / `policy_frame` /
    `unavailable`，绝不 `return ""`。
    """
    from agent import adminops_account as _account
    want = str(name or "").strip()
    if not want:
        return unavailable("没给出要动的账号名，本次未改动——请让主人说清是哪个账号")

    # ① 写前读：既拿复核基线，也让"名字不存在"在**发请求之前**就响亮地报出来
    before_index = _base._user_directory(config)
    if isinstance(before_index, ToolResult):
        # 名录读不到 → 明说"本次未改动"（读侧那句原话留在括号里，便于排查）。
        return _base._pre_read_fail(before_index, "后台账号名录")
    # ② 解析：唯一命中才继续。**index 传进去**，于是这条路上的所有拒绝都是
    #    "目标不明"（读不到那一支已经在上面被挡掉了），出口统一按 not_found 走
    #    ——planner 的应对是换个名字或如实问主人，不是"稍后再试"。
    row, err = _base._find_named_user(want, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or want)
    was_frozen = _account_frozen(row)

    # ③ 兼容入口仍发送明确方向：冻结会撤销旧登录，即使可见状态已经冻结。
    # 下面的旧回执仅比较可见状态；完整副作用由原生 account_actions 计划与回执核验。
    data = _base._policy_post(f"/api/temp-users/{target_id}/status",
                              {"frozen": frozen}, config)
    if isinstance(data, ToolResult):
        # 政策拒绝（自己 / 超管 / 同级管理员）就走这一支：`_policy_post` 已经
        # 把后端的原话包成了 `policy_refused` 帧（checker BLOCK ⇒ 零回执 ⇒ 不进
        # 跨轮执行记忆），这里原样往外传，不改写一个字。
        return data

    # ④ 写后复核：重读**同一份名录**按 id 找回那一行。三种情形一律 unavailable
    #    （"…本次改动未确认生效"），**不许说成功**：读不回 / 那一行不见了
    #    （并发删号）/ 状态仍是旧值。
    after_index = _base._user_directory(config)
    if isinstance(after_index, ToolResult):
        return unavailable(f"改动请求已发出，但读不回后台账号名录（{after_index}），"
                           f"本次改动未确认生效")
    got = after_index.get(target_id)
    if not isinstance(got, dict):
        return unavailable(f"改动请求已发出，但读回的名录里找不到 id={target_id} 那一行"
                           f"（账号可能已被删除），本次改动未确认生效")
    now_frozen = _account_frozen(got)
    if now_frozen is None:
        return unavailable(f"改动请求已发出，但读回的账号「{username}」没有状态字段，"
                           f"无法确认，本次改动未确认生效")
    if now_frozen != frozen:
        verb = "冻结" if frozen else "解冻"
        state = "冻结" if now_frozen else "正常"
        return unavailable(f"{verb}请求已发出，但读回账号「{username}」的状态仍是"
                           f"「{state}」，本次改动未确认生效")

    changed = (was_frozen != now_frozen)
    return ok(
        _account.render_account_status(username, target_id, frozen, changed=changed,
                                before_frozen=was_frozen),
        meta=fact("account_freeze" if frozen else "account_unfreeze", changed=changed,
                  target=tgt("user", target_id, username),
                  before="冻结" if was_frozen else ("正常" if was_frozen is False else ""),
                  after="冻结" if now_frozen else "正常",
                  evidence="冻结" if now_frozen else "正常",
                  account_id=target_id, account_name=username,
                  change=_account.account_change_phrase(frozen, changed)))


@tool
def freeze_account(
    name: Annotated[str, "要冻结的那个后台账号的**账号名**（后台账号列表里看得见的那一行）"],
    config: RunnableConfig,
) -> str:
    """冻结一个后台账号：他立刻被踢下线，**在他被解冻之前连登录都进不来**。
    已登录的会话不会恢复（解冻后他需要重新登录）。需要管理员身份，且每次都要经主人确认。

    **要动的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。管理员之间不能互相冻结，超级管理员
    谁的账号都冻不了——撞上这两条时，如实把系统给的原话转告主人，不要换个说法重试。"""
    return _set_account_frozen(name, True, config)


@tool
def unfreeze_account(
    name: Annotated[str, "要解冻的那个后台账号的**账号名**（后台账号列表里看得见的那一行）"],
    config: RunnableConfig,
) -> str:
    """解冻一个被冻结的后台账号：他重新可以登录了。
    **冻结期间被踢下线的会话不会自动恢复**——解冻不等于"恢复原状"，他需要自己重新
    登录一次。需要管理员身份，且每次都要经主人确认。

    **要动的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。超级管理员的账号谁都解冻不了——
    撞上时如实转告系统给的原话，不要换个说法重试。"""
    return _set_account_frozen(name, False, config)


def _set_account_muted(name, muted: bool, hours, config: RunnableConfig) -> ToolResult:
    """旧图禁言/解除禁言兼容实现，两个工具分别表达操作方向。

    照 `_set_account_frozen` 的五段式：① 读名录 → ② 解析出唯一一行 → ③ 写 →
    ④ 写后重读**同一份名录**复核 → ⑤ 出口只有 `ok` / `not_found` / `policy_frame` /
    `unavailable`，绝不 `return ""`。

    与冻结族的三处差异（都是"禁言不是冻结"的直接后果）：
      · **多一个时长参数 `hours`**（冻结没有时长这个概念）：`None` ⇒ 永久，与后端
        `{hours: null}` 同义；认不出的字面**零写并如实问**，绝不静默按永久办
        ——那会把「禁他三天」办成「永久禁言」，而且办完谁也看不出来；
      · **写后复核的读数不是 `status` 而是现算的 `muted`**（`_account_muted`）；
      · **`changed` 的判据带上 `mutedUntil`**：对一个已在禁言期的账号再禁一次是
        **有意义的操作**（改时长/转永久，Rust 侧明确不做 no-op），只比布尔会把
        "把三天改成永久"读成"本来就是禁言中、本次未发生变更"（一句错话）。

    措辞（卡面与回执行）全在 adminops 那一份：本节**绝不复用冻结族的 `_ACCOUNT_DONE`**
    ——它写着"所有登录会话已经全部失效"，而禁言从不 bump `token_version`（Rust
    `set_user_muted` 头注），照抄过来是一句假话（同「消息壳架空判据」那族坑）。
    """
    from agent import adminops_account as _account
    want = str(name or "").strip()
    if not want:
        return unavailable("没给出要动的账号名，本次未改动——请让主人说清是哪个账号")
    ok_hours, hours_n = _account.normalize_mute_hours(hours)
    if not ok_hours:
        # 出口用 `not_found`（目标类失败）而不是 `unavailable`：后者的过程行是
        # 「服务不可用」、planner 收到的指引是"稍后再试"，而这条请求**永远不会**因为
        # 重试而成功；`not_found` 的指引恰好是这件事该做的——换个值、或如实问主人
        # 禁多久（措辞本身把这一步说清了）。主路径其实到不了这里：技能展开层对
        # 认不出的时长就零工具 + 问清了（同 `account_set_role` 的"认不出的身份"）。
        return not_found(
            f"「{str(hours or '').strip()[:40]}」不是一个能认下的禁言时长，本次未改动"
            f"——请让主人说清禁多久（几个小时，或者说「永久」）")

    # ① 写前读：既拿复核基线，也让"查无此名"在发请求之前就响亮地报出来。
    before_index = _base._user_directory(config)
    if isinstance(before_index, ToolResult):
        return _base._pre_read_fail(before_index, "后台账号名录")
    # ② 解析：唯一命中才继续（重名 ⇒ not_found，零写——选错就是禁了另一个活人）。
    row, err = _base._find_named_user(want, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or want)
    was_muted = _account_muted(row)
    before_until = _account.mute_until_raw(row)

    # ③ 写。**幂等不短路**：后端对"本来就是没被禁言"的解禁方向才是真 no-op，禁言
    #    方向永远不是——短路掉就分不出"刚禁的"与"本来就禁着"（同冻结族那条）。
    data = _base._policy_post(f"/api/temp-users/{target_id}/mute",
                        {"muted": muted, "hours": hours_n if muted else None}, config)
    if isinstance(data, ToolResult):
        # 政策拒绝（自己 / 超管 / 同级管理员）走这一支：`_policy_post` 已经把后端的
        # 原话包成 `policy_refused` 帧，这里原样往外传，不改写一个字。
        return data

    # ④ 写后复核：重读**同一份名录**按 id 找回那一行。三种情形一律 unavailable
    #    （"…本次改动未确认生效"），**不许说成功**。
    after_index = _base._user_directory(config)
    if isinstance(after_index, ToolResult):
        return unavailable(f"改动请求已发出，但读不回后台账号名录（{after_index}），"
                           f"本次改动未确认生效")
    got = after_index.get(target_id)
    if not isinstance(got, dict):
        return unavailable(f"改动请求已发出，但读回的名录里找不到 id={target_id} 那一行"
                           f"（账号可能已被删除），本次改动未确认生效")
    now_muted = _account_muted(got)
    if now_muted is None:
        return unavailable(f"改动请求已发出，但读回的账号「{username}」没有禁言字段，"
                           f"无法确认，本次改动未确认生效")
    if now_muted != muted:
        verb = "禁言" if muted else "解除禁言"
        state = "禁言中" if now_muted else "正常"
        return unavailable(f"{verb}请求已发出，但读回账号「{username}」仍是「{state}」，"
                           f"本次改动未确认生效")

    after_until = _account.mute_until_raw(got)
    if muted:
        # 禁言方向：期限变了（含"首次禁言"）就是一次真变更——同长度的再次禁言也会
        # 得到一个更晚的时刻，那是真事（他确实又被禁了 N 小时），照实报"已禁言"。
        changed = (was_muted, before_until) != (now_muted, after_until)
    else:
        # 解禁方向的判据与后端 no-op 同源（Rust 用的是 `muted_until is None`）：
        # 库里本来就没留值 ⇒ 这一次什么都没发生；留过值（哪怕是已过期的残留）
        # ⇒ 这一下把它清掉了，是真变更。
        changed = before_until != ""
    return ok(
        _account.render_account_mute_status(username, target_id, muted,
                                     until_raw=after_until, changed=changed,
                                     before_muted=was_muted),
        meta=fact("account_mute" if muted else "account_unmute", changed=changed,
                  target=tgt("user", target_id, username),
                  before=("禁言中" if was_muted else
                          ("正常" if was_muted is False else "")),
                  after="禁言中" if now_muted else "正常",
                  evidence="禁言中" if now_muted else "正常",
                  account_id=target_id, account_name=username,
                  change=_account.account_mute_change_phrase(muted, changed,
                                                      until_raw=after_until)))


@tool
def account_mute(
    name: Annotated[str, "要禁言的那个后台账号的**账号名**（后台账号列表里看得见的那一行）"],
    config: RunnableConfig,
    hours: Annotated[int | None, "禁言时长（小时）。主人没给时长就**不要填**（不填=永久）"] = None,
) -> str:
    """禁言一个后台账号：他**照常登录、浏览文章、跟你对话**，只是发不出评论与留言。
    与冻结是两件事——禁言**不会**让他掉线，也不会让他的会话失效。需要管理员身份，
    且每次都要经主人确认。

    `hours` 是禁言时长（小时）；**不填 = 永久**（解禁要再单独说一次）。
    **要动的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。管理员之间不能互相禁言，超级管理员
    谁的账号都禁不了——撞上这两条时，如实把系统给的原话转告主人，不要换个说法重试。"""
    return _set_account_muted(name, True, hours, config)


@tool
def account_unmute(
    name: Annotated[str, "要解除禁言的那个后台账号的**账号名**（后台账号列表里看得见的那一行）"],
    config: RunnableConfig,
) -> str:
    """解除一个账号的禁言：他重新可以发布评论与留言。**与冻结/解冻无关**
    （禁言从不影响登录，所以解禁也不会"让他回来"——他本来就在）。需要管理员身份，
    且每次都要经主人确认。

    **要动的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。超级管理员的账号谁都解禁不了——
    撞上时如实转告系统给的原话，不要换个说法重试。"""
    return _set_account_muted(name, False, None, config)


def _set_account_role(name, role, config: RunnableConfig) -> ToolResult:
    """变更一个账号的身份（20261002：下放给管理员 + agent 代理）。

    五段式同 `_set_account_frozen`：① 读名录 → ② 按名字解析出唯一一行 → ③ 写 →
    ④ 写后重读**同一份名录**复核 → ⑤ 出口只有 `ok` / `not_found` / `policy_frame` /
    `unavailable`。

    **这一处刻意不写第二份权限表**（用户拍板）。能改谁、能改成什么，全由后端
    `authz::check_role_change` 一项判据说了算，agent 只负责转述它的原话——理由是
    **政策只有一份实现**，而"更保守地预检一遍"在这件事上已经栽过一次：冻结预检
    （`graph._freeze_policy_refusal`）曾把"管理员冻杂鱼"拦成一句**说错政策**的
    「管理员之间不能互相冻结」。同族论证见 `_policy_post` 头注。
    所以这里连"目标身份认不认得出"都不判：认不出的身份交给后端回「站内没有这个身份」
    （一句真话），agent 侧预判只会得到一句**像诚实拒绝的错话**。

    **幂等不短路**（同冻结那一支）：目标已经是目标身份时照样发请求，结论由复核给。
    """
    from agent import adminops_account as _account
    want = str(name or "").strip()
    if not want:
        return unavailable("没给出要改的账号名，本次未改动——请让主人说清是哪个账号")
    new_role = str(role or "").strip()
    if not new_role:
        return unavailable("没给出要改成什么身份，本次未改动——请让主人说清改成哪个身份")

    # ① 写前读：既拿复核基线，也让"名字不存在"在**发请求之前**就响亮地报出来
    before_index = _base._user_directory(config)
    if isinstance(before_index, ToolResult):
        return _base._pre_read_fail(before_index, "后台账号名录")
    row, err = _base._find_named_user(want, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or want)
    was_role = str(row.get("role") or "").strip()

    # ②' 身份归一：把"杂鱼/普通用户/zako"这类说法翻成后端认的身份码。
    #     翻不出**不在这里拒**（同头注那条"不写第二份政策表"）：原样把它交给后端，
    #     让后端回一句『站内没有这个身份』——那是政策的原话，而 agent 自己编一句
    #     「认不出这个身份」会是**另一句话**，且它与政策漂移时无人发现。
    sent_role = _account.normalize_role(new_role) or new_role

    # ③ 写。方向由**参数字面**决定（技能展开层已经把它归一过一次；这里再归一一次
    #    是为了让直接调用本工具的路径也拿到同一个值）。
    data = _base._policy_post(f"/api/temp-users/{target_id}/role",
                        {"role": sent_role}, config)
    if isinstance(data, ToolResult):
        # 政策拒绝（自己 / 超管 / 越档 / 身份不存在…）走这一支：`_policy_post` 已经
        # 把后端的原话包成了 `policy_refused` 帧，这里原样往外传，不改写一个字。
        return data

    # ④ 写后复核：重读**同一份名录**按 id 找回那一行。四种情形一律 unavailable
    #    （"…本次改动未确认生效"），**不许说成功**：读不回 / 那一行不见了（并发删号）/
    #    那一行没有身份字段 / 身份仍是旧值。
    after_index = _base._user_directory(config)
    if isinstance(after_index, ToolResult):
        return unavailable(f"改动请求已发出，但读不回后台账号名录（{after_index}），"
                           f"本次改动未确认生效")
    got = after_index.get(target_id)
    if not isinstance(got, dict):
        return unavailable(f"改动请求已发出，但读回的名录里找不到 id={target_id} 那一行"
                           f"（账号可能已被删除），本次改动未确认生效")
    raw_now = got.get("role")
    now_role = str(raw_now or "").strip()
    if not now_role:
        return unavailable(f"改动请求已发出，但读回的账号「{username}」没有身份字段，"
                           f"无法确认，本次改动未确认生效")
    if now_role != sent_role:
        return unavailable(f"变更身份的请求已发出，但读回账号「{username}」的身份仍是"
                           f"「{_account.role_cn(now_role)}」，本次改动未确认生效")

    changed = (was_role != sent_role)
    return ok(
        _account.render_account_role_status(username, target_id, sent_role,
                                     before_role=was_role, changed=changed),
        meta=fact("account_set_role", changed=changed,
                  target=tgt("user", target_id, username),
                  before=_account.role_cn(was_role) if was_role else "",
                  after=_account.role_cn(sent_role),
                  evidence=_account.role_cn(now_role),
                  account_id=target_id, account_name=username,
                  change=_account.account_role_change_phrase(sent_role, changed)))


@tool
def set_account_role(
    name: Annotated[str, "要改身份的那个后台账号的**账号名**（后台账号列表里看得见的那一行）"],
    role: Annotated[str, "要改成什么身份（照抄主人说的那个说法，例如「杂鱼」「普通用户」；"
                         "英文码 zako / user 也认）——**能改成哪几档由系统判定**，不要自己挑"],
    config: RunnableConfig,
) -> str:
    """变更一个后台账号的权限身份（例如把普通用户设成杂鱼、或把杂鱼改回普通用户）。
    **改完他当场被踢下线**（所有已登录的会话立刻失效），需要重新登录一次。需要管理员
    身份，且每次都要经主人确认。

    **只按账号名指认**：要动的账号名必须能在后台账号列表里看到，列表里没有这个名字
    就当它不存在（不要用账号编号，也不要自己拼一个名字）。能改谁、能改成什么由系统
    判定**并给出原话**——被拒时如实把那句话转告主人，**不要**换个身份或换个账号重试。"""
    return _set_account_role(name, role, config)


# 通知的标题/正文上限与**服务端同一处口径**（`src/routes/notice.rs` 的 `TITLE_MAX` /
# `CONTENT_MAX`，前端 `Users/index.tsx` 的 maxLength 也是这两个数）。三处必须一致：
# 前端拦一道、展开层拦一道（零工具 + 说清原因）、工具这一道是硬闸；服务端那一道是
# 最终判据，超长会被拒（**不是**静默截断——见 notice.rs 头注）。
_NOTICE_TITLE_LIMIT = 128
_NOTICE_CONTENT_LIMIT = 1000
# 标题留空时用的默认标题。**是系统写的字**（服务端 `notice::DEFAULT_TITLE` 一处）。
# 这里重复一份只为"工具自己也知道最终会是什么标题"（回执里念得出那个字），
# 真正落库的字由服务端决定。
_NOTICE_DEFAULT_TITLE = "站内通知"


def _send_user_notice(name, content, title, config: RunnableConfig) -> ToolResult:
    """给一个账号发一条站内通知（**只有**这一条写通道）。

    五段式的前两段照 `_set_account_frozen`（① 读名录 → ② 按名字解析出唯一一行），
    后三段**不同族**，三条都是"这一件事没有别的载体"的直接后果：

      · **没有写后复核那条腿**。站内**不存在**"读别人的通知"的通道
        （`/api/protected/notifications` 一族是 own-data only，`profile.rs` 只按自己的
        uid 查），所以公告族那种"建完读回清单、按新旧 id 差集认人"在这里**结构上做不到**。
        复核判据 = **端点回执本身**：`temp_user::send_user_notice` 只在
        `notice::push_notice_checked` 插入成功时才回 code 200（那条 Result 直接决定
        成败分支）——**这是后端契约**，后来人若把"插入失败也回 200"带进来，这一层的
        "成功"就同时失真了。别以为这里漏写了一条腿。
      · **不重读名录**。重读能证明的只是"账号还在"，证明不了通知发出去了——那会是一段
        看着像复核、实际什么都没核的空转（同族里唯一没有读回复核的一件，理由写在这里）。
      · 正文**可以不是主人的原话**（用户拍板：允许把主人的意思整理成一句得体的通知），
        所以这里的措辞纪律与公告族相反：公告要求"只写用户给了的"，这里允许整理，但
        **不许添加主人没说过的事实、承诺或威胁**（「你已被警告三次」这类编造），
        而**唯一的人眼复核点是确认卡**（`adminops.render_notice_action` 把正文**全文**
        印出来）——那条纪律是硬要求，不是措辞偏好。
    """
    from agent import adminops_account as _account
    want = str(name or "").strip()
    if not want:
        return unavailable("没给出要发给哪个账号，本次未发送——请让主人说清是哪个账号")
    body_text = str(content or "").strip()
    if not body_text:
        # 空正文**不发**：一条什么都不说的通知对收件人只是打扰，而且它是一段以主人名义
        # 发出去、删不掉的字（见 notice.rs 那条"发出后没有撤回的通道"）。
        return unavailable("通知正文为空，本次未发送")
    if len(body_text) > _NOTICE_CONTENT_LIMIT:
        return unavailable(f"通知正文太长（{len(body_text)} 字，上限 {_NOTICE_CONTENT_LIMIT} 字），"
                           f"本次未发送")
    head = str(title or "").strip() or _NOTICE_DEFAULT_TITLE
    if len(head) > _NOTICE_TITLE_LIMIT:
        # 超长在**校验**这一层拒（不静默截断）：截断会让主人核对的是没被截的那一句、
        # 库里存的是另一句（同 notice.rs 头注那条取舍）。
        return unavailable(f"通知标题太长（{len(head)} 字，上限 {_NOTICE_TITLE_LIMIT} 字），"
                           f"本次未发送")

    # ① 写前读：既拿"这个名字在不在名录里"的判据，也让"查无此名"在**发请求之前**
    #    就响亮地报出来（同 `_set_account_frozen`）。
    before_index = _base._user_directory(config)
    if isinstance(before_index, ToolResult):
        return _base._pre_read_fail(before_index, "后台账号名录")
    # ② 解析：唯一命中才继续（重名 ⇒ not_found，零写——选错就是给**另一个活人**发了一段话）。
    row, err = _base._find_named_user(want, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or want)

    # ③ 写。失败按原因码分区（见 `_admin_notice_post` 头注），**原样往外传**：
    #    目标类走 not_found（planner 去问主人），其余走 unavailable（不许说成功）。
    data = _base._admin_notice_post(target_id, head, body_text, config)
    if isinstance(data, ToolResult):
        return data
    # ④ 回执即复核判据（这一族没有第二条腿，见头注）：端点回了 code 200 ⇒
    #    服务端那次 `push_notice_checked` 插入成功。
    return ok(_account.render_notice_status(username, target_id, head, body_text),
              meta=fact("notice_send", changed=True,
                        target=tgt("user", target_id, username),
                        before="未发送", after="已发送", evidence=head,
                        account_id=target_id, account_name=username))


@tool
def send_user_notice(
    name: Annotated[str, "收件人的**账号名**（后台账号列表里看得见的那一行）"],
    content: Annotated[str, "通知正文（可以是一句整理过的话；不要编造主人没说过的事实、"
                            "承诺或威胁）"],
    config: RunnableConfig,
    title: Annotated[str | None, "（可选）通知标题：一句话；主人没说标题就不填，"
                                 "**不要自己挑一个**"] = None,
) -> str:
    """给一个后台账号发一条站内通知（会出现在**对方**个人中心的通知面板里，**发出后没有撤回的通道**）。

    **收件人的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。正文可以按主人的意思整理成一句得体的
    通知，但**不许**添加主人没说过的事实、承诺或威胁。需要管理员身份，且每次都要经
    主人确认（确认卡上会印出**正文全文**，主人核对的就是将要发出的那一句）。"""
    return _send_user_notice(name, content, title, config)
