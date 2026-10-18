# -*- coding: utf-8 -*-
"""用户**自己那一份**数据：收藏 / 未读汇总 / 站内通知 / 站内信（20261018 拆 `tools/base.py`）。

**为什么单独成模块**：`tools/base.py` 是全仓第二大体量文件（5351 行 / 141 个模块级定义），
本模块收的是其中**相邻的两节**——「用户自己的数据工具」（20260923）与「站内信（私信）」
（20260923 批 8）。它们本来就是一件事的两面：scope 同属 `read.own` / `write.own`
（见 `agent/authz.py` 的登记），读写的是**登录者自己那一份**，与公开池（`list_notes` /
`search_notes` 那一族，`read.public`）在语义上就是两套视图。合并成一个模块而不是切两刀，
理由是它们共用同一批原语（`_as_ids` / `_as_bool` / `_as_article_id` / `_own_get` /
`_own_write_guard` / `_pre_read_fail`）——切开只会让这批原语在两边各留一份。

**搬家方式**（与 `tools/device.py` 20261017 那一刀逐字同源）：`tools/base.py` 在自己
那一节原来的位置按**原名** re-export 本模块全部顶层名（`# noqa: E402,F401`）。为什么不
逐个改 import 方——本仓有套件**直接给 `tools.base` 的模块属性打桩**（实测：`_client` 159
处、`_get` 10 处、`_admin_get` 4 处、`_find_named_user` 3 处），而"补丁打在哪个模块对象上"
决定它生不生效：桩写在 `tools.base` 上，就得有人**从 `tools.base` 的名字空间里**去读它。

⚠️ **本模块取 `tools.base` 的东西一律走模块级 `from tools.base import …`（取函数对象），
今天这么写是安全的——本模块一个都不用那三个被套件打桩的名字（`_get` / `_admin_get` /
`_find_named_user`）。将来谁要在本模块里调它们，必须写成 `from tools import base as _base`
再 `_base._admin_get(...)`**：按名 import 会拿到**打桩前**的旧函数对象，桩静默失效、
测试真去发网络请求（同 `tools/result.py` 头注那条：看着在跑、其实没跑）。

⚠️ **别丢注释**：两节的节头注（`read.own` 为什么不能取 `read.public`、站内信为什么**不是**
河灯留言）连同正文一起搬过来了——那些话解释的是"为什么是这个 scope"，删掉它下一个人就会
"顺手统一"成公开面。折行说明见下。

⚠️ **一处折行**：`read_notifications` 里那句 `meta=fact("notice_read", …)` 在
`tools/base.py` 里是**126 列的历史长行**（那边的 E501 是存量豁免）。本文件按
`docs/lint-baseline.md` 纪律 1「新增文件零 ruff error」折了行 ⇒ 该函数**行数 111→113**，
`eval/complexity_baseline.json` 里那一条随之重登记为 113（**`c` 仍是 35，一个字没动**；
字面与逻辑均逐字不变，多出来的只是排版）。
"""

from typing import Annotated

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools.base import (
    _as_article_id,
    _own_delete,
    _own_get,
    _own_post,
    _own_write_guard,
    _pre_read_fail,
)
from tools.result import ToolResult, _shape, fact, not_found, ok, tgt, unavailable

# ---------------------------------------------------------------------------
# 用户自己的数据工具（20260923：收藏 / 未读汇总 / 站内通知）
# ---------------------------------------------------------------------------
# 这一节是"用户侧感知能力"的**读**那一半（写那一半是同一批里的 add_favorite /
# remove_favorite / read_notifications，scope 为 `write.own`）：
# 访客问"我收藏了哪些文章""有没有未读的公告"，planner 点名这三个工具，execute
# 以**本轮发起人身份**读他自己的数据后如实作答。
#
# 三条纪律：
#   ① scope 全声明为 `read.own`（agent/authz.py）——三档角色都有，匿名没有。
#      **不是** admin.console：收藏/通知是"我的"，与管理员身份无关，判据是
#      "以谁的 uid 去读"，由工具层落地。
#   ② 通道是 `_own_get`（与 `_admin_get` 同一个 `_principal_get` 本体，只有措辞
#      不同）：fail-closed 同族，"读不到"绝不返回空——否则 narrator 会对着一次
#      读失败说"你还没有收藏任何文章"。
#   ③ 返回值走 `_shape(data)`（结构化），**不做中文报表**——它们是"用户要的值"
#      （标题、日期、已读态），narrator 手上还有完整 ToolMessage 可转述；报表那套
#      渲染（agent/reports.py）是给"数字必须可信、模型数不了"的管理报表用的。
#      planner 那一侧由 `_compact_list_frame` 压成一行一条（见 agent/context.py）。
#
# 端点（Rust 侧，src/routes/profile.rs）：`/favorites`、`/notifications`、
# `/notifications/summary` 都只认 auth_uid，**自己读自己**——没有"读别人的"接口。

@tool
def list_my_favorites(config: RunnableConfig) -> str:
    """列出**当前登录用户自己**收藏的文章（返回 noteId / title / status / createdAt）。
    访客问"我收藏了哪些文章""我的收藏夹里有什么"时用。没携带身份时如实告知读不到。
    注意：这条读的是**用户自己的**收藏夹，不是全站文章列表（那是 list_notes）。"""
    data = _own_get("/api/protected/favorites", config)
    return _shape(data)


# 未读汇总连带带回的条目（20260924）：红点问的是"几条"，紧接着的一句必定是
# "是什么"——只有计数时那一句要么再调一次 list_notifications（多一轮）、要么
# 拿计数去编内容。上限 10 条：条目是给"红点里是什么"看的，不是列表副本
# （要全量走 list_notifications）；超限时在 `unread_items_note` 里如实说明。
# 20260924 二改：`content` **进**条目。首版刻意不搬它（当时的口径是"条目只给指代用"），
# 实测错了——留言审核的**驳回理由就写在 content 里**，不搬等于逼着多问一轮：
# trace 20260924T025619 只拿到 title，访客紧接着问"那两条留言的审核意见是什么"，
# 正是这一句逼出后面两轮（其中一轮还把通知里的留言 id 当文章 id 去查 get_article_detail）。
# 截断到 120 字：条目是摘要，要原文走 list_notifications。
_UNREAD_ITEMS_MAX = 10
_UNREAD_ITEM_KEYS = ("id", "type", "title", "content", "link", "createdAt")
_UNREAD_CONTENT_MAX = 120
# 条目那半读不到时的措辞（计数是真的、条目没有）：**不许**退化成"没有未读条目"
_UNREAD_ITEMS_DOWN = "未读条目的内容这次没读到（只有计数是可信的）"


def _unread_item_slim(row: dict) -> dict:
    """一条未读通知 → 给 planner/narrator 看的条目（`content` 截断，别把整条通知搬进帧）。"""
    out = {k: row[k] for k in _UNREAD_ITEM_KEYS if k in row}
    text = out.get("content")
    if isinstance(text, str) and len(text) > _UNREAD_CONTENT_MAX:
        out["content"] = text[:_UNREAD_CONTENT_MAX] + "…"
    return out


@tool
def get_unread_summary(config: RunnableConfig) -> str:
    """查看**当前登录用户自己**的未读汇总：计数（notifications / messages / total）
    **连带未读的那几条通知**（`unread_items`：id / type / title / content（正文，截断
    120 字）/ link / createdAt，最多 10 条，超出时另有 `unread_items_note` 说明）。
    红点数 = 站内通知未读 + 私信未读；**公告在发布时按用户展开成通知行**，所以
    公告的未读也计在 notifications 里，不需要另一套计数。
    访客问"我有未读吗""红点上有几条""有多少没看的消息""红点里是什么/是什么通知"时用
    ——问"是什么"**不必**再调 list_notifications（未读条目连同正文就在这次返回里）。
    **通知正文里往往就是答案本体**（如留言审核的驳回理由写在 content 里）：访客追问
    "那是什么内容/理由是什么"时先看这里，别绕去查别的接口，更不许说"读不到内容"。
    没携带身份时如实告知读不到。"""
    data = _own_get("/api/protected/notifications/summary", config)
    if isinstance(data, ToolResult):
        # 计数都没读到：原样透出（fail-closed），**不做任何"连带"**——
        # 计数读不到时再补一次列表调用，只会让"没携带身份"变成一次多余的请求
        return data
    if not isinstance(data, dict):
        return _shape(data)
    notices = _own_get("/api/protected/notifications", config)
    rows: list = []
    if isinstance(notices, dict):
        if isinstance(notices.get("items"), list):
            rows = notices["items"]
    elif isinstance(notices, list):
        rows = notices
    if isinstance(notices, ToolResult) or not rows:
        # 两种情况分开：条目读失败（notices 是 unavailable）与真的零条目。
        # 读失败绝不能写成"你没有未读的通知"——本模块头注第 2 条纪律。
        note = (_UNREAD_ITEMS_DOWN if isinstance(notices, ToolResult)
                else "")
        out = dict(data, unread_items=[])
        if note:
            out["unread_items_note"] = note
        return _shape(out)
    unread = [r for r in rows
              if isinstance(r, dict) and not r.get("isRead")]
    slim = [_unread_item_slim(r) for r in unread[:_UNREAD_ITEMS_MAX]]
    out = dict(data, unread_items=slim)
    # 抬头那个计数（notifications）才是权威数量：拿它跟带回来的条数比，**两种缺口
    # 用同一句话如实说**——① 超过本条上限；② 未读里较早的落在列表接口的最近 100 条
    # 窗口之外（那时"未读里还有几条"照样成立）。不许默默少给。
    total_unread = data.get("notifications")
    if (isinstance(total_unread, int) and not isinstance(total_unread, bool)
            and total_unread > len(slim)):
        out["unread_items_note"] = (f"只带回 {len(slim)} 条"
                                    f"（未读通知共 {total_unread} 条）")
    return _shape(out)


@tool
def list_notifications(config: RunnableConfig) -> str:
    """列出**当前登录用户自己**的站内通知（返回 unread 与 items：id / type / title /
    content / link / isRead / createdAt，按时间倒序，最多 100 条）。
    type=announcement 的是站内公告（发布时按用户展开），其余是系统通知（如留言审核
    结果）。访客问"有什么未读的公告/通知""最新的一条通知说了什么"时用。
    没携带身份时如实告知读不到；**这条不含私信**（私信未读只在 get_unread_summary 的
    messages 里计数，本站没有"读自己的私信列表"的 agent 工具）。"""
    data = _own_get("/api/protected/notifications", config)
    return _shape(data)


# ── 写那一半（20260923 批 7）：收藏 / 取消收藏 / 标记已读 ────────────────────
# scope = `write.own`（三档角色都有、匿名没有）。三条纪律，第三条是写操作独有的：
#   ① 同意闸判"本轮有没有一条**明确命令**"，判据按**工具名**分开（agent/authz.py
#      的 `_own_command`：一个 scope 挂多个工具，只看"有没有写动作"会让填错工具
#      的那一轮照样放行）。判不出来走确认弹窗，既不硬拒也不静默写。
#   ② 通道是 `_own_request`（与后台写共用 `_principal_request` 本体）：uid ≤ 0 时
#      **一个字节都不发**——写操作最不该做的就是在没身份时猜"写给谁"。
#   ③ **写前先读、写后再读**：写前读不到就不写（连"是不是已经收藏了"都判不出来时
#      写下去等于蒙）；写后读不回目标状态就报"未确认生效"，**绝不拿接口的成功文案
#      当事实**——Rust 的 `ApiResponse` 成功文案是给人看的，不是给 agent 当判据的。
#
# 幂等（写前读的副产品）：已经收藏 / 本来就没收藏 / 这些通知本来就是已读
# → **不发请求**、如实说"本来就是这样"。收藏端点自己也是幂等的，但一次空写会刷新
# 那一行的时间戳（纯副作用、无收益），与 set_article_status 的同值 noop 同一条理由。


def _favorites_snapshot(config: RunnableConfig,
                        what: str) -> tuple[list | None, ToolResult | None]:
    """本人收藏列表 → `(行列表, 失败原因)`，两者**恰有一个**为 None。

    **弹窗的「状态已达成」判据与 add/remove_favorite 的写前读走这一个函数**（照
    `_tag_index` / `_note_index` / `_todo_rows` 那条先例）：两处各写一套形态判断时，
    "弹窗说本来就在收藏夹里、工具却照写一遍"这种不一致没有任何东西拦得住——而它正是
    20260926 那一批要治的病。

    `what` = 失败话里的那半句（收藏与取消收藏问的不是同一件事："是不是已经收藏过"
    /"是不是本来就没收藏"），由调用点带进来，措辞不在这里分叉。
    """
    got = _own_get("/api/protected/favorites", config)
    if isinstance(got, ToolResult):
        return None, _pre_read_fail(got, what)
    if not isinstance(got, list):
        return None, unavailable(f"读不到{what}，本次未改动")
    return got, None


def _fav_row(data, note_id: int) -> dict | None:
    """收藏列表 → 目标那一行（不是列表/没这一行 → None，不猜）。"""
    if not isinstance(data, list):
        return None
    for r in data:
        if isinstance(r, dict) and _as_article_id(r.get("noteId")) == note_id:
            return r
    return None


def _fav_count(data) -> str:
    """收藏条数的括注；读不出条数就**什么都不写**（不写 0）。"""
    n = len(data) if isinstance(data, list) else None
    return f"（你的收藏夹现在有 {n} 篇）" if n is not None else ""


def _fav_title(row: dict) -> str:
    t = str(row.get("title") or "").strip()
    return f"《{t}》" if t else ""


def _note_items(data) -> dict[int, dict] | None:
    """通知列表（`{unread, items}`）→ `{id: 行}`；形态不对 → None（不猜）。"""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    out: dict[int, dict] = {}
    for r in data["items"]:
        if not isinstance(r, dict):
            continue
        nid = _as_article_id(r.get("id"))
        if nid is not None:
            out[nid] = r
    return out


def _notifications_snapshot(config: RunnableConfig,
                            what: str) -> tuple[dict[int, dict] | None, ToolResult | None]:
    """通知列表 → `({id: 行}, 失败原因)`，两者**恰有一个**为 None。

    与 `_favorites_snapshot` 同一条纪律（弹窗判据与 read_notifications 的写前读
    必须是**同一份**读、同一份形态判断）。`what` 同上：由调用点给出失败话的那半句。
    """
    got = _own_get("/api/protected/notifications", config)
    if isinstance(got, ToolResult):
        return None, _pre_read_fail(got, what)
    rows = _note_items(got)
    if rows is None:
        return None, unavailable(f"读不到{what}，本次未改动")
    return rows, None


def _as_ids(value) -> list[int]:
    """实参 → 正整数 id 列表（去重、保序；认不出的项丢掉——不猜）。"""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[int] = []
    for it in items:
        i = _as_article_id(it)
        if i is not None and i not in out:
            out.append(i)
    return out


def _as_bool(value) -> bool | None:
    """实参 → 布尔；认不出 → None（调用方按"没说"处理，**绝不默认 True**）。"""
    if isinstance(value, bool):
        return value
    s = str(value if value is not None else "").strip().lower()
    if s in ("true", "1", "yes", "y", "on", "all", "全部", "全都", "所有"):
        return True
    if s in ("false", "0", "no", "n", "off", ""):
        return False
    return None


@tool
def add_favorite(
    article_id: Annotated[int, "文章 id：用户本轮点名了（如「收藏文章 12」）就直接用点名的那个；"
                               "只说特征（「收藏那篇讲架构的」）或指代（「收藏这篇」）时"
                               "必须先用检索/列表工具或 get_article_detail 拿到确切 id，"
                               "不许凭记忆猜"],
    config: RunnableConfig,
) -> str:
    """把一篇文章收藏进**当前登录用户自己**的收藏夹（幂等：已收藏过就如实说本来就有，
    不会重复收藏）。收藏只有他自己看得见，**不改变文章的公开状态**。
    没携带身份时如实告知，一个请求都不发。"""
    aid = _as_article_id(article_id)
    if aid is None:
        return unavailable(f"文章 id「{article_id}」不合法，未改动")
    guard = _own_write_guard(config)
    if guard is not None:
        return guard

    before, fail = _favorites_snapshot(config, "你的收藏列表（拿不准是不是已经收藏过）")
    if fail is not None:
        return fail
    hit = _fav_row(before, aid)
    if hit is not None:
        return ok(f"文章 {aid}{_fav_title(hit)}本来就在你的收藏夹里，无需改动（没有发出写请求）。",
                  meta=fact("favorite_add", changed=False, target=tgt("note", aid),
                            before="已收藏", after="已收藏", change="本来已收藏",
                            article_id=aid))

    data = _own_post("/api/protected/favorites", {"noteId": aid}, config)
    if isinstance(data, ToolResult):
        return data

    after = _own_get("/api/protected/favorites", config)
    if isinstance(after, ToolResult):
        # ⚠️ 写**已经发出去了**，此后读不回来不能再把原样的话交出去：读侧的
        # unavailable 措辞只说"读不到"（那是写前读该说的话），而下游 narrator 需要
        # 知道"写请求已发出、但没确认生效"。两种情形混用会让它把一次读失败讲成
        # 收藏失败，或者更糟——照接口回的话说"已收藏"。
        return unavailable(f"收藏请求已发出，但读不回你的收藏列表（{after}），"
                           f"本次改动未确认生效（不要声称已收藏）")
    got = _fav_row(after, aid)
    if got is None:
        return unavailable(f"收藏请求已发出，但读回收藏列表里没有文章 {aid}，"
                           f"本次改动未确认生效（不要声称已收藏）")
    return ok(f"已收藏文章 {aid}{_fav_title(got)}{_fav_count(after)}。",
              meta=fact("favorite_add", changed=True, target=tgt("note", aid),
                        before="未收藏", after="已收藏", change="已收藏", article_id=aid))


@tool
def remove_favorite(
    article_id: Annotated[int, "文章 id：同 add_favorite（点名了就直接用，"
                               "只说特征/指代时先用检索或收藏列表拿到确切 id）"],
    config: RunnableConfig,
) -> str:
    """把一篇文章从**当前登录用户自己**的收藏夹里去掉（本来就没收藏过就如实说，
    不当成出错——同一个按钮点两次不该报错）。只动他自己的收藏夹，不改文章本身。
    没携带身份时如实告知，一个请求都不发。"""
    aid = _as_article_id(article_id)
    if aid is None:
        return unavailable(f"文章 id「{article_id}」不合法，未改动")
    guard = _own_write_guard(config)
    if guard is not None:
        return guard

    before, fail = _favorites_snapshot(config, "你的收藏列表（拿不准是不是本来就没收藏）")
    if fail is not None:
        return fail
    hit = _fav_row(before, aid)
    if hit is None:
        return ok(f"文章 {aid} 本来就不在你的收藏夹里，无需改动（没有发出写请求）。",
                  meta=fact("favorite_remove", changed=False, target=tgt("note", aid),
                            before="未收藏", after="未收藏", change="本来就没收藏",
                            article_id=aid))

    data = _own_delete(f"/api/protected/favorites/{aid}", config)
    if isinstance(data, ToolResult):
        return data

    after = _own_get("/api/protected/favorites", config)
    if isinstance(after, ToolResult):
        return unavailable(f"取消收藏请求已发出，但读不回你的收藏列表（{after}），"
                           f"本次改动未确认生效（不要声称已取消）")
    if _fav_row(after, aid) is not None:
        return unavailable(f"取消收藏请求已发出，但读回收藏列表里文章 {aid} 还在，"
                           f"本次改动未确认生效（不要声称已取消）")
    return ok(f"已取消收藏文章 {aid}{_fav_title(hit)}{_fav_count(after)}。",
              meta=fact("favorite_remove", changed=True, target=tgt("note", aid),
                        before="已收藏", after="未收藏", change="已取消收藏", article_id=aid))


@tool
def read_notifications(
    config: RunnableConfig,
    ids: Annotated[list[int] | None, "要标记已读的通知 id 列表（用户点名了具体哪几条时给；"
                                     "id 只能来自 list_notifications 的返回或执行记忆摘要里 "
                                     "`id《标题》` 形态的编号，**不许把「共 N 条」里的 N 当 id**，"
                                     "也不许自己编——编号必须是真的才能在列表里对上"] = None,
    all: Annotated[bool | None,
                   "true = 把**全部**未读通知标记已读（用户说了「全部/都/所有」才给；"
                   "只是「把通知标记为已读」这种没限定范围的**不要自己填 True**）"] = None,
) -> str:
    """把**当前登录用户自己**的站内通知标记为已读（按 id 或全部）。**已读不可撤销**：
    标记之后那几条就不再是未读（头顶红点问的是"未读总数 > 0"，而那个总数是**通知与
    站内信之和**：只标一部分时它不变，把通知全标完也还要信那边也没有未读才会消失），
    所以只有用户明确说了要标记才用。
    既要不了 id 也没说"全部"时**什么都不动**，如实问清是哪几条。
    没携带身份时如实告知，一个请求都不发。

    ⚠️ 形参名 `all` 是对外（planner / 技能参数）的 JSON 键，**本函数体内不得再调用
    内置 `all()`** —— 它已被同名形参遮蔽（20260923 实测：`all(...)` 直接
    `TypeError: 'NoneType' object is not callable`）。要判"全都没读"用显式循环。
    """
    want_all = _as_bool(all) is True
    want_ids = _as_ids(ids)
    guard = _own_write_guard(config)
    if guard is not None:
        return guard
    if not want_all and not want_ids:
        return unavailable("没有指出要标记哪些通知（是全部未读、还是某几条的 id），未改动")

    rows, fail = _notifications_snapshot(config, "通知列表（无法确认哪几条是未读）")
    if fail is not None:
        return fail

    if want_all:
        targets = [i for i, r in rows.items() if not r.get("isRead")]
    else:
        targets = want_ids
        # ⚠️ 这里刻意**不是**"按 id 原样交给服务端、由它在全量里改"（那是本工具
        # 20260923 批 7 最早的写法）：列表只回最近 100 条，交出去确实能覆盖到更早的
        # 那些，但代价是**编出来的 id 也会被照样发出去**——服务端按它匹配 0 行，
        # 写后复核报"未确认生效"，过程行还说成「服务不可用」。trace 实证
        # （`20260923T130033_9`）：planner 把摘要里的条数「3」当 id 填了进来（真实那条
        # 是 23），用户看到的就是"标记已读未成功（服务不可用）"——一次注定 0 行的写
        # 换来一句误导。现在改成写**之前**先确认这些 id 至少是**我读得到的**：
        # 一个都不在就零写 + not_found 如实说清。上限 100 条这个边界写进话里
        # （**不谎称"站内没有这条"**——它可能只是比最近 100 条更早）。
        unknown = [i for i in targets if i not in rows]
        if len(unknown) == len(targets):
            return not_found(
                f"你点名的通知 {'、'.join(str(i) for i in unknown)} 不在我读到的通知列表"
                f"（最近 {len(rows)} 条，最多 100 条）里，无法确认是哪几条——"
                f"本次一个字节都没改（没有发出写请求）。"
                f"要标记哪一条请先读一遍通知列表拿它的 id（列表里每条都带着 id）。")
        if unknown:
            return not_found(
                f"你点名的通知里 {'、'.join(str(i) for i in unknown)} 不在我读到的通知列表"
                f"（最近 {len(rows)} 条，最多 100 条）里，无法确认是哪几条——"
                f"本次一个字节都没改（没有发出写请求）。"
                f"请把 id 核对一遍（或说「全部标记已读」由系统按未读的那几条来标）。")
        # 已经全是已读 → 不发请求（写前读的副产品，见本节头注的幂等段）。
        # 显式循环而非 all(...)：见本函数 docstring 的形参遮蔽警告。
        all_read = True
        for i in targets:
            if rows.get(i, {}).get("isRead") is not True:
                all_read = False
                break
        if all_read:
            return ok(f"通知 {('、'.join(str(i) for i in targets))} 本来就是已读，"
                      f"无需改动（没有发出写请求）。",
                      meta=fact("notice_read", changed=False,
                                target=tgt("notice",
                                           targets[0] if len(targets) == 1 else None),
                                before="已读", after="已读", change="本来就读过"))
    if want_all and not targets:
        return ok("你的通知本来就没有未读的，无需改动（没有发出写请求）。",
                  # `change` 与这句正文同源、要能**独立成句**：Rust 那边把它接在物件名
                  # 后面渲染成「站内通知本来就没有未读的（未改动）」（`render_exec_row`
                  # 的四臂），少一个「有…的」就变成「站内通知本来就没未读」这种半截话，
                  # 而这一行会经 recent_executions 注回下一轮上下文。
                  # target 不带 id：这一支治的是"整个列表本来就没有未读"，不是某一条。
                  meta=fact("notice_read", changed=False,
                            before="无未读", after="无未读", change="本来就没有未读的"))

    before_sum = _own_get("/api/protected/notifications/summary", config)
    if isinstance(before_sum, ToolResult):
        return _pre_read_fail(before_sum, "未读通知的条数（无法确认改动是否生效）")
    n_before = (before_sum or {}).get("notifications")
    if not isinstance(n_before, int) or isinstance(n_before, bool):
        return unavailable("读不回未读通知的条数，无法确认改动是否生效，本次未改动")

    payload = {"ids": [], "all": True} if want_all else {"ids": sorted(targets), "all": False}
    data = _own_post("/api/protected/notifications/read", payload, config)
    if isinstance(data, ToolResult):
        return data

    # 复核只认**服务端重新数出来的**未读数（写入响应里那个数是更新后当场查的，
    # 但"接口说成功了"仍然不是判据——读侧的判据必须是一次独立读数）。
    after_sum = _own_get("/api/protected/notifications/summary", config)
    if isinstance(after_sum, ToolResult):
        return unavailable(f"标记已读请求已发出，但读不回未读条数（{after_sum}），"
                           f"本次改动未确认生效（不要声称已标记）")
    n_after = (after_sum or {}).get("notifications")
    if not isinstance(n_after, int) or isinstance(n_after, bool):
        return unavailable("标记已读请求已发出，但读不回未读条数，本次改动未确认生效")
    if n_after >= n_before:
        return unavailable(f"标记已读请求已发出，但读回未读通知仍是 {n_after} 条"
                           f"（可能这几条本来就是已读）——本次改动未确认生效，不要声称已标记")
    marked = n_before - n_after
    m_after = (after_sum or {}).get("messages")
    tail = (f" / 私信 {m_after}"
            if isinstance(m_after, int) and not isinstance(m_after, bool) else "")
    return ok(f"已把 {marked} 条通知标记为已读（现在未读：通知 {n_after} 条{tail}）。",
              meta=fact("notice_read", changed=True,
                        before=f"未读 {n_before} 条", after=f"未读 {n_after} 条",
                        evidence=f"未读 {n_after} 条", change=f"标记已读 {marked} 条"))


# ---------------------------------------------------------------------------
# 站内信（私信）（20260923 批 8）：读信箱 / 标记已读（发信见本节的最后一小节）
# ---------------------------------------------------------------------------
# **术语必须分开**（用户点名要求）：站内信（私信）≠ 河灯留言。
#   · 河灯留言 = 访客在**公开页面**写下的内容（灯影集/说说），谁都看得见
#     ——那是 `list_guestbook` / `list_talks`。
#   · 站内信 = 一封**一对一**的信，只有收发双方看得见——就是这一节。
# 两者混用会让 narrator 说出"我看了你留言板上收到的私信"这种不存在的东西，
# 所以每个工具的 docstring 与技能描述里都把这句话写死一遍。
#
# 端点（Rust src/routes/profile.rs）：`GET /api/protected/messages`（收件箱 +
# 发件箱，各最近 100 条）、`POST /api/protected/messages/read`（只动收件箱里我的
# 信）、`POST /api/protected/messages`（发一条，收件人**按账号或 UID** 精确匹配）。
# 收发双方都只认 auth_uid——**没有"读别人的信箱"的接口**。
#
# 读那一半（scope = read.own）与收藏/通知同族：`_own_get` fail-closed，读不到
# 绝不返回空（"你信箱里没有信"是结论，"没读到"不是）。


def _mailbox_inbox(data) -> dict[int, dict] | None:
    """信箱返回（`{inbox, outbox, unread}`）→ `{id: 收件箱那一行}`；形态不对 → None。

    只收**收件箱**：这个函数的调用点（写前读、写后复核、弹窗判据）都只关心
    "我收到的信"，发件箱里的信被别人读没读是别人的事（`isRead` 只对收件人有意义）。
    """
    if not isinstance(data, dict) or not isinstance(data.get("inbox"), list):
        return None
    out: dict[int, dict] = {}
    for r in data["inbox"]:
        if not isinstance(r, dict):
            continue
        mid = _as_article_id(r.get("id"))
        if mid is not None:
            out[mid] = r
    return out


def _mailbox_snapshot(config: RunnableConfig,
                      what: str) -> tuple[dict | None, ToolResult | None]:
    """本人信箱 → `(信箱原始返回, 失败原因)`，两者**恰有一个**为 None。

    这里回的是**原始返回**而不是直接给收件箱那半：同一个 payload 上还有第二个
    事实（`unread` 未读封数）是 read_messages 的写前基线，只给 `inbox` 会逼它再读一次
    （或者更糟——另算一遍未读数，那就成了同一件事的第二份判据）。调用方各自用
    `_mailbox_inbox` / `_mailbox_unread` 取自己那半。

    与 `_favorites_snapshot` 同一条纪律：弹窗的「已达成」判据与工具的写前读**同一份**。
    """
    got = _own_get("/api/protected/messages", config)
    if isinstance(got, ToolResult):
        return None, _pre_read_fail(got, what)
    if _mailbox_inbox(got) is None:
        return None, unavailable(f"读不到{what}，本次未改动")
    return got, None


def _mailbox_unread(data) -> int | None:
    """信箱返回里的未读封数（Rust `MailboxDto.unread`）；读不出 → None（不写 0）。"""
    n = (data or {}).get("unread") if isinstance(data, dict) else None
    return n if isinstance(n, int) and not isinstance(n, bool) else None


@tool
def list_my_messages(config: RunnableConfig) -> str:
    """列出**当前登录用户自己**的站内信（私信）：收件箱与发件箱各最近 100 条，
    外加未读封数（返回 inbox / outbox / unread，每封带 id / fromUserId / toUserId /
    peerName / title / content / isRead / createdAt）。
    访客问"我的信箱里有什么""谁给我写过信""我发出去的信"时用。
    ⚠️ 站内信与**河灯留言**是两回事：留言在公开页面上、谁都看得见（那是
    list_guestbook）；站内信是一对一写的信，只有收发双方看得见。
    没携带身份时如实告知读不到。"""
    data = _own_get("/api/protected/messages", config)
    return _shape(data)


@tool
def read_messages(
    config: RunnableConfig,
    ids: Annotated[list[int] | None, "要标记已读的那几封**收到的信**的 id（用户点名了具体"
                                     "哪几封时给；id 只能来自 list_my_messages 的返回或执行"
                                     "记忆摘要里 `id《标题》` 形态的编号，**不许把「共 N 封」"
                                     "里的 N 当 id**，也不许自己编"] = None,
    all: Annotated[bool | None,
                   "true = 把**全部**未读的收信标记已读（用户说了「全部/都/所有」才给；"
                   "只是「把信读了」这种没限定范围的**不要自己填 True**）"] = None,
) -> str:
    """把**当前登录用户自己收到的**站内信标记为已读（按 id 或全部）。**已读不可撤销**：
    标记之后那几封就不再是未读（头顶红点问的是"未读总数 > 0"，而那个总数是**通知与
    站内信之和**：只标一部分时它不变，把信全标完也还要通知那边也没有未读才会消失），
    所以只有用户明确说了要标记才用。
    既能要不到 id 也没说"全部"时**什么都不动**，如实问清是哪几封。
    只动**收到的**信——发出去的信别人读没读改不了。
    没携带身份时如实告知，一个请求都不发。

    ⚠️ 与 read_notifications 同一个坑：形参名 `all` 遮蔽内置 `all()`，本函数体内
    不得再调用内置 `all(...)`（要判"全都没读"用显式循环）。
    """
    want_all = _as_bool(all) is True
    want_ids = _as_ids(ids)
    guard = _own_write_guard(config)
    if guard is not None:
        return guard
    if not want_all and not want_ids:
        return unavailable("没有指出要标记哪几封信（是全部未读、还是某几封的 id），未改动")

    before, fail = _mailbox_snapshot(config, "你的信箱（无法确认哪几封是未读）")
    if fail is not None:
        return fail
    rows = _mailbox_inbox(before)

    if want_all:
        targets = [i for i, r in rows.items() if r.get("isRead") is not True]
    else:
        targets = want_ids
        # 与 read_notifications 同一条纪律（20260923 三轮的那次误导）：**写之前**先确认
        # 这些 id 至少是"我读得到的"——一个都不在就零写 + not_found 如实说清，绝不让
        # 一个编出来的 id 变成一次注定 0 行的写 + 一句「服务不可用」的误报。
        unknown = [i for i in targets if i not in rows]
        if len(unknown) == len(targets):
            return not_found(
                f"你点名的信 {'、'.join(str(i) for i in unknown)} 不在我读到的收件箱"
                f"（最近 {len(rows)} 封，最多 100 封）里，无法确认是哪几封——"
                f"本次一个字节都没改（没有发出写请求）。"
                f"要标记哪一封请先读一遍信箱拿它的 id（每封都带着 id）。")
        if unknown:
            return not_found(
                f"你点名的信里 {'、'.join(str(i) for i in unknown)} 不在我读到的收件箱"
                f"（最近 {len(rows)} 封，最多 100 封）里，无法确认是哪几封——"
                f"本次一个字节都没改（没有发出写请求）。"
                f"请把 id 核对一遍（或说「全部标记已读」由系统按未读的那几封来标）。")
        all_read = True
        for i in targets:
            if rows.get(i, {}).get("isRead") is not True:
                all_read = False
                break
        if all_read:
            return ok(f"信 {('、'.join(str(i) for i in targets))} 本来就是已读，"
                      f"无需改动（没有发出写请求）。",
                      meta=fact("message_read", changed=False,
                                target=tgt("message",
                                           targets[0] if len(targets) == 1 else None),
                                before="已读", after="已读", change="本来就读过"))
    if want_all and not targets:
        return ok("你的收件箱本来就没有未读的信，无需改动（没有发出写请求）。",
                  # `change` 要能独立成句，理由同上面通知那半（少一个「有…的」就成了
                  # 半截话，而它会被拼在物件名后面落进跨轮执行记忆）。
                  meta=fact("message_read", changed=False,
                            before="无未读", after="无未读", change="本来就没有未读的"))

    n_before = _mailbox_unread(before)
    if n_before is None:
        return unavailable("读不回未读的封数，无法确认改动是否生效，本次未改动")

    payload = {"ids": [], "all": True} if want_all else {"ids": sorted(targets), "all": False}
    data = _own_post("/api/protected/messages/read", payload, config)
    if isinstance(data, ToolResult):
        return data

    # 写后复核 = **一次独立读数**（接口回的那份不算判据）：未读封数必须真降，
    # 且点名的每一封都确实是已读了——只看"降了"会在"标错了几封、又漏了几封"
    # 的巧合下判成功（净变化相同）。
    after = _own_get("/api/protected/messages", config)
    if isinstance(after, ToolResult):
        return unavailable(f"标记已读请求已发出，但读不回你的信箱（{after}），"
                           f"本次改动未确认生效（不要声称已标记）")
    n_after = _mailbox_unread(after)
    if n_after is None:
        return unavailable("标记已读请求已发出，但读不回未读封数，本次改动未确认生效")
    if n_after >= n_before:
        return unavailable(f"标记已读请求已发出，但读回未读的信仍是 {n_after} 封"
                           f"（可能这几封本来就是已读）——本次改动未确认生效，不要声称已标记")
    arows = _mailbox_inbox(after) or {}
    still = [i for i in targets if arows.get(i, {}).get("isRead") is not True]
    if still:
        return unavailable(f"标记已读请求已发出，但读回信 {'、'.join(str(i) for i in still)} "
                           f"还不是已读——本次改动未确认生效，不要声称已标记")
    return ok(f"已把 {n_before - n_after} 封信标记为已读（收件箱现在未读 {n_after} 封）。",
              meta=fact("message_read", changed=True,
                        before=f"未读 {n_before} 封", after=f"未读 {n_after} 封",
                        evidence=f"未读 {n_after} 封",
                        change=f"标记已读 {n_before - n_after} 封"))
