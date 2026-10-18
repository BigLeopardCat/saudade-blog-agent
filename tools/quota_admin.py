# -*- coding: utf-8 -*-
"""管理助手写工具：对话额度的申请审核（只读队列 + 恢复满额）—— 20261018 拆 `tools/base.py`。

20261018 拆 `tools/base.py`。**为什么单独成模块**：这一族是 `tools/base.py` 里**连续的
387 行、12 个模块级名字**（三张拒绝话术表 + 三个帮手 + 四件工具），scope 全是
`admin.console` / `write.console`。它自己就是一个完整闭环：**读申请队列 → 按账号名或 uid
解析唯一目标 → 代管理员恢复满额 → 读回复核**。

## 这一族独有的事实（实现为什么长这样）
  ① **恢复额度的两个入口是"两条目标通道"而不是一个带开关的工具**：`approve_quota_request`
     按**账号名**（名录里看得见的那一行）、`reset_user_quota` 也按名字——**没有编号门**。
     20260929 批 H · S2 之前只有 uid 通道，于是"给某个没申请过的人重置额度"这条今天真能
     办的事被静默丢掉；名字→uid 的解析在工具侧对着**实时名录**做，唯一命中才动手。
  ② **只读的那件（`list_quota_requests`）要标出"哪几件 agent 办不了"**：够得着的与够不着
     的在同一屏里必须长得不一样，否则模型每次都挑到够不着的那件、每次都白跑
     （标注用的 uid 集合来自 `_reachable_uids`）。
  ③ **拒绝的形态是后端的政策**（"没有待处理的申请"/"这条申请已经处理过了"/"用户不存在"），
     所以两张拒绝话术表与 `_NO_SUCCESS_TAIL` 是**给主人看的原话**，不是判定分支的输入。
  ④ **口径是"余额"不是"计数器清零"**（20260929 用户反馈）：给主人看的话一律说余额。

## 搬家时守的一条
⚠️ **所有共享帮手一律写成 `_base.<名>(...)`**：`_user_directory`（族内 3 处、套件打桩
**24 处**）、`_admin_get`（2 处、50 处）、`_quota_pending_index`（2 处、2 处）、
`_find_user_by_id`（1 处）、`_find_named_user`（1 处、套件另有一处 `base._find_named_user = …`）、
`_device_get_user_id`（1 处、2 处）、`_sign_local_jwt`（1 处、2 处）、`_client`（1 处、29 处）
都是**被套件直接打桩**的模块全局（`patch(**kw)` + `setattr(tools.base, k, v)`）；
`_pre_read_fail` / `_reachable_uids` / `_NO_SUCCESS_TAIL` / `_NO_IDENTITY_WRITE` /
`ADMIN_BASE` / `logger` 虽然没人打桩，但本模块**一个名字都不按名从 `tools.base` 取**
——模块级 from-import 会让 mypy 在这个环上判不出被 re-export 的名字的类型。
`logger` 走 `_base.logger`：告警行名必须与搬家前逐字相同。
"""

from typing import Annotated

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, empty, fact, not_found, ok, tgt, unavailable

# `__all__` = 本模块**所有不以 `_` 开头**的模块级名字（社区惯例，不留例外）：
# 四件 `@tool` 是工具本体。三张话术表与三个帮手是下划线名，由 `tools/base.py`
# 按原名显式 re-export 供给（显式 import 不受 `__all__` 约束）。
__all__ = ["approve_quota_request", "list_quota_requests", "reject_quota_request",
           "reset_user_quota"]


# ── 管理助手写工具：对话额度的申请审核（20260929）──────────────────────────
# 背景（三端都核过的现状）：每个**普通用户**终身 500 轮（含 embedding 检索轮），
# 管理员不设限；用尽之后由 **Rust 侧硬拦**（agent 只负责把那一轮答成一句人设话术，
# 见 server.py 的 `_quota_blocked_stream`），唯一恢复途径 = 管理员把它恢复满。
# 本节的三个写工具是**代管理员**执行"恢复满额"那一半，外加一件只读的申请队列。
# （措辞：给主人看的话一律说**余额**，不说"计数器清零"——20260929 用户反馈，
#  见 `agent/adminops.py` 的 `_QUOTA_CONSEQ` 上面那段。）
#
# 两条跨语言契约（改一侧必须改另一侧，`tests/test_chat_quota.py` 有守卫）：
#   · `GET /api/temp-users` 每行多 `chatQuotaUsed` / `chatQuotaLimit`（0 = 不限额）
#     ——所以"这个人用了多少"**不需要新查询**：冻结/通知族本来就要读那份名录
#     （见 `_user_directory` 那条"不要包信封"的长注）。只加字段是安全的。
#   · 申请行的状态是**三值**（0 待处理 / 1 已批准 / 2 已驳回），服务端用
#     `WHERE id=? AND status=0` **原子认领**——认领不到 ⇒ 零副作用。所以
#     「这条申请已经处理过了」不是故障，是并发下的正常结局，必须如实转述。
#
# **目标一律走名字通道**（与冻结/通知族同一条）：申请人由**账号名**定位，行 id 是
# 模型永远猜不出的内部键。代价如实记：管理员在后台列表里看到的是账号名，与这里
# 要求的输入**同一张表**，所以主人说得出、也核得对。
#
# 属于**政策/状态类**的三句（跨语言契约，见 `src/routes/quota.rs` 头注与
# `docs/security-boundary.md` §7⑬——⚠️ 是 **⑬**，⑫ 是令牌收回那节）。三句的共性 =
# **再试一次也是同一个结果**，所以走 `policy_frame`（错误帧族）：checker 判 BLOCK ⇒
# **零回执**、不进跨轮执行记忆，planner 拿到的原因码叫它"如实转述、别改参重试"。
# 这一条同时消灭了一个具体风险：管理员重复点通过时，**绝不会**在台账里留下第二笔"已恢复"。
# ⚠️ **三句的来源不一样**（20260929 核过源码，别把它们一律写成"后端措辞"）：
#   前两句是 Rust 真会发的（`这条申请已经处理过了` = 认领不到 / `你已经有一份待处理的
#   申请了` = 重复申请）；**第三句是本节自己合成的**——它在"这个 uid 在当前 pending
#   列表里没有行"时由 `_reset_or_review` 那条支路直接产出，Rust 侧从不发这句话。
#   放进同一个元组是因为**分族行为相同**（都不许改参重试），不是因为来源相同。
_QUOTA_POLICY_REFUSALS = ("该账号没有待处理的额度申请", "这条申请已经处理过了",
                          "你已经有一份待处理的申请了")
# 属于**目标类**的一句（planner 的应对是换一个账号 / 问主人，不是"稍后再试"）。
_QUOTA_TARGET_REFUSALS = ("用户不存在",)
# 申请人填的理由上限（500 字）：`quota_request.reason` 是 text 列，500 是**产品上限**
# 而不是列宽——超限**拒绝、不截断**（截断会让主人核对的是这一句、库里存的是另一句，
# 同 notice.rs 头注那条取舍）。
# ⚠️ 唯一事实源是 Rust `quota::REASON_MAX`（它真的拦人）；
# Python 侧**故意不抄这个数**（20261018 A1 删了零引用副本）。
# 管理员驳回理由的上限 = **迁移里 `quota_request.note` 的列宽**（varchar(255)），
# 同时它会被原样拼进发给申请人的通知正文。这条理由**必须**与列宽对齐：写超了
# 后端要么报错要么截断，而截断意味着申请人收到的理由与主人核对的那一句不同。
_QUOTA_NOTE_LIMIT = 255


def _admin_quota_post(path: str, payload: dict, config: RunnableConfig):
    """额度审核/重置专用的 POST：非 200 业务码**按"是目标类还是政策类"分两族**。

    这个端点的非 200 有五种来源，落到这里只剩两族的区分有意义：

      · **目标类**（`用户不存在`）⇒ `not_found`：planner 拿到这个原因码才会去
        问主人 / 换一个账号；
      · **政策/状态类**（`这条申请已经处理过了` / `你已经有一份待处理的申请了`，
        外加本节自己合成的那句 `该账号没有待处理的额度申请`）⇒ `policy_frame`：
        三句的共同点是**再试一次也还是它**（别人刚处理过、他没申请过、他早申请
        过了），所以归政策族，让 planner 如实转述而**不是**改参重试；
      · **其余一律 unavailable**（含真·存储故障、以及任何我们没见过的措辞）⇒
        明说"未确认"，不替后端断言一个我们并不知道的原因。
        ⚠️ 后端对"这个 id 找不到申请"说的是 `没有找到这条额度申请`，**不在**上面
        任何一族里 ⇒ 它会落到这一档（"未确认"）。这是**安全方向**，而且按现在的
        路径走不到：agent 发 POST 之前已经先读过 pending 列表、没有行就自己早退了
        （见 `_review_quota_request` ③）。真要接住它，得先想清"再试一次有意义吗"。

    方向刻意与 `_policy_post`（冻结族，非 200 **无条件**读成政策拒绝）不同：
    那一族的非 200 没有一族是"服务不可用"，所以无条件按政策出口是对的；这一族有
    真·存储故障，一律按政策/目标出口会把"库写失败"说成"没这个申请"——一句假话，
    而且后果是**额度没有清零而主人以为清了**。按消息字符串匹配有代价（后端改字就
    认不出），接受的依据是**兜底方向**：认不出 ⇒ unavailable（"没确认"），永远
    不会断言一个假的结论。那几句已登记为跨语言契约，改它们要走
    `docs/security-boundary.md §7⑬` 那条同步流程（⚠️ 是 **⑬**：⑫ 是令牌收回与
    账号冻结那节；20260929 之前这里指向 ⑫，是一处指错的引用）。
    """
    from agent import adminops_target as _target
    uid = _base._device_get_user_id(config)
    if uid <= 0:
        # 身份不明时一个请求都不发（同 `_principal_request`）。
        return unavailable(_base._NO_IDENTITY_WRITE)
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer "
               + _base._sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _base._client.post(f"{_base.ADMIN_BASE}{path}", headers=headers,
                                  json=payload, timeout=15)
    except Exception as exc:
        _base.logger.error("admin quota post %s failed: %s", path, exc)
        return unavailable(f"接口请求失败: {exc}{_base._NO_SUCCESS_TAIL}")
    if resp.status_code in (401, 403):
        return unavailable("当前身份无权处理额度申请（该功能仅管理员可用），本次未改动任何内容")
    if resp.status_code != 200:
        return unavailable(f"接口返回 HTTP {resp.status_code}{_base._NO_SUCCESS_TAIL}")
    try:
        body = resp.json()
    except Exception:
        return unavailable(f"接口返回的不是 JSON{_base._NO_SUCCESS_TAIL}")
    if body.get("code") != 200:
        msg = str(body.get("message") or "")
        _base.logger.warning("admin quota post %s refused: %s", path, msg)
        for phrase in _QUOTA_TARGET_REFUSALS:
            if phrase in msg:
                return not_found(msg or "后台账号列表里没有这个账号")
        for phrase in _QUOTA_POLICY_REFUSALS:
            if phrase in msg:
                # ⚠️ 必须包成 `ToolResult`（`policy_frame` 返回的是普通 str）：调用方的
                # 失败判据是 `isinstance(data, ToolResult)`，裸传会被当成"成功返回的
                # data"接着往下走写后复核，最后报成「已受理…未确认生效」——一句假话
                # （这条请求根本没改任何东西，它是被状态判据挡下的）。同
                # `_policy_post` 里那段一模一样的警告。
                return ToolResult(_target.policy_frame(msg))
        return unavailable(f"后台拒绝了这次额度操作（{msg or '没有给出原因'}）"
                           f"{_base._NO_SUCCESS_TAIL}")
    return body.get("data")


def _quota_pending_index(config: RunnableConfig):
    """读**待处理**的额度重置申请 → `{uid: 行}`；读不到 → `ToolResult`。

    为什么按 uid 建索引而不是按行 id：**uid 是模型看得见、可核对的目标**（台账与
    `list_quota_requests` 都印「账号 id=」），而行 id（`rid`）是模型永远猜不出、
    也永远不需要知道的内部键——它由本函数的结果就地取给写通道。"这个账号有没有
    待处理的申请"因此是**一次读同时回答两件事**：定位（拿到行 id）与预检（没有
    这一行就不发请求、直接如实说"他现在没有待处理的申请"——那比让后端回一句
    拒绝更早、更清楚）。

    返回的是 `ToolResult` 而不是 `None`：调用方在**任何**读不到的情况下都必须零写
    （同 `_user_directory` 那条纪律），原样往外传一个带人话的 ToolResult 最好用。
    """
    data = _base._admin_get("/api/protected/quota/requests?status=pending", config)
    if isinstance(data, ToolResult):
        return data
    if not isinstance(data, list):
        # 形状不对 = 后端换了契约。**不当成"没有待处理的申请"**：那会让下一步
        # 说出"他没有申请"——一句可能是假话的断言（同 `_user_directory` 那条）。
        return unavailable("后台额度申请列表返回的形状不对（不是数组），读不出待处理的申请")
    out: dict[int, dict] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            uid = int(row.get("userId"))
        except (TypeError, ValueError):
            continue
        out[uid] = row
    return out


@tool
def list_quota_requests(
    config: RunnableConfig,
    status: Annotated[str | None,
                      "看哪些：「pending」只看待处理的（默认），「all」连已处理的一起看"] = None,
) -> str:
    """查看用户提交的**对话额度重置申请**清单：每行给出申请人的**账号 id**、账号名、
    他现在用了多少轮、申请理由与状态。

    批准或驳回某人之前，先用它确认**申请人的账号 id**（`账号 id=` 就是写通道收的
    目标；编号必须真实存在于账号名录里，系统在写之前还会对着现场队列再校验一次）。
    要改某个用户的额度而不涉及他的申请，用不上这个工具——直接按账号名重置即可
    （主动重置仍走名字通道）。需要管理员身份。"""
    from agent import adminops_quota as _quota
    want = str(status or "").strip().lower()
    q = "all" if want in ("all", "全部", "所有") else "pending"
    data = _base._admin_get(f"/api/protected/quota/requests?status={q}", config)
    if isinstance(data, ToolResult):
        return data
    if not isinstance(data, list):
        # 形状不对 = 后端换了契约（信封而不是裸数组）。**不当成"一条都没有"**：那会让
        # 下一个人说出"没有申请"——一句可能是假话的断言（同 `_quota_pending_index`）。
        return unavailable("后台额度申请列表返回的形状不对（不是数组），读不出申请清单")
    rows = data
    if not rows:
        return empty("现在没有待处理的额度重置申请（一条都没有）" if q == "pending"
                     else "额度重置申请的记录是空的（一条都没有）")
    # 名录里够不着的行（超管 / 已注销）**照印但标注**——这一屏的用途正是"挑一件来办"，
    # 不标的话模型每次都挑到那一件、每次都白跑（见 `_reachable_uids`）。
    return ok(_quota.render_quota_requests(rows, _base._reachable_uids(config)),
              meta={"count": len(rows), "status": q})


def _quota_readback(config: RunnableConfig, target_id: int, username: str, limit, kind: str):
    """写后重读名录 → 回执行；读不回 / 缺字段 → `unavailable`（"未确认"）。

    **这一族的复核读的是"那件被改的东西本身"**（计数器），不是"账号还在不在"：
    重读名录只是为了拿到同一个人的那一行，判据是行里的 `chatQuotaUsed`。
    三种取值分开说（见 `adminops.render_quota_status` 头注）：0 = 已确认恢复满；
    >0 = 他在这之后又聊过了（如实报实测值，**不许**照抄"已恢复"）；读不出 = 未复核。
    """
    from agent import adminops_quota as _quota
    after = _base._user_directory(config)
    if isinstance(after, ToolResult):
        return unavailable(f"改动请求已发出，但读不回后台账号名录（{after}），"
                           f"本次改动未确认生效")
    got = after.get(target_id)
    if not isinstance(got, dict):
        return unavailable(f"改动请求已发出，但读回的名录里找不到 id={target_id} 那一行"
                           f"（账号可能已被删除），本次改动未确认生效")
    return ok(_quota.render_quota_status(kind, username, target_id, limit,
                                    _quota.quota_used(got)),
              meta=fact(kind, changed=True, target=tgt("quota_request", target_id, username),
                        before="已用未清零", after="已恢复到上限",
                        evidence=f"已用 {_quota.quota_used(got)}",
                        account_id=target_id, account_name=username))


def _review_quota_request(user_id, approved: bool, reason, config: RunnableConfig) -> ToolResult:
    """批准 / 驳回某人的额度重置申请（两个 @tool 是方向不同的薄壳，同冻结族）。

    照 `_set_account_frozen` 的五段式：① 读名录 → ② 按 **uid** 取出那一行 →
    ③ 读**待处理申请**（定位 + 预检，读不到/没有他那一行都零写）→ ④ 写 →
    ⑤ 写后复核 → 出口只有 `ok` / `not_found` / `policy_frame` / `unavailable`。

    **目标走 id 通道**（20260929 批 H · S2）：参数是系统摆上桌的台账里那个
    「账号 id=」——申请行 id（`rid`）由系统从 `_quota_pending_index[uid]` 自己取，
    模型永远不需要知道它（它是模型猜不出的内部键）。**仍然过一次名录**：uid 必须
    在场且真存在，见 `_find_user_by_id` 头注。

    复核分两个方向，因为**两个方向改变的东西不同**：
      · **批准**恢复额度满额 ⇒ 重读名录判 `chatQuotaUsed`（同主动重置，走
        `_quota_readback`）；
      · **驳回**不改变任何额度 ⇒ 重读**待处理申请**，判"那一行已经不在了"
        （这正是服务端那次原子认领的可见效果）。用同一条读回复核去判额度在这里
        是错的——驳回之后他的额度本来就还是用尽的样子，判它必然误报"未生效"。
    """
    from agent import adminops_quota as _quota
    from agent import adminops_target as _target
    note = str(reason or "").strip()
    if not approved and not note:
        # 驳回理由必填（用户拍板）。为零的理由不是"空着没关系"：那条通知的正文
        # 会回落成「管理员没有填写理由」，而申请人收到的是一句什么都没说的话。
        # **校验放在发请求之前**（同 `_send_user_notice` 的空正文）。
        return unavailable("驳回理由为空，本次未改动——驳回要告诉他为什么，请让主人给一句理由")
    if len(note) > _QUOTA_NOTE_LIMIT:
        return unavailable(f"驳回理由太长（{len(note)} 字，上限 {_QUOTA_NOTE_LIMIT} 字），"
                           f"本次未改动")

    # ① 写前读：既拿复核基线（`limit`、写前的用量），也让"这个编号不在名录里"在
    #    发请求之前就响亮地报出来。
    before_index = _base._user_directory(config)
    if isinstance(before_index, ToolResult):
        return _base._pre_read_fail(before_index, "后台账号名录")
    # ② 解析：uid 必须在名录里（不在 ⇒ not_found，零写——选错就是批了**另一个活人**）。
    # ⚠️ `index=` 必须写成关键字（同一行曾写成 `_base._find_user_by_id(user_id, before_index)`，
    # 于是名录快照落进了 `config` 形参、`index` 仍是 None ⇒ 该函数自己拿快照去当
    # config 再读一次名录 ⇒ 读到 uid=0 ⇒ 回一句「无法获取当前用户身份」（**假话**：
    # 身份好着，是调用姿势错了）⇒ 批准/驳回整条通道自 4cb1f93 起一次都没成功过。
    # 定义处的三件同族（`_find_named_user` / `_find_named_tag` / `_find_board_comment_by_id`）
    # 都是这个形状，调用方一律写 `index=`）。
    row, err = _base._find_user_by_id(user_id, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or f"账号 id={target_id}")
    limit = _quota.quota_limit(row)

    # ③ 读待处理申请：定位（拿行 id）与预检（没他那一行就不发请求）一次完成。
    pending = _base._quota_pending_index(config)
    if isinstance(pending, ToolResult):
        return _base._pre_read_fail(pending, "待处理的额度申请列表")
    entry = pending.get(target_id)
    if not isinstance(entry, dict):
        # 早退**不是**优化，是诚实：让后端回同一句拒绝也行，但那样主人要等一个
        # 往返才知道"他压根没申请"。
        # ⚠️ 这句**不是后端说的话**（20260929 核过：后端对"这个 id 找不到申请"说的是
        # `没有找到这条额度申请`）。这里刻意另说一句更准的——"他没申请过"比"没找到
        # 这条申请"对主人有用得多，而分类只认我方的 policy_frame（早退不经网络，
        # 不存在"后端改字认不出"的问题）。**别把这两句写成同一句**：它们来源不同。
        return ToolResult(_target.policy_frame("该账号没有待处理的额度申请"))
    try:
        rid = int(entry.get("id"))
    except (TypeError, ValueError):
        return unavailable("后台额度申请列表里那一行没有编号，无法定位这条申请，本次未改动")

    # ④ 写。驳回理由由**调用方**保证（前端必填、工具参数必填），后端只做回落链
    #    （照 `audit_board` 的既有形态：必填留给调用方，后端不硬闸）。
    payload = {"approved": bool(approved)}
    if note:
        payload["reason"] = note
    data = _admin_quota_post(f"/api/protected/quota/requests/{rid}/review", payload, config)
    if isinstance(data, ToolResult):
        # 政策族（别人刚处理过 / 没有这一条）与目标族原样往外传，不改写一个字。
        return data

    # ⑤ 复核（两个方向各判各的，见头注）。
    if approved:
        return _quota_readback(config, target_id, username, limit, "approve")
    after = _base._quota_pending_index(config)
    if isinstance(after, ToolResult):
        return unavailable(f"驳回请求已发出，但读不回后台额度申请列表（{after}），"
                           f"本次改动未确认生效")
    if target_id in after:
        return unavailable(f"驳回请求已发出，但读回的待处理列表里**还有**账号"
                           f"「{username}」的申请，本次改动未确认生效")
    return ok(_quota.render_quota_status("reject", username, target_id, limit, None),
              meta=fact("quota_reject", changed=True,
                        target=tgt("quota_request", target_id, username),
                        before="待处理", after="已驳回", evidence="已驳回",
                        account_id=target_id, account_name=username))


@tool
def approve_quota_request(
    user_id: Annotated[str | int,
                       "申请人的**账号 id**（从待办台账里原样抄，形如 3 或 账号 id=3）"],
    config: RunnableConfig,
) -> str:
    """**批准**某个用户的对话额度重置申请：他的额度立刻恢复到上限，马上可以继续提问。
    需要管理员身份，且每次都要经主人确认。

    **批准不可撤销**（额度是个数，没有"撤回"这个概念）。申请人的账号 id 必须能在
    后台账号列表里找到；他没有待处理的申请、或那条已经被处理过时，如实把系统给的
    原话转告主人——**不要**换个说法重试，也**不要**改用主动重置去"绕开"这句话
    （那两件事的后果不同，主人同意的是前者）。"""
    return _review_quota_request(user_id, True, None, config)


@tool
def reject_quota_request(
    user_id: Annotated[str | int,
                       "申请人的**账号 id**（从待办台账里原样抄，形如 3 或 账号 id=3）"],
    reason: Annotated[str, "驳回理由（会作为站内通知发给申请人，让他知道为什么；必填）"],
    config: RunnableConfig,
) -> str:
    """**驳回**某个用户的对话额度重置申请：他的额度**不会变化**，但会收到一条站内
    通知（正文含这条理由），而且**可以重新申请**。需要管理员身份，且每次都要经主人
    确认。

    这里给的理由会原样出现在发给申请人的通知里——主人没说理由就问他要一句，**不要
    自己编一个**（申请人收到的是主人的裁决，理由写错比不写更糟）。申请人的账号 id
    必须能在后台账号列表里找到；他没有待处理的申请时如实转告系统原话，不要重试。"""
    return _review_quota_request(user_id, False, reason, config)


def _reset_user_quota(name, config: RunnableConfig) -> ToolResult:
    """**主动**把某个账号的对话额度恢复到上限（不要求他申请过）。

    五段式同 `_set_account_frozen`：① 读名录 → ② 按名字解析唯一一行 → ③ 写 →
    ④ 写后重读**同一份名录**复核计数器 → ⑤ 出口只有 `ok` / `not_found` /
    `policy_frame` / `unavailable`，绝不 `return ""`。

    与 `_review_quota_request` 的差别**只有一处**：那里是"回应他的申请"（先要有一条
    待处理的申请行），这里是"你替他决定"（他申请没申请过都行）。所以这一支**不读**
    待处理申请——读了反而会让人误以为必须有一条申请才能重置。
    """
    from agent import adminops_quota as _quota
    want = str(name or "").strip()
    if not want:
        return unavailable("没给出要重置哪个账号，本次未改动——请让主人说清是哪个账号")

    # ① 写前读：既拿复核基线，也让"查无此名"在发请求之前就响亮地报出来。
    before_index = _base._user_directory(config)
    if isinstance(before_index, ToolResult):
        return _base._pre_read_fail(before_index, "后台账号名录")
    # ② 解析：唯一命中才继续（重名 ⇒ not_found，零写——选错就是清了另一个活人的额度）。
    row, err = _base._find_named_user(want, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or want)
    limit = _quota.quota_limit(row)

    # ③ 写。**幂等不短路**：额度本来就是满的时照样发请求（后端那一支是真 no-op），
    #    结论由下面的复核给——短路成 ok 会让回执把一次"没发生的事"读成一个动作
    #    （同 `_set_account_frozen` 那条"刚冻结的"与"本来就是冻结的"之分）。
    data = _admin_quota_post(f"/api/temp-users/{target_id}/quota-reset", {}, config)
    if isinstance(data, ToolResult):
        return data
    # ④ 写后复核：重读**同一份名录**按 id 找回那一行，判据是计数器读数本身。
    return _quota_readback(config, target_id, username, limit, "reset")


@tool
def reset_user_quota(
    name: Annotated[str, "要重置额度的**账号名**（后台账号列表里看得见的那一行）"],
    config: RunnableConfig,
) -> str:
    """把某个账号的**对话额度恢复到上限**（他还剩的轮数变回满格，马上可以继续提问）。

    **不需要他申请过**——这是管理员主动给的一次重置，与"批准他的申请"是两件事
    （想回应申请就用批准那件）。**恢复满额不可撤销**。管理员账号本来就不限额，对他们
    做这件事不会改变任何东西。需要管理员身份，且每次都要经主人确认。

    **要动的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。

    ⚠️ **为什么这一件仍是名字通道**（20260929 批 H · S2 的选择，不是漏改）：批准与
    驳回治的是"台账里等着办的那一行"，目标现成；而这一件治的是**主人临时想起的某个
    人**——站内**没有**"列出全部账号"的读工具（`list_quota_requests` 只有申请队列），
    所以改成只认 id 就**静默丢掉**「给某个没申请过的人重置额度」这条今天真能办的事
    （`_find_named_user` 按名字查名录正是为它服务的）。名字→uid 的解析在工具侧对着
    实时名录做，唯一命中才动手，与冻结族逐字同规。"""
    return _reset_user_quota(name, config)
