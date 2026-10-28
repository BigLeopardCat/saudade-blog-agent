"""Legacy account directory adapter; native mutations use versioned domain operations."""


from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, empty, ok, unavailable


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
    # ⚠️ 传进来的**必须是 config**。本函数的调用方里有一族
    #    `_find_*(name, config, index=…)` 的形状——"读好的快照"是**第三个**参数。一旦
    #    有人把快照当 config 传进来，下面读出的是 uid=0，函数会回一句「无法获取当前
    #    用户身份」，而**身份其实好着**：那是一句假话，调用方还会照着它零写收场，谁也
    #    看不出哪里错了。生产实证：额度批准/驳回整条通道自 4cb1f93起一次都
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

__all__ = ['list_accounts']
