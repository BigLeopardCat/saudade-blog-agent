# -*- coding: utf-8 -*-
"""报表摘要读**结构**、不读我们自己渲染的中文（20261019 批 G5）。

**这一件为什么存在**：七件报表工具（`tools/admin_read.py` 六件 + `tools/note_admin.py`
的 `list_admin_notes`）与 `read_notifications` 的跨轮实体摘要，此前一律对着**自己刚
渲染出来的中文报表**跑正则抠数字（`agent/entity_digest.py::_TEXT_DIGESTERS`）。那等于
把已经算好的数重新猜一遍：`render_*` 改一句措辞，摘要就**静默变空**——不报错、不留痕，
下一轮"刚才磁盘占用多少"只能重跑工具；而"标好了"与"没标"在跨轮记忆里长得一样。

批 G5 让这些工具把结构一并交出（`ToolResult.data`），摘要改从结构取值。被测四块：

  · ① 两张表**键集合相等**——同一个工具两条通道（有结构走 `_REPORT_DIGESTERS`、只有
    文本回落 `_TEXT_DIGESTERS`）。只改一侧 ⇒ 那条通道静默失效，所以这条要机械判。
  · ② 同一份夹具上**两条通道逐字相同**（真渲染器渲染出来的文本 vs 真结构）。这是
    "换通道不改行为"的正控：少了它，下面 ③ 的"不变"可以靠"两边都空"骗过去。
  · ③ 反控：把渲染文案**整段打乱**（CJK 全换掉）⇒ 文本那一支当场退化，结构那一支
    **一个字都不变**。这一条才是本批要买的东西（今天它会一起变空）。
  · ④ 接线（**行为级**）：真调那七件工具（桩只打在 httpx 边界与 `agent.hostinfo`
    的读口），断言返回的 `ToolResult.data` 非空、且两条通道仍然逐字相同。源码里写了
    `data=` 而没接上线，是这一族最常见的假绿。

用法：.venv/bin/python tests/test_report_digest_structure.py
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.base as base  # noqa: E402
import tools.own_data as own  # noqa: E402
from agent import adminops_tags as _tags  # noqa: E402
from agent import hostinfo as H  # noqa: E402
from agent import reports as R  # noqa: E402
from agent.entity_digest import (  # noqa: E402
    _REPORT_DIGESTERS,
    _TEXT_DIGESTERS,
    receipt_digest,
)
from agent.principal import Principal  # noqa: E402

FAILS: list[str] = []
NOW = datetime(2026, 9, 21, 13, 5)


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ══════════════════════════════════════════════════════════════════
print("\n① 两张表：同一个工具族的**两条通道**，键集合必须相等")

check("正控：两张表都非空（空表会让下面那条变成空过）",
      bool(_REPORT_DIGESTERS) and bool(_TEXT_DIGESTERS),
      f"{len(_REPORT_DIGESTERS)}/{len(_TEXT_DIGESTERS)}")
check("⭐ 键集合相等（只给一个工具加结构通道 ⇒ 当场红）",
      set(_REPORT_DIGESTERS) == set(_TEXT_DIGESTERS),
      f"仅结构={sorted(set(_REPORT_DIGESTERS) - set(_TEXT_DIGESTERS))} "
      f"仅文本={sorted(set(_TEXT_DIGESTERS) - set(_REPORT_DIGESTERS))}")


# ══════════════════════════════════════════════════════════════════
# 夹具：**真结构**（就是喂给渲染器的那些入参）。文本一律由**真渲染器**现渲染出来
# ——手抄一份报表串就等于又造一个真相源，而这一件判的正是"渲染与结构是不是同源"。
print("\n② 七条报表：真渲染 + 真结构 ⇒ 两条通道逐字相同")

MEMINFO = ("MemTotal:        4000000 kB\nMemAvailable:     2000000 kB\n"
           "SwapTotal:       2000000 kB\nSwapFree:        2000000 kB\n")

SERVER = {"cpu_pct": 37.5, "cores": 4, "load": (0.52, 0.48, 0.44),
          "mem": H.mem_summary(H.parse_meminfo(MEMINFO)),
          "disks": [{"path": "/", "total": 100 * 1024 ** 3, "used": 60 * 1024 ** 3,
                     "free": 40 * 1024 ** 3, "pct": 60}],
          "uptime_s": 3 * 86400 + 4 * 3600}
SERVER_DOWN = {"cpu_pct": None, "cores": 4, "load": None, "mem": {}, "disks": [],
               "uptime_s": None}

HEALTH = {"warn": 3, "fail": 1, "first": None, "last": None, "recent": []}
SERVICES = [("saudade-rust", {"ActiveState": "active", "SubState": "running",
                              "NRestarts": 0}),
            ("saudade-agent", {}),
            ("saudade-device", {"ActiveState": "failed", "SubState": "dead",
                                "NRestarts": 7})]
HEALTH_TRACES = {"rounds": 5, "abnormal": 1, "fallback": 0, "denied": 2, "readable": True}
SIZES = [{"path": "rust.log", "size": 3 * 1024 ** 2}]
HEALTH_UNKNOWN = {"services": [("saudade-rust", {})], "health": {},
                  "traces": {"readable": False}, "sizes": []}


def _brow(i, approved, ai, body="内容", who="访客"):
    return {"talkKey": i, "approved": approved, "ai_result": ai, "author": who,
            "content": body, "userId": 100 + i,
            "createTime": f"2026-09-21 12:{i:02d}:00"}


BOARD = [_brow(30, 0, "flag", "求个学习资料", "小明"), _brow(29, 0, "flag", "第二条"),
         _brow(28, 0, None, "AI 没审过的"), _brow(27, 1, "pass", "正常留言"),
         _brow(26, 2, "pass", "AI 放过但被驳回"), _brow(25, 2, "flag", "AI 拦下且驳回"),
         _brow(24, 2, "reject", "AI 驳回且人工维持"), _brow(23, 0, "reject", "还等着"),
         _brow(22, 1, "reject", "人工改判放行")]

USERS = {"generatedAt": "2026-09-21 12:00", "roleCounts": [{"role": "admin", "count": 1}],
         "totalUsers": 42, "totalConversations": 310, "totalMessages": 5120,
         "totalExecutions": 880, "activeUsers7d": 6, "activeUsers30d": 19, "listedUsers": 1,
         "users": [{"id": 1, "name": "博主", "role": "admin", "conversations": 120,
                    "messages": 3000, "lastActiveAt": "2026-09-21 11:30:00"}]}

_NOTE_ROW = {"noteId": 12, "title": "架构笔记", "views": 300, "likes": 20, "favorites": 3}
TRAFFIC = {"generatedAt": "2026-09-21 12:00", "totalViews": 1234, "totalLikes": 88,
           "totalFavorites": 12, "topViewed": [dict(_NOTE_ROW)], "topLiked": [dict(_NOTE_ROW)],
           "topFavorited": [dict(_NOTE_ROW)],
           "daily": [{"date": "2026-09-20", "views": 10, "likes": 1}]}

NOTES = [{"noteKey": 54, "noteTitle": "架构文档", "status": "public", "isTop": 1,
          "noteTags": [], "categoryTitle": "技术"},
         {"noteKey": 53, "noteTitle": "草稿一篇", "status": "draft", "isTop": 0,
          "noteTags": [], "categoryTitle": ""},
         {"noteKey": 52, "noteTitle": "私密一篇", "status": "private", "isTop": 0,
          "noteTags": [], "categoryTitle": ""}]

# (工具名, 渲染出来的文本, 结构)
CASES = [
    ("get_server_status", R.render_server_status(**SERVER, now=NOW), SERVER),
    ("get_service_health",
     R.render_service_health(services=SERVICES, health=HEALTH, traces=HEALTH_TRACES,
                             sizes=SIZES, now=NOW),
     {"services": SERVICES, "health": HEALTH, "traces": HEALTH_TRACES, "sizes": SIZES}),
    ("get_moderation_status", R.render_moderation_status(BOARD, now=NOW),
     {"rows": BOARD, "status": None}),
    ("get_user_stats", R.render_user_stats(USERS, now=NOW), USERS),
    ("get_note_stats", R.render_note_stats(TRAFFIC, now=NOW), TRAFFIC),
    ("list_admin_notes", _tags.render_admin_notes(NOTES, None),
     {"notes": NOTES, "keyword": "", "category": "", "limit": _tags.ADMIN_NOTES_LIMIT}),
    ("read_notifications", "已把 2 条通知标记为已读（现在未读：通知 0 条 / 私信 1）。",
     {"marked": 2, "unread": 0, "unread_messages": 1}),
]

# 正控：文本那一支**确实在工作**（每条都抽出了东西）。少了它，下面的"两条相同"
# 可以靠"两边都返回空串"骗过去——那正是这一族失败时的样子（静默变空）。
_text_out = {n: receipt_digest(n, t) for n, t, _ in CASES}
check("正控：七条文本通道都产出了摘要（空串会让下面的相等变成空过）",
      all(_text_out.values()), str({k: v for k, v in _text_out.items() if not v}))

for _name, _text, _struct in CASES:
    _both = receipt_digest(_name, _text, data=_struct)
    check(f"{_name}：结构通道与文本通道**逐字相同**",
          _both == _text_out[_name] and bool(_both),
          f"结构={_both!r} 文本={_text_out[_name]!r}")


# ══════════════════════════════════════════════════════════════════
print("\n③ 反控：把渲染文案整段打乱 ⇒ 文本那一支退化，结构那一支一个字都不变")


def _scramble(text: str) -> str:
    """渲染出来的中文 → 每个 CJK 字符换成 `×`（ASCII 与数字留着）。

    留着 ASCII 是刻意的：`saudade-rust` / `noteId=` / `CPU` 这些**不是**被改写的
    那一层，改掉它们就分不清"判据在读中文措辞"还是"判据在读整个串"。只动中文，
    正好对上本批要治的那一件事（我们自己渲染的中文措辞）。
    """
    return re.sub(r"[一-鿿]", "×", text)


_degraded = 0
for _name, _text, _struct in CASES:
    _mangled = _scramble(_text)
    _now_text = receipt_digest(_name, _mangled)
    _now_struct = receipt_digest(_name, _mangled, data=_struct)
    check(f"{_name}：文案打乱后文本那一支**真的变了**（下面那条不是白绿）",
          _now_text != _text_out[_name],
          f"{_now_text!r} == {_text_out[_name]!r}")
    _degraded += 1 if _now_text != _text_out[_name] else 0
    check(f"{_name}：⭐ 同一份打乱文本 + 结构 ⇒ 摘要**逐字不变**",
          _now_struct == _text_out[_name],
          f"{_now_struct!r} vs {_text_out[_name]!r}")

check("反控覆盖面：七条**全部**都随文案退化（少一条说明那条的正则不吃中文）",
      _degraded == len(CASES), f"{_degraded}/{len(CASES)}")


# ══════════════════════════════════════════════════════════════════
print("\n④ 接线（行为级）：七件工具真的把结构交出来了")


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.text = ""

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _Fake:
    """按 URL 子串分派的假 httpx 客户端（**只桩边界**，方法名与 httpx 一致）。

    某个路由的值给成列表时按调用次序逐次出（用尽后重复最后一个）——`read_notifications`
    一轮里读两次未读汇总（写前 / 写后），单响应桩表达不了"写成功了、未读数降了"。
    """

    def __init__(self, table):
        self.table = list(table)
        self.calls = []

    def _pick(self, url):
        for key, val in self.table:
            if key in url:
                if isinstance(val, list):
                    return val.pop(0) if len(val) > 1 else val[0]
                return val
        return _Resp(404)

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, headers or {}, json))
        r = self._pick(url)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, headers=None, timeout=None):
        return self.request("GET", url, headers=headers)

    def post(self, url, headers=None, json=None, timeout=None):
        return self.request("POST", url, headers=headers, json=json)


def _ok(data):
    return _Resp(200, {"code": 200, "data": data})


def _cfg(uid, role="admin"):
    return {"configurable": {"user_id": uid,
                             "principal": Principal(uid=uid, role=role)}}


_NOTICES = {"unread": 2, "items": [{"id": 7, "isRead": False}, {"id": 6, "isRead": False}]}

_HTTP = [
    ("/notifications/summary", [_ok({"notifications": 2, "messages": 1}),
                                _ok({"notifications": 0, "messages": 1})]),
    ("/notifications/read", _ok({"updated": 2})),
    ("/notifications", _ok(_NOTICES)),
    ("/api/protect/board", _ok(BOARD)),
    ("/api/protected/stats/users", _ok(USERS)),
    ("/api/protected/stats/notes", _ok(TRAFFIC)),
    ("/api/protected/notes/list", _ok(NOTES)),
    ("/api/protected/tags", _ok([])),
]

_HOST = {"cpu_percent_over": lambda *a, **k: 37.5,
         "read_meminfo": lambda: MEMINFO,
         "read_loadavg": lambda: "0.52 0.48 0.44 1/234 5678",
         "disk_rows": lambda *a, **k: SERVER["disks"],
         "read_uptime": lambda: SERVER["uptime_s"],
         "SERVICES": ("saudade-rust", "saudade-agent", "saudade-device"),
         "service_show": lambda unit: dict(SERVICES)[unit],
         "parse_health_log": lambda *a, **k: HEALTH,
         "trace_stats": lambda *a, **k: HEALTH_TRACES,
         "log_sizes": lambda *a, **k: SIZES}

INVOCATIONS = [
    ("get_server_status", base.get_server_status, {}),
    ("get_service_health", base.get_service_health, {}),
    ("get_moderation_status", base.get_moderation_status, {}),
    ("get_user_stats", base.get_user_stats, {}),
    ("get_note_stats", base.get_note_stats, {}),
    ("list_admin_notes", base.list_admin_notes, {}),
    ("read_notifications", own.read_notifications, {"all": True}),
]

_saved_client = base._client
_saved_host = {k: getattr(H, k, None) for k in _HOST}
base._client = _Fake(_HTTP)
for _k, _v in _HOST.items():
    setattr(H, _k, _v)
try:
    for _name, _tool, _args in INVOCATIONS:
        _out = _tool.invoke(_args, config=_cfg(7))
        _d = getattr(_out, "data", None)
        check(f"{_name}：真调用返回的结构非空（`ToolResult.data` 接上线了）",
              _d is not None, f"{type(_out).__name__} data={_d!r}"[:90])
        check(f"  {_name}：端到端两条通道仍逐字相同",
              receipt_digest(_name, str(_out), data=_d) == receipt_digest(_name, str(_out)),
              f"{receipt_digest(_name, str(_out), data=_d)!r} vs "
              f"{receipt_digest(_name, str(_out))!r}")
finally:
    base._client = _saved_client
    for _k, _v in _saved_host.items():
        if _v is None:
            delattr(H, _k)
        else:
            setattr(H, _k, _v)


# ══════════════════════════════════════════════════════════════════
print("\n⑤ 结构缺失/形状不符 ⇒ 回落文本那一支（不猜、不炸）")

for _name, _text, _struct in CASES:
    check(f"{_name}：data=None ⇒ 与今天的文本通道一致（老调用点一个字都不用改）",
          receipt_digest(_name, _text, data=None) == _text_out[_name])
check("形状不符（喂一个字符串）不炸，回落文本那一支",
      receipt_digest("get_user_stats", CASES[3][1], data="不是结构") == _text_out["get_user_stats"])
check("全项读不到的服务器报表 → 仍是空摘要（不编）",
      receipt_digest("get_server_status",
                     R.render_server_status(**SERVER_DOWN, now=NOW), data=SERVER_DOWN) == "")
check("服务读不到的报表 → 摘要照实标『读不到』（结构通道也是）",
      receipt_digest("get_service_health",
                     R.render_service_health(services=HEALTH_UNKNOWN["services"],
                                             health={}, traces={"readable": False},
                                             sizes=[], now=NOW),
                     data=HEALTH_UNKNOWN) == "服务健康: 服务 rust=读不到")

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
