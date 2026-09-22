# -*- coding: utf-8 -*-
"""用户自己的数据工具单测（纯函数 / 假 httpx，零网络零 LLM，秒级）。

被测两半：
  · **读**（20260923 批 6）：`list_my_favorites` / `get_unread_summary` /
    `list_notifications`，scope=`read.own`，走 `_own_get`；
  · **写**（20260923 批 7）：`add_favorite` / `remove_favorite` /
    `read_notifications`，scope=`write.own`。

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


READ_TOOLS = ["list_my_favorites", "get_unread_summary", "list_notifications"]

# 三个端点的返回样本（形态抄自 src/routes/profile.rs 的 FavoriteDto /
# UnreadDto / NotificationListDto，字段名一个不差——它们是 Python↔Rust 的契约）
FAVS = [{"noteId": 12, "title": "留言板怎么用", "status": "published",
         "createdAt": "2026-09-20 11:02:00"},
        {"noteId": 19, "title": "Saudade Blog AI Agent（泠月喵）架构文档", "status": "published",
         "createdAt": "2026-09-18 09:30:00"}]
UNREAD = {"notifications": 3, "messages": 1, "total": 4}
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
         ("list_notifications", NOTICES)]
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
check("通知 → 总条数 + 未读条数 + 标题（未读标出来）",
      receipt_digest("list_notifications", str(NOTICES))
      == "通知 3 条（未读 2）: 国庆维护公告（未读）/你的留言已通过审核（未读）/欢迎来到 Saudade",
      receipt_digest("list_notifications", str(NOTICES)))
check("通知**正文不进摘要**（挤进来只会把标题挤掉——要正文就再调一次工具）",
      "10 月 1 日" not in receipt_digest("list_notifications", str(NOTICES)))
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
print("\n④ 接线在位（改坏了这几处，上面的功能静默失效）")


def _src(p):
    return (Path(__file__).resolve().parent / p).read_text(encoding="utf-8")


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

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
