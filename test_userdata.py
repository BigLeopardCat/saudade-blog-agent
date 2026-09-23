# -*- coding: utf-8 -*-
"""用户自己的数据工具单测（纯函数 / 假 httpx，零网络零 LLM，秒级）。

被测各批：
  · **读**（20260923 批 6）：`list_my_favorites` / `get_unread_summary` /
    `list_notifications`，scope=`read.own`，走 `_own_get`；
  · **写**（20260923 批 7）：`add_favorite` / `remove_favorite` /
    `read_notifications`，scope=`write.own`；
  · **站内信**（20260923 批 8）：`list_my_messages`（read.own）与 `read_messages`
    （write.own）——**站内信（私信）不是站内通知**：两族各有各的 id 空间，摘要
    与回执的措辞刻意不同字（混了就是拿通知的 id 去标记信、或反过来）。本文件
    在 ③ 锁摘要、在 ④ 锁"发件箱里的 id 标不得"、在 ⑥ 锁两处回执措辞不同字，
    父仓 `chat.rs` 的同名测试再锁一次（`assert_ne!` 那一对）。

这一批与"管理助手"那批共用一个请求本体（`_principal_get`，见 tools/base.py）、
但**共用的只有请求形状**——两件事必须分开，本文件就是那两件事的回归锁：

  1. **措辞不能串**：后台接口读不到时说"仅管理员可用"是对的，用户自己的收藏读不到
     时这么说就是错的（那是他自己的数据）；反过来把后台报表说成"你未登录"更糟。
     所以 401/403 与 uid≤0 两处**四句话**逐条锁。
  2. **读不到 ≠ 是空的**：`_own_get` 任何一条不确定路径都必须 unavailable。
     退化成 `[]` 的话，narrator 会对着一次读失败说"你还没有收藏任何文章"——
     这正是这批工具最坏的失败形态（访客信了，且没有任何办法发现）。
  3. **未登录不发请求**：uid≤0 时一个字节都不出去（写侧同一条更严：写操作最不该
     做的就是在没身份时猜）。

写法沿用 test_reports.py：假 httpx 只桩边界（`base._client`），工具的输入输出
一律用真对象。
"""
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import authz  # noqa: E402
from agent.entities import receipt_digest  # noqa: E402
from agent.principal import Principal  # noqa: E402
import tools.base as base  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


READ_TOOLS = ["list_my_favorites", "get_unread_summary", "list_notifications",
              "list_my_messages"]

# 三个端点的返回样本（形态抄自 src/routes/profile.rs 的 FavoriteDto /
# UnreadDto / NotificationListDto，字段名一个不差——它们是 Python↔Rust 的契约）
FAVS = [{"noteId": 12, "title": "留言板怎么用", "status": "published",
         "createdAt": "2026-09-20 11:02:00"},
        {"noteId": 19, "title": "Saudade Blog AI Agent（泠月喵）架构文档", "status": "published",
         "createdAt": "2026-09-18 09:30:00"}]
UNREAD = {"notifications": 3, "messages": 1, "total": 4}
# 站内信（20260923 批 8）：Rust `MailboxDto{inbox, outbox, unread}` 的形态——
# **不是行列表**（所以进不了 `_rows` 那族摘要器），字段名抄自 src/routes/profile.rs
# 的 MessageDto（peerName 是"对方的展示名"，title 可为 null = 没填标题）
MAILBOX = {"unread": 2, "inbox": [
    {"id": 11, "fromUserId": 3, "toUserId": 7, "peerName": "小猫咪",
     "peerAvatar": None, "title": "河灯集那篇", "content": "第三段的说法想跟你确认一下",
     "isRead": False, "createdAt": "2026-09-22 21:00:00"},
    {"id": 9, "fromUserId": 4, "toUserId": 7, "peerName": "阿岚", "peerAvatar": None,
     "title": None, "content": "上次那个 ESP32 的问题解决了",
     "isRead": False, "createdAt": "2026-09-21 10:00:00"},
    {"id": 8, "fromUserId": 5, "toUserId": 7, "peerName": "路人甲", "peerAvatar": None,
     "title": "问好", "content": "你好呀", "isRead": True,
     "createdAt": "2026-09-01 08:00:00"},
], "outbox": [
    {"id": 10, "fromUserId": 7, "toUserId": 3, "peerName": "小猫咪",
     "peerAvatar": None, "title": "回你", "content": "好，我看下", "isRead": True,
     "createdAt": "2026-09-22 22:00:00"},
]}
NOTICES = {"unread": 2, "items": [
    {"id": 7, "type": "announcement", "title": "国庆维护公告", "content": "10 月 1 日凌晨维护",
     "link": "/article/3", "isRead": False, "createdAt": "2026-09-22 10:00:00"},
    {"id": 6, "type": "notice", "title": "你的留言已通过审核", "content": "理由：无",
     "link": "/guestbook?lid=88", "isRead": False, "createdAt": "2026-09-21 21:40:00"},
    {"id": 5, "type": "notice", "title": "欢迎来到 Saudade", "content": "随便逛逛",
     "link": None, "isRead": True, "createdAt": "2026-09-01 08:00:00"},
]}


# ══════════════════════════════════════════════════════════════════
print("\n① _own_get：请求形状与失败取向（假 httpx 客户端，只桩边界）")


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.text = ""

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _Client:
    def __init__(self, resp=None, exc=None):
        self.calls = []
        self.resp, self.exc = resp, exc

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers or {}))
        if self.exc:
            raise self.exc
        return self.resp


def cfg(uid, role="user"):
    return {"configurable": {"user_id": uid, "principal": Principal(uid=uid, role=role)}}


real_client = base._client
try:
    c = _Client(_Resp(200, {"code": 200, "data": FAVS}))
    base._client = c
    out = base._own_get("/api/protected/favorites", cfg(7))
    check("正常路径 → 返回 data 字段", out == FAVS, str(out)[:40])
    url, hdrs = c.calls[0]
    check("打的是本机回环后台地址（/api/protected 前缀）",
          url.startswith(base.ADMIN_BASE + "/api/protected/"), url)
    tok = hdrs.get("Authorization", "")
    check("带 Bearer 局部 JWT（两段点 = 三段式）",
          tok.startswith("Bearer ") and tok.count(".") == 2)
    seg = tok.split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    check("JWT sub = 本轮发起人 uid（自己读自己，服务端再按 uid 过滤）",
          payload.get("sub") == 7, str(payload))
    check("JWT 有效期 60 秒（当场用掉，不持有）", 50 <= payload["exp"] - time.time() <= 60)
    check("JWT 不带 aud（Rust verify_token 用 Validation::default）",
          "aud" not in payload, str(list(payload)))

    # 401/403：**身份**问题不是故障，且措辞必须是"你自己的数据"那一套
    for status, why in ((401, "未带/验签失败"), (403, "身份有效但无权")):
        c = _Client(_Resp(status))
        base._client = c
        r = base._own_get("/api/protected/favorites", cfg(7))
        check(f"{status}（{why}）→ unavailable 且措辞是『无权』",
              r.kind == "unavailable" and "无权" in r, f"{r.kind}: {r}")
        check(f"{status} → **不说**『仅管理员可用』（这是访客自己的数据）",
              "管理员" not in str(r), str(r))

    c = _Client(_Resp(500))
    base._client = c
    r = base._own_get("/api/protected/favorites", cfg(7))
    check("HTTP 500 → unavailable（不是空列表）", r.kind == "unavailable", f"{r.kind}: {r}")

    c = _Client(_Resp(200, None))
    base._client = c
    r = base._own_get("/api/protected/favorites", cfg(7))
    check("非 JSON 响应 → unavailable", r.kind == "unavailable", f"{r.kind}: {r}")

    # Rust 的 ApiResponse::error 是 HTTP 200 + code 500（src/utils.rs）——
    # 只看状态码会把"未登录/查询失败"读成成功
    c = _Client(_Resp(200, {"code": 500, "message": "未登录"}))
    base._client = c
    r = base._own_get("/api/protected/favorites", cfg(7))
    check("业务码非 200 → unavailable（HTTP 200 也不当成功）",
          r.kind == "unavailable" and "未登录" in r, f"{r.kind}: {r}")

    c = _Client(exc=RuntimeError("connection refused"))
    base._client = c
    r = base._own_get("/api/protected/favorites", cfg(7))
    check("连接异常 → unavailable", r.kind == "unavailable", f"{r.kind}: {r}")

    c = _Client(_Resp(200, {"code": 200, "data": []}))
    base._client = c
    r = base._own_get("/api/protected/favorites", cfg(0))
    check("uid ≤ 0 → unavailable（身份拿不到就不发请求）", r.kind == "unavailable", f"{r.kind}: {r}")
    check("uid ≤ 0 的措辞说的是『未登录』（不是『无法获取身份』这种系统腔）",
          "未登录" in str(r), str(r))
    check("uid ≤ 0 时**没有**发出任何请求", c.calls == [], str(c.calls))
finally:
    base._client = real_client


# ══════════════════════════════════════════════════════════════════
print("\n② 三个读工具：以发起人身份读、读不到就如实说（走真工具）")

CASES = [("list_my_favorites", FAVS), ("get_unread_summary", UNREAD),
         ("list_notifications", NOTICES), ("list_my_messages", MAILBOX)]
try:
    for name, sample in CASES:
        tool = getattr(base, name)
        c = _Client(_Resp(200, {"code": 200, "data": sample}))
        base._client = c
        out = tool.invoke({}, config=cfg(7))
        check(f"{name} → 原样透出接口数据（结构化，不做中文报表）",
              out == str(sample) and not getattr(out, "kind", ""), str(out)[:50])
        check(f"{name} 打的端点在 /api/protected 下", len(c.calls) == 1, str(c.calls))

        c = _Client(_Resp(200, {"code": 200, "data": sample}))
        base._client = c
        out = tool.invoke({}, config=cfg(0))
        check(f"{name} 未登录 → kind=unavailable 且零请求",
              out.kind == "unavailable" and c.calls == [], f"{out.kind}: {out}")
        check(f"{name} 未登录的措辞里没有『管理员』", "管理员" not in str(out), str(out))

    # 空结果**不是**故障：`_shape([])` 出来的就是 `"[]"`（无 kind 标记 ⇒ 上层按
    # kind 默认 ok 判 PASS，"查到了、就是空的"是事实）。这条与上面那条要分开锁——
    # 混了以后"你还没收藏过文章"与"我读不到"就分不开了。
    c = _Client(_Resp(200, {"code": 200, "data": []}))
    base._client = c
    out = base.list_my_favorites.invoke({}, config=cfg(7))
    check("收藏为空 → 原样透出 '[]'（不是 unavailable、不是『暂无收藏』这种人话）",
          out == "[]" and not getattr(out, "kind", ""), f"{getattr(out, 'kind', '')}: {out}")
finally:
    base._client = real_client


# ══════════════════════════════════════════════════════════════════
print("\n③ 实体摘要：跨轮指代的取值来源")

check("收藏 → `noteId《标题》`（noteId 是 favorites 的字段名，此前只有 noteKey/key）",
      receipt_digest("list_my_favorites", str(FAVS))
      == "我的收藏: 12《留言板怎么用》/19《Saudade Blog AI Agent》",
      receipt_digest("list_my_favorites", str(FAVS)))
check("通知 → 总条数 + 未读条数 + **id《标题》**（未读标出来）",
      receipt_digest("list_notifications", str(NOTICES))
      == "通知共 3 条（未读 2）: 7《国庆维护公告》（未读）/6《你的留言已通过审核》（未读）/5《欢迎来到 Saudade》",
      receipt_digest("list_notifications", str(NOTICES)))
# 20260923 三轮（trace 实证）：摘要里的"3 条"曾被 planner 读成 id 填进参数
# ⇒ 一次注定 0 行的写 + 一次「服务不可用」误报。两条判据分开锁：
#   ① id 必须给足（上面那条）；
#   ② 条数不得写成可被读成 id 的形态（抬头带"共"字、且数字紧贴"条"）。
check("通知条数带「共…条」措辞（「3 条」不再是一个可被读成 id 的裸数字）",
      "共 3 条" in receipt_digest("list_notifications", str(NOTICES))
      and not receipt_digest("list_notifications", str(NOTICES)).startswith("通知 3"))
check("通知条目缺 id 字段 → 不编 id（只给标题）",
      receipt_digest("list_notifications", str({"unread": 0, "items": [
          {"title": "没有 id 的通知", "isRead": True}]})) ==
      "通知共 1 条（未读 0）: 没有 id 的通知")
check("通知**正文不进摘要**（挤进来只会把标题挤掉——要正文就再调一次工具）",
      "10 月 1 日" not in receipt_digest("list_notifications", str(NOTICES)))
# ── 站内信（20260923 批 8）─────────────────────────────────────
check("站内信 → 收件/发出封数 + 未读封数 + **id《标题》寄自谁（未读）**",
      receipt_digest("list_my_messages", str(MAILBOX))
      == "信箱: 收件 3 封（未读 2） / 发出 1 封: 11《河灯集那篇》寄自小猫咪（未读）"
         "/9（无标题信）寄自阿岚（未读）/8《问好》寄自路人甲",
      receipt_digest("list_my_messages", str(MAILBOX)))
# id 给足是**判据的一部分**：20260923 三轮那次「把条数当 id」的误导，堵法只有
# "摘要里每个可点名的东西都带编号"（见 agent/entities.py `_mailbox_digest` 头注）。
check("信件摘要里每封都带编号（planner 不必编 id 就能点名）",
      all(f"{i}《" in receipt_digest("list_my_messages", str(MAILBOX)) or
          f"{i}（无标题信）" in receipt_digest("list_my_messages", str(MAILBOX))
          for i in (11, 9, 8)))
check("信件摘要用**封**这个量词（通知是「条」——量词分开，两个 id 空间就不会被混）",
      "封" in receipt_digest("list_my_messages", str(MAILBOX))
      and "条" not in receipt_digest("list_my_messages", str(MAILBOX)))
check("发给别人的信**不进摘要**（发件箱里的 isRead 是别人的事，也不能被当成本人收到过）",
      "10《回你》" not in receipt_digest("list_my_messages", str(MAILBOX)))
check("信件**正文不进摘要**（要正文就再调一次工具）",
      "第三段" not in receipt_digest("list_my_messages", str(MAILBOX)))
check("缺 id 的信 → 不编编号（只给标题/寄信人）",
      receipt_digest("list_my_messages",
                     str({"inbox": [{"peerName": "无 id 无标题"}], "outbox": [],
                          "unread": 1})) == "信箱: 收件 1 封（未读 1） / 发出 0 封: （无标题信）寄自无 id 无标题（未读）")
check("空信箱 → 只留封数（不是空串：**一封都没有**是事实，与「读不到」不是一回事）",
      receipt_digest("list_my_messages",
                     str({"inbox": [], "outbox": [], "unread": 0}))
      == "信箱: 收件 0 封（未读 0） / 发出 0 封")
check("信箱形态不符（缺 inbox/outbox）→ 空摘要（不猜）",
      receipt_digest("list_my_messages", str({"unread": 0, "items": []})) == ""
      and receipt_digest("list_my_messages", "未登录：读不到你自己的数据") == "")

check("未读汇总 → 两个数 + 合计（key 名与 Rust UnreadDto 同源）",
      receipt_digest("get_unread_summary", str(UNREAD))
      == "未读: 通知 3 条 / 私信 1 条（合计 4）",
      receipt_digest("get_unread_summary", str(UNREAD)))

# 缺字段/形态不符一律空串——**不写 0**（"0 条未读"是结论，"没读到字段"不是）
check("未读汇总缺字段 → 空摘要（不写 0）",
      receipt_digest("get_unread_summary", str({"notifications": 3})) == "")
check("未读汇总字段是字符串 → 空摘要（不 int() 硬转）",
      receipt_digest("get_unread_summary", str({"notifications": "3", "messages": 0, "total": 3})) == "")
check("空列表 → 空摘要（退化为改动前行为）",
      receipt_digest("list_my_favorites", "[]") == ""
      and receipt_digest("list_notifications", str({"unread": 0, "items": []})) == "")
check("读不到（UPSTREAM_DOWN 文本）→ 空摘要",
      receipt_digest("list_my_favorites", "UPSTREAM_DOWN") == ""
      and receipt_digest("list_notifications", "未登录：读不到你自己的数据") == "")
check("通知摘要 ≤150 字（Rust detail 列 varchar(300) 的一半留给动作行）",
      len(receipt_digest("list_notifications",
                         str({"unread": 0, "items": [
                             {"id": i, "title": "很长很长的一条通知标题用来把摘要撑爆" * 2,
                              "isRead": True} for i in range(8)]}))) <= 150)


# ══════════════════════════════════════════════════════════════════
print("\n④ 写侧：写前先读（读不到就不写）＋ 写后复核（读不回就不确认）")

WRITE_TOOLS = ["add_favorite", "remove_favorite", "read_notifications",
               "read_messages"]


def _data(x):
    return _Resp(200, {"code": 200, "data": x})


def _err(status=500):
    return _Resp(status)


class _SeqClient:
    """按调用**顺序**逐次出响应的桩：写工具一轮里有"写前读 → 写 → 写后再读"
    三次不同的返回，单响应桩根本表达不了"写成功了但复核失败"。

    响应不够用时重复最后一个（省得每个用例都要补一条尾巴）。只桩边界，方法名与
    httpx 一致（`_principal_request` 走 `.request`，`_principal_get` 走 `.get`）。
    """

    def __init__(self, *resps):
        self.calls = []
        self._resps = list(resps)

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, headers or {}, json))
        r = self._resps[min(len(self.calls), len(self._resps)) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, headers=None, timeout=None):
        return self.request("GET", url, headers=headers)

    def post(self, url, headers=None, json=None, timeout=None):
        return self.request("POST", url, headers=headers, json=json)

    def delete(self, url, headers=None, timeout=None):
        return self.request("DELETE", url, headers=headers)


def _writes(c):
    """桩记录里的写请求（非 GET）——每个用例都要断言"发了几次写、写的是什么"。"""
    return [(m, u, p) for m, u, _h, p in c.calls if m != "GET"]


FAV12 = {"noteId": 12, "title": "留言板怎么用", "status": "published",
         "createdAt": "2026-09-20 11:02:00"}

try:
    # ── add_favorite：成功路径 ────────────────────────────────────
    base._client = _SeqClient(_data([FAVS[1]]), _data({"ok": True}),
                              _data([FAVS[1], FAV12]))
    c = base._client
    out = base.add_favorite.invoke({"article_id": 12}, config=cfg(7))
    check("收藏成功 → ok，回执写明文章 id 与标题",
          out.kind == "ok" and "已收藏文章 12《留言板怎么用》" in out, f"{out.kind}: {out}")
    check("回执带上收藏夹条数（读得出来才写，读不出不写 0）",
          "你的收藏夹现在有 2 篇" in out, str(out))
    check("顺序 = 写前读 → 写 → 写后再读（三次请求）",
          [m for m, _u, _h, _p in c.calls] == ["GET", "POST", "GET"],
          str([m for m, _u, _h, _p in c.calls]))
    check("写请求体是 {'noteId': 12}（Rust 侧字段名，不是 article_id）",
          _writes(c) == [("POST", base.ADMIN_BASE + "/api/protected/favorites", {"noteId": 12})],
          str(_writes(c)))
    check("三次请求都带同一把局部 JWT（写前读与写同一身份）",
          len({h.get("Authorization") for _m, _u, h, _p in c.calls}) == 1, str(c.calls[0][2]))

    # ── add_favorite：幂等（写前读的副产品）────────────────────────
    base._client = _SeqClient(_data([FAV12]))
    c = base._client
    out = base.add_favorite.invoke({"article_id": 12}, config=cfg(7))
    check("已收藏过 → 如实说本来就有，**零写请求**",
          out.kind == "ok" and "本来就在你的收藏夹里" in out and _writes(c) == [],
          f"{out.kind}: {out} / {_writes(c)}")
    check("幂等分支带回执标记 noop（跨轮执行记忆据此区分「改过」与「本来如此」）",
          out.meta.get("noop") is True and out.meta.get("change") == "本来已收藏", str(out.meta))

    # ── add_favorite：fail-closed 三条 ────────────────────────────
    base._client = _SeqClient(_err(500))
    c = base._client
    out = base.add_favorite.invoke({"article_id": 12}, config=cfg(7))
    check("写前读失败 → unavailable 且零写请求（读不到就不写）",
          out.kind == "unavailable" and _writes(c) == [], f"{out.kind}: {out}")
    check("写前读失败的措辞说了『未改动』", "未改动" in str(out), str(out))

    base._client = _SeqClient(_data({"ok": True}))
    c = base._client
    out = base.add_favorite.invoke({"article_id": 0}, config=cfg(7))
    check("文章 id 不合法（0）→ unavailable 且**一个请求都不发**",
          out.kind == "unavailable" and c.calls == [], f"{out.kind}: {out} / {c.calls}")

    base._client = _SeqClient(_data([FAVS[1]]), _data({"ok": True}), _data([FAVS[1]]))
    c = base._client
    out = base.add_favorite.invoke({"article_id": 12}, config=cfg(7))
    check("写后复核：读回来还是没有这一篇 → unavailable（不拿接口的成功文案当事实）",
          out.kind == "unavailable" and "未确认生效" in str(out), f"{out.kind}: {out}")
    check("写后复核失败的措辞明确写了『不要声称已收藏』", "不要声称已收藏" in str(out), str(out))

    base._client = _SeqClient(_data([FAVS[1]]), _data({"ok": True}), _err(500))
    out = base.add_favorite.invoke({"article_id": 12}, config=cfg(7))
    check("写已发出但复核读失败 → 措辞是『已发出 + 未确认生效』（不是读侧那句原话）",
          out.kind == "unavailable" and "收藏请求已发出" in str(out)
          and "不要声称已收藏" in str(out), f"{out.kind}: {out}")

    # ── remove_favorite ──────────────────────────────────────────
    base._client = _SeqClient(_data([FAVS[1], FAV12]), _data({"ok": True}), _data([FAVS[1]]))
    c = base._client
    out = base.remove_favorite.invoke({"article_id": 12}, config=cfg(7))
    check("取消收藏成功 → DELETE 到 /favorites/<id>（payload 为 None，端点从路径取参）",
          _writes(c) == [("DELETE", base.ADMIN_BASE + "/api/protected/favorites/12", None)],
          str(_writes(c)))
    check("取消收藏成功回执写明标题（取的是写前那一行——写后就没了）",
          out.kind == "ok" and "已取消收藏文章 12《留言板怎么用》" in out, f"{out.kind}: {out}")

    base._client = _SeqClient(_data([FAVS[1]]))
    c = base._client
    out = base.remove_favorite.invoke({"article_id": 12}, config=cfg(7))
    check("本来就没收藏 → 如实说，**零写请求**（同一个按钮点两次不该报错）",
          out.kind == "ok" and "本来就不在你的收藏夹里" in out and _writes(c) == [],
          f"{out.kind}: {out} / {_writes(c)}")

    base._client = _SeqClient(_data([FAVS[1], FAV12]), _data({"ok": True}),
                              _data([FAVS[1], FAV12]))
    out = base.remove_favorite.invoke({"article_id": 12}, config=cfg(7))
    check("写后复核：读回来还在 → unavailable 且写明『不要声称已取消』",
          out.kind == "unavailable" and "不要声称已取消" in str(out), f"{out.kind}: {out}")

    # ── read_notifications：范围判据（没范围就零工具）──────────────
    base._client = _SeqClient(_data(NOTICES))
    c = base._client
    out = base.read_notifications.invoke({}, config=cfg(7))
    check("既没给 id 也没说『全部』→ unavailable 且零请求（不猜范围）",
          out.kind == "unavailable" and c.calls == [], f"{out.kind}: {out} / {c.calls}")
    base._client = _SeqClient(_data(NOTICES))
    c = base._client
    out = base.read_notifications.invoke({"all": False}, config=cfg(7))
    check("all=False 不等于『全部』→ 同样零请求（不默认 True）",
          out.kind == "unavailable" and c.calls == [], f"{out.kind}: {out} / {c.calls}")

    # ── read_notifications：幂等两态 ──────────────────────────────
    base._client = _SeqClient(_data(NOTICES))
    c = base._client
    out = base.read_notifications.invoke({"ids": [5]}, config=cfg(7))
    check("点名的 id 本来就是已读 → 零写请求、如实说本来就读过",
          out.kind == "ok" and "本来就是已读" in out and _writes(c) == [],
          f"{out.kind}: {out} / {_writes(c)}")
    base._client = _SeqClient(_data({"unread": 0, "items": [NOTICES["items"][2]]}))
    c = base._client
    out = base.read_notifications.invoke({"all": True}, config=cfg(7))
    check("全部已读 → 零写请求、如实说本来就没有未读",
          out.kind == "ok" and "本来就没有未读的" in out and _writes(c) == [],
          f"{out.kind}: {out} / {_writes(c)}")

    # ── read_notifications：点名的 id 不在自己列表里（20260923 三轮）────
    # 生产事故（trace `20260923T130033_9`）：planner 把摘要里的**条数**「3」当 id
    # 填进 `ids:[3]`（真实那条是 23）⇒ 服务端按 3 匹配 0 行 ⇒ 写后复核报
    # "未确认生效"，过程行还显示成「服务不可用」。现在**写之前**就判：读到的列表里
    # 没有这几个编号 ⇒ 一个字节都不发 + kind=not_found（与"服务挂了"分开）。
    base._client = _SeqClient(_data(NOTICES))
    c = base._client
    out = base.read_notifications.invoke({"ids": [3]}, config=cfg(7))
    check("点名的 id 不在读到的列表里 → kind=not_found 且**零写请求**（不是服务故障）",
          out.kind == "not_found" and _writes(c) == [], f"{out.kind}: {out} / {_writes(c)}")
    check("措辞点名了那个 id、写明一个字节没改、并把列表上限如实交代（不谎称站内没有）",
          "通知 3 不在" in str(out) and "一个字节都没改" in str(out)
          and "100" in str(out) and "未确认生效" not in str(out), str(out))

    base._client = _SeqClient(_data(NOTICES))
    c = base._client
    out = base.read_notifications.invoke({"ids": [7, 8]}, config=cfg(7))
    check("混着来（7 在列表里、8 不在）→ 同样**整批零写**（绝不部分改）",
          out.kind == "not_found" and _writes(c) == [], f"{out.kind}: {out} / {_writes(c)}")
    check("混着来的措辞也给另一条出口（「全部标记已读」由系统按未读的来标）",
          "全部标记已读" in str(out), str(out))

    base._client = _SeqClient(_data(NOTICES), _data(UNREAD), _data({"ok": True}),
                              _data({"notifications": 0, "messages": 1, "total": 1}))
    c = base._client
    out = base.read_notifications.invoke({"all": True}, config=cfg(7))
    check("「全部」路径不受新判据影响（它不点名 id，直接按读到的未读来标）",
          out.kind == "ok" and _writes(c) and _writes(c)[0][2] == {"ids": [], "all": True},
          f"{out.kind}: {out} / {_writes(c)}")

    # ── read_notifications：成功路径（复核判据 = 未读数真的下降）───
    base._client = _SeqClient(_data(NOTICES), _data(UNREAD), _data({"ok": True}),
                              _data({"notifications": 0, "messages": 1, "total": 1}))
    c = base._client
    out = base.read_notifications.invoke({"all": True}, config=cfg(7))
    check("全部标记已读 → POST payload {'ids': [], 'all': True}",
          _writes(c) == [("POST", base.ADMIN_BASE + "/api/protected/notifications/read",
                          {"ids": [], "all": True})], str(_writes(c)))
    check("成功回执报的是**减少的条数**（3→0）与剩余未读",
          out.kind == "ok" and "已把 3 条通知标记为已读" in out
          and "现在未读：通知 0 条 / 私信 1" in out, f"{out.kind}: {out}")
    check("顺序 = 写前读列表 → 写前读汇总 → 写 → 写后读汇总（四次，复核是独立读数）",
          [m for m, _u, _h, _p in c.calls] == ["GET", "GET", "POST", "GET"],
          str([m for m, _u, _h, _p in c.calls]))

    base._client = _SeqClient(_data(NOTICES), _data(UNREAD), _data({"ok": True}),
                              _data({"notifications": 1, "messages": 1, "total": 2}))
    c = base._client
    out = base.read_notifications.invoke({"ids": [7, 6, 7]}, config=cfg(7))
    check("点名 id 路径 → payload 用 sorted 去重后的 id（'all': False）",
          _writes(c)[0][2] == {"ids": [6, 7], "all": False}, str(_writes(c)))
    check("复核只认服务端重数出来的未读数：2→1 也在降，报 2 条",
          out.kind == "ok" and "已把 2 条通知标记为已读" in out, f"{out.kind}: {out}")

    base._client = _SeqClient(_data(NOTICES), _data(UNREAD), _data({"ok": True}),
                              _data({"notifications": 3, "messages": 1, "total": 4}))
    out = base.read_notifications.invoke({"all": True}, config=cfg(7))
    check("复核判据 = 未读数**真的下降**：没降 → unavailable（接口说成功也不算数）",
          out.kind == "unavailable" and "未确认生效" in str(out), f"{out.kind}: {out}")
    check("未下降的措辞明确写了『不要声称已标记』", "不要声称已标记" in str(out), str(out))

    base._client = _SeqClient(_data(NOTICES), _err(500))
    c = base._client
    out = base.read_notifications.invoke({"all": True}, config=cfg(7))
    check("写前读汇总失败 → 零写请求（拿不到基准数就不写）",
          out.kind == "unavailable" and _writes(c) == [], f"{out.kind}: {out} / {_writes(c)}")

    base._client = _SeqClient(_data(NOTICES), _data({"unread": "3"}), _data({"ok": True}))
    c = base._client
    out = base.read_notifications.invoke({"all": True}, config=cfg(7))
    check("基准数不是 int（形态不符）→ 零写请求（不 int() 硬转）",
          out.kind == "unavailable" and _writes(c) == [], f"{out.kind}: {out} / {_writes(c)}")

    base._client = _SeqClient(_data(NOTICES), _data(UNREAD), _data({"ok": True}), _err(500))
    out = base.read_notifications.invoke({"all": True}, config=cfg(7))
    check("写已发出但复核读失败 → 措辞是『已发出 + 未确认生效』",
          out.kind == "unavailable" and "标记已读请求已发出" in str(out)
          and "不要声称已标记" in str(out), f"{out.kind}: {out}")

    # ── read_messages（20260923 批 8）：与 read_notifications 同一形状，
    #    但物件是**收到**的信——发件箱里的 id 不能标、别人读没读也不归他管。──
    MB_AFTER = {"unread": 1, "inbox": [MAILBOX["inbox"][0],
                                       {**MAILBOX["inbox"][1], "isRead": True},
                                       MAILBOX["inbox"][2]],
                "outbox": MAILBOX["outbox"]}
    MB_ALL_READ = {"unread": 0,
                   "inbox": [{**r, "isRead": True} for r in MAILBOX["inbox"]],
                   "outbox": MAILBOX["outbox"]}

    base._client = _SeqClient(_data(MAILBOX))
    c = base._client
    out = base.read_messages.invoke({}, config=cfg(7))
    check("既没给 id 也没说『全部』→ unavailable 且零请求（同一句命令的双胞胎工具也不猜）",
          out.kind == "unavailable" and c.calls == [], f"{out.kind}: {out} / {c.calls}")
    base._client = _SeqClient(_data(MAILBOX))
    c = base._client
    out = base.read_messages.invoke({"all": False}, config=cfg(7))
    check("all=False 不等于『全部』→ 同样零请求",
          out.kind == "unavailable" and c.calls == [], f"{out.kind}: {out} / {c.calls}")

    base._client = _SeqClient(_data(MAILBOX))
    c = base._client
    out = base.read_messages.invoke({"ids": [8]}, config=cfg(7))
    check("点名的信本来就是已读 → 零写请求、如实说本来就读过",
          out.kind == "ok" and "本来就是已读" in out and _writes(c) == [],
          f"{out.kind}: {out} / {_writes(c)}")
    base._client = _SeqClient(_data(MB_ALL_READ))
    c = base._client
    out = base.read_messages.invoke({"all": True}, config=cfg(7))
    check("全部已读 → 零写请求、如实说本来就没有未读的信",
          out.kind == "ok" and "本来就没有未读的信" in out and _writes(c) == [],
          f"{out.kind}: {out} / {_writes(c)}")

    # 发件箱里的 id（10 是"我发出去的那封"）同样不在收件箱里 ⇒ 一个字节都不发。
    # 这条是这族工具的**独有**风险：通知只有一个列表，信有两个（收/发），
    # 拿发件箱的 id 去标已读是"改别人的东西"的错觉。
    base._client = _SeqClient(_data(MAILBOX))
    c = base._client
    out = base.read_messages.invoke({"ids": [10]}, config=cfg(7))
    check("点的是**发件箱**里的信（我发出去的）→ kind=not_found 且零写请求",
          out.kind == "not_found" and _writes(c) == [], f"{out.kind}: {out} / {_writes(c)}")
    check("措辞把出口写清楚（先说读一遍信箱拿 id），且不谎称站内没有这封信",
          "不在我读到的收件箱" in str(out) and "一个字节都没改" in str(out)
          and "未确认生效" not in str(out), str(out))

    base._client = _SeqClient(_data(MAILBOX))
    c = base._client
    out = base.read_messages.invoke({"ids": [11, 99]}, config=cfg(7))
    check("混着来（11 在、99 不在）→ **整批零写**（绝不部分改）",
          out.kind == "not_found" and _writes(c) == [], f"{out.kind}: {out} / {_writes(c)}")

    # 成功路径：写前读 → 写 → 写后再读（三次，复核是独立读数）
    base._client = _SeqClient(_data(MAILBOX), _data({"ok": True}), _data(MB_ALL_READ))
    c = base._client
    out = base.read_messages.invoke({"all": True}, config=cfg(7))
    check("全部标记已读 → POST {'ids': [], 'all': True} 到 /messages/read",
          _writes(c) == [("POST", base.ADMIN_BASE + "/api/protected/messages/read",
                          {"ids": [], "all": True})], str(_writes(c)))
    check("顺序 = 写前读信箱 → 写 → 写后再读信箱（三次）",
          [m for m, _u, _h, _p in c.calls] == ["GET", "POST", "GET"],
          str([m for m, _u, _h, _p in c.calls]))
    check("成功回执报的是**减少的封数**（2→0）与收件箱剩余未读",
          out.kind == "ok" and "已把 2 封信标记为已读" in out
          and "收件箱现在未读 0 封" in out, f"{out.kind}: {out}")
    check("回执 meta 是 message_read（与通知那族的 op 分开——跨轮记忆按 op 区分）",
          out.meta.get("op") == "message_read", str(out.meta))

    base._client = _SeqClient(_data(MAILBOX), _data({"ok": True}), _data(MB_AFTER))
    c = base._client
    out = base.read_messages.invoke({"ids": [9, 9]}, config=cfg(7))
    check("点名路径 → payload 用 sorted 去重后的 id（'all': False）",
          _writes(c)[0][2] == {"ids": [9], "all": False}, str(_writes(c)))
    check("复核只认服务端重数出来的未读封数（2→1 也在降）",
          out.kind == "ok" and "已把 1 封信标记为已读" in out, f"{out.kind}: {out}")

    # 「报的是服务端重数出来的数」与「报的是我点了几封」在数字上通常是同一个，
    # 这里造一个**能让两者分开**的夹具：收件箱列表被截到只剩 2 封未读可见（最多
    # 100 封），而服务端报的未读是 5 —— 复核读回来 4 ⇒ 净降 1，如实报 1（不是 2）。
    # 宁可报小：跨轮执行记忆里"标了 2 封"而实际只降了 1 是虚报。
    MB_TRUNC = {"unread": 5, "inbox": MAILBOX["inbox"][:2], "outbox": MAILBOX["outbox"]}
    MB_TRUNC_AFTER = {"unread": 4,
                      "inbox": [{**r, "isRead": True} for r in MAILBOX["inbox"][:2]],
                      "outbox": MAILBOX["outbox"]}
    base._client = _SeqClient(_data(MB_TRUNC), _data({"ok": True}), _data(MB_TRUNC_AFTER))
    out = base.read_messages.invoke({"all": True}, config=cfg(7))
    check("报的封数 = 服务端两次读数之差（不是「我点了几封」）",
          out.kind == "ok" and "已把 1 封信标记为已读" in out, f"{out.kind}: {out}")

    # 复核判据是**两条**：封数真降 + 点名的每一封都确实已读。只看"降了"会在
    # "标错了一封、又漏了一封"的巧合下判成功（净变化相同）。
    base._client = _SeqClient(_data(MAILBOX), _data({"ok": True}), _data(MB_AFTER))
    out = base.read_messages.invoke({"all": True}, config=cfg(7))
    check("封数降了但有一封仍不是已读 → unavailable（净变化相同也能抓出来）",
          out.kind == "unavailable" and "还不是已读" in str(out), f"{out.kind}: {out}")
    check("未确认生效的措辞明确写了『不要声称已标记』", "不要声称已标记" in str(out), str(out))

    base._client = _SeqClient(_data(MAILBOX), _data({"ok": True}),
                              _data({"unread": 2, "inbox": [{**r, "isRead": False}
                                                            for r in MAILBOX["inbox"]],
                                     "outbox": MAILBOX["outbox"]}))
    out = base.read_messages.invoke({"all": True}, config=cfg(7))
    check("未读封数**没降** → unavailable（接口说成功也不算数）",
          out.kind == "unavailable" and "未确认生效" in str(out), f"{out.kind}: {out}")

    base._client = _SeqClient(_err(500))
    c = base._client
    out = base.read_messages.invoke({"all": True}, config=cfg(7))
    check("写前读失败 → 零写请求（读不到就不写）",
          out.kind == "unavailable" and _writes(c) == [] and "未改动" in str(out),
          f"{out.kind}: {out} / {_writes(c)}")

    base._client = _SeqClient(_data({"unread": "2", "inbox": [MAILBOX["inbox"][0]],
                                     "outbox": []}))
    c = base._client
    out = base.read_messages.invoke({"all": True}, config=cfg(7))
    check("未读封数形态不符（字符串）→ 零写请求（不 int() 硬转）",
          out.kind == "unavailable" and _writes(c) == [], f"{out.kind}: {out} / {_writes(c)}")

    base._client = _SeqClient(_data(MAILBOX), _data({"ok": True}), _err(500))
    out = base.read_messages.invoke({"all": True}, config=cfg(7))
    check("写已发出但复核读失败 → 措辞是『已发出 + 未确认生效』",
          out.kind == "unavailable" and "标记已读请求已发出" in str(out)
          and "不要声称已标记" in str(out), f"{out.kind}: {out}")

    # ── 未登录：写侧比读侧更严（一个字节都不发）───────────────────
    for name, args in (("add_favorite", {"article_id": 12}),
                       ("remove_favorite", {"article_id": 12}),
                       ("read_notifications", {"all": True}),
                       ("read_messages", {"all": True})):
        base._client = _SeqClient(_data([]))
        c = base._client
        out = getattr(base, name).invoke(args, config=cfg(0))
        check(f"{name} 未登录 → unavailable 且**零请求**（写最不该在没身份时猜）",
              out.kind == "unavailable" and c.calls == [], f"{out.kind}: {out} / {c.calls}")
        check(f"{name} 未登录的措辞说了『未改动』且不提『管理员』",
              "未改动" in str(out) and "管理员" not in str(out), str(out))
finally:
    base._client = real_client


# ══════════════════════════════════════════════════════════════════
print("\n⑤ 写技能展开：范围缺失就零工具（不是零参数硬跑）")

from agent.skills import (SKILL_MAP, _OWN_WRITE_SKILLS,  # noqa: E402
                          _WRITE_NAME_TARGET_SKILLS, WRITE_SKILL_NAMES)
from agent.skills import instantiate_plan  # noqa: E402


def _plan(name, params):
    return instantiate_plan(name, params)


def _args(spec):
    """`name({...})` → (工具名, 参数字典)，与 execute_node 同一门语法。"""
    tool, _, rest = spec.partition("(")
    return tool, json.loads(rest.rstrip(")"))


p = _plan("favorite_add", {"article_id": 12})
check("favorite_add 展开成一条 add_favorite（参数是确切 id，不是 null）",
      len(p["tools"]) == 1 and _args(p["tools"][0]) == ("add_favorite", {"article_id": 12}),
      str(p["tools"]))
p = _plan("favorite_add", {"article_id": "$tool[0].noteId"})
check("favorite_add 认引用语法（$tool[…] 不被当成 PARAMS 里的键、原样落进参数）",
      p["tools"] and _args(p["tools"][0])[1] == {"article_id": "$tool[0].noteId"},
      str(p["tools"]))
p = _plan("favorite_add", {})
check("favorite_add 缺 article_id → 零工具 + 注记（不猜一篇文章去收藏）",
      p["tools"] == [] and p["note"], f"{p['tools']} / {p['note']}")

p = _plan("favorite_remove", {"article_id": 12})
check("favorite_remove 展开成一条 remove_favorite",
      len(p["tools"]) == 1 and _args(p["tools"][0])[0] == "remove_favorite", str(p["tools"]))

p = _plan("notice_read", {"all": True})
check("notice_read all=True → 一条 read_notifications({'all': True})",
      len(p["tools"]) == 1 and _args(p["tools"][0]) == ("read_notifications", {"all": True}),
      str(p["tools"]))
p = _plan("notice_read", {"ids": [7, 8]})
check("notice_read ids → 一条 read_notifications({'ids': [7, 8]})",
      p["tools"] and _args(p["tools"][0])[1] == {"ids": [7, 8]}, str(p["tools"]))
p = _plan("notice_read", {})
check("notice_read 既没 id 也没 all → **零工具**（最危险的一份「全部标记已读」绝不擅自展开）",
      p["tools"] == [] and p["note"], f"{p['tools']} / {p['note']}")
p = _plan("notice_read", {"all": True, "ids": [7]})
check("notice_read all 与 ids 同时给 → 零工具 + 追问（两者语义冲突，不替用户选）",
      p["tools"] == [] and p["note"], f"{p['tools']} / {p['note']}")
p = _plan("notice_read", {"all": "True"})
check("notice_read all 是字符串 'True'（planner 常见形态）也认",
      p["tools"] and _args(p["tools"][0]) == ("read_notifications", {"all": True}),
      str(p["tools"]))
p = _plan("notice_read", {"all": "maybe"})
check("notice_read all 认不出来（'maybe'）→ 零工具（不默认 True）",
      p["tools"] == [] and p["note"], f"{p['tools']} / {p['note']}")

# notice_read 的双胞胎（20260923 批 8）：同一形状、另一个物件。这一组刻意**逐条
# 对称**——两个技能共用 `_expand_own_skill` 的同一个分支，哪一条不对称就说明
# 有一边漏改了（例如"缺范围就零工具"只在通知那半保留着）。
p = _plan("message_read", {"all": True})
check("message_read all=True → 一条 read_messages({'all': True})",
      len(p["tools"]) == 1 and _args(p["tools"][0]) == ("read_messages", {"all": True}),
      str(p["tools"]))
p = _plan("message_read", {"ids": [11, 9]})
check("message_read ids → 一条 read_messages({'ids': [11, 9]})",
      p["tools"] and _args(p["tools"][0])[1] == {"ids": [11, 9]}, str(p["tools"]))
p = _plan("message_read", {})
check("message_read 既没 id 也没 all → **零工具**（把全部未读一次清掉是不可逆的）",
      p["tools"] == [] and p["note"], f"{p['tools']} / {p['note']}")
p = _plan("message_read", {"all": True, "ids": [11]})
check("message_read all 与 ids 同时给 → 零工具 + 追问",
      p["tools"] == [] and p["note"], f"{p['tools']} / {p['note']}")
p = _plan("message_read", {"all": "True"})
check("message_read all 是字符串 'True' 也认",
      p["tools"] and _args(p["tools"][0]) == ("read_messages", {"all": True}),
      str(p["tools"]))
p = _plan("message_read", {})
check("message_read 的注记指向 list_my_messages（要 id 就先读信箱，不是读通知）",
      "list_my_messages" in p["note"] and "list_notifications" not in p["note"],
      p["note"])
p = _plan("message_read", {"all": True})
check("message_read 的注记说的是**信**、不是通知（两个 id 空间不许混）",
      "信" in p["note"] and "通知" not in p["note"], p["note"])

check("own 写技能都落在 _OWN_WRITE_SKILLS 里（不是靠减法落到名字通道）",
      _OWN_WRITE_SKILLS == {"favorite_add", "favorite_remove", "notice_read",
                            "message_read"},
      str(sorted(_OWN_WRITE_SKILLS)))
check("own 写技能与名字通道不重叠（两套展开器的判据互斥）",
      not (_OWN_WRITE_SKILLS & _WRITE_NAME_TARGET_SKILLS))
check("注册表里每个写技能都落进三者之一（名字通道 / own 通道 / 文章两件）",
      WRITE_SKILL_NAMES <= (_WRITE_NAME_TARGET_SKILLS | _OWN_WRITE_SKILLS
                            | {"article_status", "article_tags"}),
      str(sorted(WRITE_SKILL_NAMES - (_WRITE_NAME_TARGET_SKILLS | _OWN_WRITE_SKILLS
                                      | {"article_status", "article_tags"}))))
for n in sorted(_OWN_WRITE_SKILLS):
    sk = SKILL_MAP.get(n)
    check(f"own 写技能 {n} 不设 roles（三档角色都看得见：动的是他自己的数据）",
          sk is not None and not sk.roles, str(sk and sk.roles))


# ══════════════════════════════════════════════════════════════════
print("\n⑥ 接线在位（改坏了这几处，上面的功能静默失效）")


def _src(p):
    return (Path(__file__).resolve().parent / p).read_text(encoding="utf-8")


# 父仓那一半（`src/routes/chat.rs` 的 render_exec_row 同名臂）：措辞必须与
# `server.py::_tool_action_text` **逐字一致**（预告帧与落库回执行是同一件事的两处
# 渲染，两处不一样会让主人以为发生了两件事）。agent 仓单独 checkout（CI）时读不到
# 父仓 —— 那时**明说跳过**，不假装通过。
_rust_path = Path(__file__).resolve().parent.parent / "src" / "routes" / "chat.rs"
if _rust_path.exists():
    _rust = _rust_path.read_text(encoding="utf-8")
else:
    _rust = ""
    print("  ⏭ 跳过父仓 Rust 侧断言（src/routes/chat.rs 不在：agent 仓单独 checkout）")


from agent.graph import _CONTENT_TOOLS  # noqa: E402
from agent.skills import _CALLABLE_QUERY_TOOLS, _EXPLICIT_TOOLS  # noqa: E402
from tools.base import _TOOL_REGISTRY  # noqa: E402

_s = _src("server.py")
check("三个读工具都在注册表里",
      all(n in {t.name for t in _TOOL_REGISTRY} for n in READ_TOOLS),
      str(sorted(n for n in READ_TOOLS if n not in {t.name for t in _TOOL_REGISTRY})))
check("scope 声明为 read.own（不是 admin.console——判据是『以谁的 uid 去读』）",
      all(authz.required_scope(n) == authz.SCOPE_READ_OWN for n in READ_TOOLS),
      str({n: authz.required_scope(n) for n in READ_TOOLS}))
check("三档角色都授予 read.own（角色轴在这里没有信息量）",
      all(authz.SCOPE_READ_OWN in authz._ROLE_SCOPES[r]
          for r in (authz.ROLE_USER, authz.ROLE_SECRETARY, authz.ROLE_ADMIN)))
check("匿名（role=None）四处都拿不到：没有身份就没有这份数据",
      all(not authz.check(Principal(uid=0, role=None), n).allowed for n in READ_TOOLS))
check("读工具**不进** CONSENT_SCOPES（读自己的收藏不需要当轮命令）",
      authz.SCOPE_READ_OWN not in authz.CONSENT_SCOPES)
check("三个都在 planner 点名白名单（无参只读，走 PARAMS.tools）",
      all(n in _EXPLICIT_TOOLS and n in _CALLABLE_QUERY_TOOLS for n in READ_TOOLS))
check("三个都在 _CONTENT_TOOLS（否则『你还没有未读通知』这句站内结论没有帧）",
      all(n in _CONTENT_TOOLS for n in READ_TOOLS))
check("三个都有中文动作词（否则过程行显示『执行 list_notifications』）",
      all(f'"{n}":' in _s for n in READ_TOOLS))
check("工具描述里写了『自己』（措辞不许让 planner 读成『全站』）",
      all("自己" in getattr(base, n).description for n in READ_TOOLS))
# 菜单是白名单 × 注册表**生成**的（不手抄）：进了白名单就必然进菜单，且带着
# 工具描述——少了这一条，planner 会看到菜单里没有这三项，于是拿检索去绕。
import agent.graph as _G  # noqa: E402
check("三个都在 planner 菜单里（带描述，`- name():` 形态）",
      all(f"- {n}()：" in _G._QUERY_TOOLS_DESC for n in READ_TOOLS),
      str([n for n in READ_TOOLS if f"- {n}()：" not in _G._QUERY_TOOLS_DESC]))

# ── 写侧接线（20260923 批 7）────────────────────────────────────────
check("三个写工具都在注册表里",
      all(n in {t.name for t in _TOOL_REGISTRY} for n in WRITE_TOOLS),
      str(sorted(n for n in WRITE_TOOLS if n not in {t.name for t in _TOOL_REGISTRY})))
check("scope 声明为 write.own（与后台写的 write.console 是**两个** scope）",
      all(authz.required_scope(n) == authz.SCOPE_WRITE_OWN for n in WRITE_TOOLS),
      str({n: authz.required_scope(n) for n in WRITE_TOOLS}))
check("三档角色都授予 write.own（秘书代博主收藏自己的号也是正当的）",
      all(authz.SCOPE_WRITE_OWN in authz._ROLE_SCOPES[r]
          for r in (authz.ROLE_USER, authz.ROLE_SECRETARY, authz.ROLE_ADMIN)))
check("匿名（role=None）拿不到：没有身份就没有收藏夹",
      all(not authz.check(Principal(uid=0, role=None), n).allowed for n in WRITE_TOOLS))
check("写工具**不进** _HARD_SCOPES（不吃 shadow，与后台写刻意分开）",
      authz.SCOPE_WRITE_OWN not in authz._HARD_SCOPES)
check("写工具**进** CONSENT_SCOPES（写自己的东西也要当轮一条命令）",
      authz.SCOPE_WRITE_OWN in authz.CONSENT_SCOPES
      and all(authz.requires_consent(Principal(uid=7, role="user"), n) for n in WRITE_TOOLS))
check("同意闸对同一句话给出**不同**工具各自的结论（判据按工具名，不是按 scope）",
      authz.consent_granted(Principal(uid=7, role="user"), "add_favorite", "收藏这篇文章")
      and not authz.consent_granted(Principal(uid=7, role="user"),
                                    "read_notifications", "收藏这篇文章"))
check("站内信**读**工具也进 planner 点名白名单（自指提问：我信箱里有什么）",
      "list_my_messages" in _EXPLICIT_TOOLS and "list_my_messages" in _CALLABLE_QUERY_TOOLS
      and "list_my_messages" in _G._QUERY_TOOLS_DESC
      and "list_my_messages" in _CONTENT_TOOLS)
check("站内信**写**工具（read_messages）**不在**任何 planner 白名单（写只走技能模板）",
      "read_messages" not in _EXPLICIT_TOOLS and "read_messages" not in _CALLABLE_QUERY_TOOLS
      and "read_messages" not in _G._QUERY_TOOLS_DESC)
check("read_messages 与 read_notifications 共用一个同意闸族（同一句命令的两种目标）",
      authz.TOOL_SCOPE["read_messages"] == authz.TOOL_SCOPE["read_notifications"]
      == authz.SCOPE_WRITE_OWN
      and authz._OWN_TOOL_FAMILY["read_messages"] == authz._OWN_TOOL_FAMILY["read_notifications"],
      f"{authz.TOOL_SCOPE['read_messages']} / {authz._OWN_TOOL_FAMILY.get('read_messages')}")
check("站内信两件都有中文动作词分支（否则过程行显示『执行 read_messages』）",
      'if name == "read_messages"' in _s and '"标记站内信已读（全部未读）"' in _s
      and '"list_my_messages": "查看站内信"' in _s)
if _rust:
    check("Rust 侧两个臂都在，且措辞与 agent 侧同源",
          '"list_my_messages" => "查看站内信"' in _rust
          and '"标记站内信已读（全部未读）"' in _rust,
          "src/routes/chat.rs 缺臂")
    check("Rust 侧通知/信两族的措辞**不同字**（同字=有一族被复制粘贴漏改）",
          '"标记站内信已读（全部未读）"' in _rust
          and '"标记站内通知已读（全部未读）"' in _rust)
check("两件描述里都写了『自己』，且都点了『与河灯留言是两回事』",
      all("自己" in getattr(base, n).description for n in
          ("list_my_messages", "read_messages"))
      and "河灯留言" in base.list_my_messages.description,
      base.list_my_messages.description[:40])

check("写工具都不在 planner 点名白名单（写只能由技能模板展开）",
      all(n not in _EXPLICIT_TOOLS and n not in _CALLABLE_QUERY_TOOLS
          and n not in _G._QUERY_TOOLS_DESC for n in WRITE_TOOLS))
check("三个写工具都有中文动作词分支（否则过程行显示『执行 add_favorite』）",
      'if name in ("add_favorite", "remove_favorite")' in _s
      and 'if name == "read_notifications"' in _s
      and '"收藏文章" if name == "add_favorite" else "取消收藏文章"' in _s
      and '"标记站内通知已读（全部未读）"' in _s)
if _rust:
    check("过程行动作词与 Rust render_exec_row 的同名臂措辞同源（收藏/取消收藏）",
          '收藏文章 {}' in _rust and '取消收藏文章 {}' in _rust,
          "src/routes/chat.rs 缺臂")
    check("Rust 侧认得 args 里的 Python 形态（列表是 repr 字符串、bool 是 'True'）",
          'fn py_int_list' in _rust
          and 'matches!(arg("all").as_str(), "True" | "true" | "1")' in _rust)
check("写工具描述里也写了『自己』", all("自己" in getattr(base, n).description
                                       for n in WRITE_TOOLS))
check("收藏/取消收藏都进『目标有据』名单（写错文章的 id 是不可回滚的对外改动）",
      {"add_favorite", "remove_favorite"} <= set(_G._ARTICLE_WRITE_TOOLS))
check("收藏两件**不在**弹窗标题工具名单里（普通访客没有读后台清单的权限）",
      not ({"add_favorite", "remove_favorite"} & set(_G._POPUP_TITLE_TOOLS)))
check("收藏列表进了『有据』的帧来源（收藏过的文章 id 也算有据）",
      "list_my_favorites" in _G._TARGET_EVIDENCE_TOOLS)

# ══════════════════════════════════════════════════════════════════
print("\n⑦ 语料与判据（20260923 批 8：这套能力「读不到时会说什么」是判据的一部分）")

_g_src = _src("agent/graph.py")
check("narrator 纪律 20 在位（未登录/读不到 ≠ 空）",
      "（帧里原话「未登录：…」）" in _g_src
      or "**一个字都不许说成" in _g_src)
check("纪律 20 把三种结局分开说（读到了是空 / 未登录 / 读不到）",
      "读到了确实是空" in _g_src and "需要先登录博客" in _g_src
      and "这次没读到，不敢下结论" in _g_src)
check("纪律 20 管到「改没改成功」（只认执行回执，不把未确认生效说成好了）",
      "未确认生效" in _g_src and "只认本轮执行回执" in _g_src)
check("纪律 18 的括注已扩到收藏 / 标记已读（原先只有新建标签、改文章状态）",
      "新建标签、改文章状态、收藏/取消收藏、标记通知已读" in _g_src)
check("纪律 18 收尾写明：这三件写自己的数据**不弹框**、只能按回执说",
      "只能按回执说" in _g_src and "系统**不弹框**" in _g_src)

# golden 三条：写面的 golden 只有"未登录"这一种形态（本机即生产库，带真 uid 的写用例
# 会真改主人的收藏夹 —— 见 test_userdata 头注）。这里锁的是"用例还在 + 它依赖的
# 输入形态判据没变"：authz 的判据一改，用例的**性质**会悄悄从"免弹窗直执行"变成
# "走确认弹窗"，而用例本身不会报错。
_gold = [json.loads(line) for line in
         (_src("eval/golden/basic.jsonl")).splitlines() if line.strip()]
_by_id = {c["id"]: c for c in _gold}
NEW_CASES = {
    "own_favorite_add_not_logged_in": {"require_tool_calls": ["add_favorite"]},
    "own_unread_not_logged_in": {"require_tool_calls_any": ["list_notifications",
                                                            "get_unread_summary"]},
    "own_mark_read_not_logged_in": {"require_tool_calls": ["read_notifications"]},
    # 站内信（20260923 批 8）：同一对形态的另一个物件。断言刻意**点名同族的另一个
    # 工具**（read_messages 而非 read_notifications）：两族 id 空间不同，走错族
    # 会把通知标成已读、信一封没动，而回执看着像成功——这种错只能靠"点名哪个工具"
    # 抓（见两条用例 _note 里的"术语分辨"）。
    "own_messages_not_logged_in": {"require_tool_calls_any": ["list_my_messages",
                                                             "get_unread_summary"]},
    "own_message_read_not_logged_in": {"require_tool_calls": ["read_messages"]},
}
for _cid, _want in NEW_CASES.items():
    _c = _by_id.get(_cid)
    check(f"golden 用例在位：{_cid}", _c is not None)
    if not _c:
        continue
    _g = _c["gold"]
    check(f"  {_cid}：断言了真调过工具（否则「需要登录」这句可以是零帧编的）",
          all(_g.get(k) == v for k, v in _want.items()))
    check(f"  {_cid}：禁 fallback（兜底道歉不算如实说）", _g.get("forbid_fallback") is True)
    check(f"  {_cid}：正文必须指向登录", _g.get("text_any_regex") == ["登录"])
    check(f"  {_cid}：有负断言（不得声称已改/不得说成空）",
          bool(_g.get("text_not_match_regex")))
    check(f"  {_cid}：匿名（无 user_id / 无 role）",
          "user_id" not in _c.get("context", {}) and "role" not in _c.get("context", {}))

check("站内信那两条点名的是**信**这一族的工具（复制粘贴时最容易串成通知族）",
      "list_my_messages" in NEW_CASES["own_messages_not_logged_in"]
      ["require_tool_calls_any"]
      and NEW_CASES["own_message_read_not_logged_in"]["require_tool_calls"]
      == ["read_messages"])

check("输入形态判据仍是「命令式 ⇒ 免弹窗直执行」（改了 authz 判据这条先红）",
      authz.consent_granted(Principal(uid=7, role="user"), "add_favorite", "收藏这篇文章")
      and authz.consent_granted(Principal(uid=7, role="user"),
                                "read_notifications", "把通知都标记成已读"))
check("两条提问形态仍是「提问 ⇒ 不弹窗」（否则用例会落进确认弹窗、测的不是同一条链路）",
      authz.is_question_like("我有哪些未读通知？"))

# ── 原因码：not_found 是**独立**一族（20260923 三轮）──────────────────
# 事故形态：工具把"你要标的第 3 条不存在"报成 unavailable ⇒ checker 用同一个码 ⇒
# 过程行显示「服务不可用」（用户看到的就是这句）。判据两头都要锁：checker 给出
# **不同**的原因码，且 server 的 `_REASON_CN` 里有对应人话（新码漏配中文 = 过程行
# 原样打英文，20260921 之前 `unavailable` 就是这样）。
check("checker：kind=not_found → BLOCK + 独立原因码 target_not_found",
      _G._check_spec("read_notifications", {"ids": [3]}, True, "通知 3 不在…", "notice_read",
                     kind="not_found") == ("BLOCK", "target_not_found"),
      str(_G._check_spec("read_notifications", {"ids": [3]}, True, "…", "notice_read",
                         kind="not_found")))
check("checker：kind=unavailable 仍是它自己的码（两族不许合并）",
      _G._check_spec("read_notifications", {}, True, "…", "notice_read",
                     kind="unavailable") == ("BLOCK", "unavailable"))
_s_src = (Path(__file__).resolve().parent / "server.py").read_text(encoding="utf-8")
check("过程行有中文：target_not_found →「目标不存在」（不是「服务不可用」）",
      '"target_not_found": "目标不存在"' in _s_src and '"unavailable": "服务不可用"' in _s_src,
      str([l for l in _s_src.splitlines() if "target_not_found" in l]))

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
