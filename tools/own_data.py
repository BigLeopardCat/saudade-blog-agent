"""Current-user reads and confirmed favorite persistence adapters."""

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
from tools.result import ToolResult, _shape, fact, ok, tgt, unavailable


@tool
def list_my_favorites(config: RunnableConfig) -> str:
    """列出**当前登录用户自己**收藏的文章（返回 noteId / title / status / createdAt）。
    访客问"我收藏了哪些文章""我的收藏夹里有什么"时用。没携带身份时如实告知读不到。
    注意：这条读的是**用户自己的**收藏夹，不是全站文章列表（那是 list_notes）。"""
    data = _own_get("/api/protected/favorites", config)
    return _shape(data)

_UNREAD_ITEMS_MAX = 10

_UNREAD_ITEM_KEYS = ("id", "type", "title", "content", "link", "createdAt")

_UNREAD_CONTENT_MAX = 120

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

def _favorites_snapshot(config: RunnableConfig,
                        what: str) -> tuple[list | None, ToolResult | None]:
    """Read current-user favorites as either rows or a failure reason.

    Exactly one result is None. An empty row list is an observed empty collection,
    while a failure prevents target selection and subsequent writes."""
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
        # The write has already been sent. A failed read-back means uncertain completion.
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

__all__ = ["list_my_favorites", "get_unread_summary", "add_favorite", "remove_favorite",
           "list_my_messages"]


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
