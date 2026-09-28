# -*- coding: utf-8 -*-
"""动作措辞的唯一实现（`agent/action_text.py`）与"两边真的比过"的那条判据。

**为什么值得单开一个套件**：这批改动之前，同一件事有**两份实现**——`server.py` 的
过程行渲染与 Rust `src/routes/chat.rs::render_exec_row`（落 `execution_log.detail` 的
跨轮执行记忆）。两份表靠注释互相提醒，没有任何测试能发现它们分了叉；20260928 逐行
对照实测 54 条取样里只有 31 条逐字相同。本套件锁的就是"它们不许再分叉"这件事：

① **过程行（preview）**：字与改动前逐条相同（唯一一处**有意**修正是"落盘后的 id 列表
   也要认得出"——完成帧此前会说成"标记站内通知已读"、不带对象，与预告帧不一致）；
② **台账行（full）**：逐条抄自父仓 `chat.rs` 的 `#[cfg(test)]` 期望值——那份期望是
   Rust 侧多年攒下来的契约（跨轮执行记忆里出现过的字），Python 接管后必须一一对上，
   抄在这里才可能在**改 Python 时立刻知道哪一条的字会变**（三处**有意**偏离都写在
   断言旁，理由见 `agent/action_text.py` 的模块头注）；
③ **通用不变量**：任何工具的空参行都不许退化成空书名号/空冒号——"Rust 读顶层 meta、
   Python 只有实参"那类分叉的症状就是它（`新建一级标签「」`）；
④ **接线**：Python 只在**有真臂**时才写 `rcpt["action"]`（没臂的留给 Rust 的老表，
   于是上线前后存量行的字一个字节都不变）；Rust 那半必须**优先读**它——不读的话
   这个字段就是个摆设，而且**看不出来**（两边都绿、线上字不变）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _parent_repo  # noqa: E402
from agent.action_text import receipt_action, tool_action_text  # noqa: E402
from tools.base import _TOOL_REGISTRY  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _pre(name, args=None) -> str:
    return tool_action_text(name, args or {}, preview=True)


def _full(name, args=None, meta=None) -> str:
    return tool_action_text(name, args or {}, meta or {}, preview=False)


# ══════════════════════════════════════════════════════════════════
print("\n① 过程行（preview）：字与改动前逐条相同")

# 这一组是**真实生产形状**（trace `execute.call` 的实参 + 技能模板展开的 spec）：
# 331 条去重组合在本机跑新旧两版实现逐条对照、零字差，这里挑代表钉住。
_PREVIEW = [
    ("新建一级标签「大笨狗」", "create_tag", {"title": "大笨狗"}),
    ("新建二级标签「Rust」（父标签 id 7）", "create_tag",
     {"title": "Rust", "parent_id": 7}),
    ("新建标签", "create_tag", {}),
    ("页面跳转「物联网平台」", "navigate_to", {"path": "/device-console/"}),
    ("页面跳转「/iot」", "navigate_to", {"path": "/iot"}),
    ("开启夜间模式", "toggle_dark_mode", {"mode": "on"}),
    ("关闭夜间模式", "toggle_dark_mode", {"mode": "off"}),
    ("开启樱花特效", "toggle_effect", {"effect": "sakura", "action": "on"}),
    ("关闭大雨特效", "toggle_effect", {"effect": "rain", "action": "off"}),
    ("站内检索「架构」", "rag_search", {"query": "架构"}),
    ("检索文章「架构」", "search_notes", {"keyword": "架构"}),
    ("读取文章 19", "get_article_detail", {"article_id": 19}),
    ("读取留言 100", "get_article_detail", {"doc_type": "board", "article_id": 100}),
    ("读取文章（上一步检索结果的第 1 条）", "get_article_detail",
     {"article_id": "$search_notes[0].noteId"}),
    ("查看审核状况（只看等人复批的）", "get_moderation_status", {"status": "pending"}),
    ("修改文章 12：私密、置顶", "set_article_status",
     {"article_id": 12, "status": "private", "is_top": 1}),
    ("修改文章 12：取消置顶", "set_article_status",
     {"article_id": 12, "is_top": 0}),
    ("修改文章 12 的标签：清空全部标签", "set_article_tags",
     {"article_id": 12, "replace": []}),
    ("修改文章 12 的标签：加上 「Rust」", "set_article_tags",
     {"article_id": 12, "add": ["Rust"]}),
    ("删除标签「Rust」", "delete_tag", {"name": "Rust"}),
    ("冻结账号「guest5」", "freeze_account", {"name": "guest5"}),
    ("给账号「guest5」发通知", "send_user_notice", {"name": "guest5"}),
    ("收藏文章 12", "add_favorite", {"article_id": 12}),
    ("查看我的收藏", "list_my_favorites", {}),
    ("屏幕显示「交房租」", "device_oled_display", {"text": "交房租"}),
    ("查看天气「杭州」", "get_weather", {"location": "杭州"}),
    ("添加待办「交房租」（2026-10-01）", "create_dashboard_todo",
     {"text": "交房租", "date": "2026-10-01"}),
    ("把待办「交房租」勾成完成", "complete_dashboard_todo", {"text": "交房租"}),
    ("执行 get_chat_history", "get_chat_history", {"n": 3}),
]
for want, name, args in _PREVIEW:
    got = _pre(name, args)
    check(f"过程行 {name} {args} → {want}", got == want, got)

# **有意修正**（唯一的 preview 档字变）：完成帧的实参是**落盘后的字符串**
# （`rcpt["args"]`，一律 `str(v)`）⇒ id 列表到那里是 Python repr `"[7, 8, 9]"`。
# 旧实现只认原始 list，于是同一次执行在预告里说"标了 7、8、9"、完成帧里说
# "标记站内通知已读"（**没有对象**）——两帧必须给主人看同一句话。
check("完成帧（落盘后的 repr）也认得 id 列表 ⇒ 与预告帧同一句话",
      _pre("read_notifications", {"all": "False", "ids": "[7, 8, 9]"})
      == "标记站内通知已读（7、8、9）",
      _pre("read_notifications", {"all": "False", "ids": "[7, 8, 9]"}))
check("  ★ 认不出的串仍然不猜编号（宁可少说一条）",
      _pre("read_notifications", {"all": "False", "ids": "['a', -3, '']"})
      == "标记站内通知已读",
      _pre("read_notifications", {"all": "False", "ids": "['a', -3, '']"}))
# 两处截断**都有省略号**（待办/屏文是主人写进去的正文，裸切一刀会被读成
# 「系统只记了这半句」）；长度不是 24 就是 20，逐条与改动前对照过、原样保留。
_TODO_LONG = "很长的一条待办正文" * 20
check("  ★ 正文类截断带省略号（待办 24 字 + …）",
      _pre("create_dashboard_todo", {"text": _TODO_LONG})
      == f"添加待办「{_TODO_LONG[:24]}…」",
      _pre("create_dashboard_todo", {"text": _TODO_LONG}))

# ══════════════════════════════════════════════════════════════════
print("\n② 执行台账行（full）：逐条对齐父仓 chat.rs 的期望")

# **这批断言抄自 `src/routes/chat.rs` 的 `#[cfg(test)]`**（函数名见前缀注释）：
# 那份期望就是跨轮执行记忆里出现过的字（下一轮 narrator 照着念的原料），
# Python 接管渲染后必须一一对上。**父仓改了那边的那条测试，这里就该红**——
# 两边同时改才算"收敛完成"，只改一边正是这批要治的病。
_LEDGER = [
    # exec_row_create_tag_reads_toplevel_name / _reuse_level2
    ("新建一级标签「大笨狗」", "create_tag", {"title": "大笨狗"},
     {"op": "tag_create", "level": "1", "tag_name": "大笨狗", "tag_id": "15"}),
    ("复用已有二级标签「泠月喵」", "create_tag", {"title": "泠月喵"},
     {"op": "tag_reuse", "level": "2", "tag_name": "泠月喵"}),
    # exec_row_tag_update_and_delete
    ("修改标签「编程 / Asyncio」：改名叫「异步」并移到「编程」下", "update_tag",
     {"name": "Asyncio"},
     {"op": "update_tag", "tag_name": "编程 / Asyncio", "level": "2",
      "change": "改名叫「异步」并移到「编程」下"}),
    ("删除一级标签「小猫咪」：连同 3 篇文章上的引用一起摘除", "delete_tag",
     {"name": "小猫咪"},
     {"op": "delete_tag", "tag_name": "小猫咪", "level": "1",
      "change": "连同 3 篇文章上的引用一起摘除"}),
    ("删除二级标签「X」", "delete_tag", {},
     {"op": "delete_tag", "tag_name": "X", "level": "2"}),
    # exec_row_category_ops
    ("新建分类「随笔」", "create_category", {"categoryTitle": "随笔"},
     {"op": "create_category", "category_name": "随笔"}),
    ("修改分类「随笔」：改名为「杂记」", "update_category", {},
     {"op": "update_category", "category_name": "随笔", "change": "改名为「杂记」"}),
    ("删除分类「随笔」：它有 4 篇文章，会变成没有分类", "delete_category", {},
     {"op": "delete_category", "category_name": "随笔",
      "change": "它有 4 篇文章，会变成没有分类"}),
    # exec_row_announcement_ops
    ("发布公告「维护通知」", "create_announcement",
     {"title": "维护通知", "content": "今晚 23 点维护"},
     {"op": "announcement_create", "announcement_id": "7",
      "announcement_title": "维护通知"}),
    ("修改公告「维护改期」：改名（原「维护通知」）", "update_announcement",
     {"title": "维护通知"},
     {"op": "announcement_update", "announcement_title": "维护改期",
      "change": "改名（原「维护通知」）"}),
    ("修改公告「维护通知」：正文已更新", "update_announcement", {"title": "维护通知"},
     {"op": "announcement_update", "announcement_title": "维护通知",
      "change": "正文已更新"}),
    ("修改公告「维护通知」", "update_announcement", {},
     {"op": "announcement_update", "announcement_title": "维护通知"}),
    ("删除公告「维护通知」", "delete_announcement", {},
     {"op": "announcement_delete", "announcement_title": "维护通知"}),
    # exec_row_board_moderation_ops
    ("人工复核留言 #12（路人甲 的留言）：待审 → 驳回", "audit_board_comment",
     {"quote": "今天天气真好呀", "verdict": "reject"},
     {"op": "board_audit", "board_id": "12", "board_author": "路人甲",
      "change": "待审 → 驳回"}),
    ("人工复核留言 #12（路人甲 的留言）", "audit_board_comment", {},
     {"op": "board_audit", "board_id": "12", "board_author": "路人甲"}),
    ("人工复核留言 #12：待审 → 通过", "audit_board_comment", {},
     {"op": "board_audit", "board_id": "12", "change": "待审 → 通过"}),
    ("删除留言 #12（路人甲 的留言）", "delete_board_comment", {},
     {"op": "board_delete", "board_id": "12", "board_author": "路人甲",
      "change": "已删除"}),
    # exec_row_userdata_reads_and_writes
    ("收藏文章 12", "add_favorite", {"article_id": "12"}, {}),
    ("取消收藏文章 12", "remove_favorite", {"article_id": "12"}, {}),
    ("标记站内通知已读（7、8、9）", "read_notifications",
     {"all": "False", "ids": "[7, 8, 9]"}, {}),
    ("标记站内通知已读（7、8、9 等 5 条）", "read_notifications",
     {"all": "False", "ids": "[7, 8, 9, 10, 11]"}, {}),
    ("标记站内通知已读", "read_notifications",
     {"all": "False", "ids": "['a', -3, '']"}, {}),
    ("标记站内信已读（3）", "read_messages", {"all": "False", "ids": "[3]"}, {}),
    ("标记站内信已读", "read_messages", {"all": "False", "ids": "[]"}, {}),
    # 工具短路那次（`noop` + `change`）：动作词整个不出现、对象留着
    ("文章 12 本来已收藏（未改动）", "add_favorite", {"article_id": "12"},
     {"change": "本来已收藏"}),
    ("文章 12 本来就没收藏（未改动）", "remove_favorite", {"article_id": "12"},
     {"change": "本来就没收藏"}),
    ("站内通知本来就读过（未改动）", "read_notifications",
     {"all": "False", "ids": "[7]"}, {"change": "本来就读过"}),
    ("站内信本来就没有未读的（未改动）", "read_messages", {"all": "True"},
     {"change": "本来就没有未读的"}),
    ("收藏文章 12", "add_favorite", {"article_id": "12"}, {"change": ""}),
    # 冻结族 / 发通知（父仓没有对应用例，按它的同名臂逐字对齐）
    ("冻结账号「guest5」", "freeze_account", {"name": "guest5"},
     {"op": "account_freeze", "account_name": "guest5"}),
    ("解冻账号「guest5」：状态本来就是正常，本次未发生变更", "unfreeze_account",
     {"name": "guest5"},
     {"op": "account_unfreeze", "account_name": "guest5",
      "change": "状态本来就是正常，本次未发生变更"}),
    ("给账号「guest5」发通知", "send_user_notice",
     {"name": "guest5", "content": "正文" * 100},
     {"op": "notice_send", "account_name": "guest5"}),
    # 文章状态 / 标签（父仓无对应用例；Rust 那两臂读顶层 before/after）
    ("修改文章 12：公开 → 私密", "set_article_status", {"article_id": 12},
     {"op": "set_status", "article_id": "12", "before": "公开", "after": "私密"}),
    ("修改文章 12 的标签：编程、Rust → 编程", "set_article_tags", {"article_id": 12},
     {"op": "set_tags", "article_id": "12", "before": "编程、Rust", "after": "编程"}),
    # 读取带《标题》（跨轮指代锚点，20260912）
    ("读取文章 19《架构随笔》", "get_article_detail", {"article_id": 19},
     {"title": "架构随笔"}),
    ("读取留言 100", "get_article_detail",
     {"doc_type": "board", "article_id": 100}, {"title": ""}),
]
for want, name, args, meta in _LEDGER:
    got = _full(name, args, meta)
    check(f"台账行 {name} → {want}", got == want, got)

# ── 三处**有意**偏离父仓那张表（理由在 `agent/action_text.py` 头注，都是"同一件事
#    两套字里留一套"与"空书名号一律不给"这两条）────────────────────────────
check("★ 有意：名字缺失时**不给空书名号**（父仓那张表渲染「新建一级标签「」」）",
      _full("create_tag", {"title": "X"}, {"op": "tag_create", "level": "1"})
      == "新建标签",
      _full("create_tag", {"title": "X"}, {"op": "tag_create", "level": "1"}))
check("★ 有意：站内信的量词是「封」（父仓那张表的镜像臂写的是「条」）",
      _full("read_messages", {"all": "False", "ids": "[3, 4, 5, 6]"}, {})
      == "标记站内信已读（3、4、5 等 4 封）",
      _full("read_messages", {"all": "False", "ids": "[3, 4, 5, 6]"}, {}))
check("★ 有意：检索类在台账里也写「检索文章」（父仓那张表写「搜索」）",
      _full("search_notes", {"keyword": "架构"}, {}) == "检索文章「架构」",
      _full("search_notes", {"keyword": "架构"}, {}))
check("★ 有意：文章标签那句台账行与过程行同字（父仓那张表写「修改文章 12 标签：」，"
      "少一个「的」）——同一件事在主人看的过程行与跨轮记忆里长得一样才算收敛",
      _full("set_article_tags", {"article_id": 12},
            {"article_id": "12", "before": "编程", "after": "编程、Rust"})
      == "修改文章 12 的标签：编程 → 编程、Rust",
      _full("set_article_tags", {"article_id": 12},
            {"article_id": "12", "before": "编程", "after": "编程、Rust"}))
check("★ 有意：before/after 都缺时**不给空冒号**（父仓那张老表无条件拼接 ⇒ 出「修改文章 ：」）",
      _full("set_article_status", {"article_id": 12}, {"article_id": "12"})
      == "修改文章 12" and _full("set_article_tags", {}, {}) == "修改文章标签",
      _full("set_article_status", {"article_id": 12}, {"article_id": "12"}))

# ── 两档的分工：差别只有"截断"与"meta 派生细节"两条 ─────────────────────
check("两档同字（没有 meta 可加的臂）：跳转/夜间/特效/检索四件逐字相同",
      all(_pre(n, a) == _full(n, a) for n, a in
          (("navigate_to", {"path": "/device-console/"}),
           ("toggle_dark_mode", {"mode": "on"}),
           ("toggle_effect", {"effect": "sakura", "action": "on"}),
           ("search_notes", {"keyword": "架构"}))))
_LONG = "很长的一段屏文" * 10
check("两档的差别之一：长正文（台账**不截**——它要进跨轮记忆，截了下一轮就取不到值）",
      _full("device_oled_display", {"text": _LONG}, {}).count("很长") == 10,
      _full("device_oled_display", {"text": _LONG}, {}))
check("  ★ 过程行仍截（屏文 24 字；这一臂改动前就不带省略号，原样保留不动它）",
      _pre("device_oled_display", {"text": _LONG}) == f"屏幕显示「{_LONG[:24]}」",
      _pre("device_oled_display", {"text": _LONG}))
check("两档的差别之二：meta 派生细节（《标题》只在台账行）",
      _full("get_article_detail", {"article_id": 19}, {"title": "架构随笔"})
      == "读取文章 19《架构随笔》" and _pre("get_article_detail", {"article_id": 19})
      == "读取文章 19")

# ══════════════════════════════════════════════════════════════════
print("\n③ 通用不变量：空参行不许退化成空书名号/空冒号（两档都要）")

_NAMES = sorted(t.name for t in _TOOL_REGISTRY)
check(f"注册表里的工具都取到了（{len(_NAMES)} 件）", len(_NAMES) > 40, str(len(_NAMES)))
_bad: list[str] = []
for _n in _NAMES:
    for _tag, _txt in (("preview", _pre(_n)), ("full", _full(_n))):
        if "「」" in _txt or _txt.endswith("：") or "  " in _txt:
            _bad.append(f"{_tag}:{_n} → {_txt!r}")
check("★ 54 件工具 × 两档：没有空「」、没有空冒号、没有双空格", not _bad, "; ".join(_bad))

# 无臂工具：两档都必须是**兜底**（preview）/ **空串**（full）。空串是本批的收敛策略
# ——没臂的工具不写 `rcpt["action"]`，Rust 那边的老表照旧渲染（存量行的字一个字节不变）。
_DEAD = {"get_chat_history", "search_knowledge_base"}   # tools/base.py 里的两件死工具
_unarmed = {n for n in _NAMES if receipt_action(n, {}, {}) == ""}
check("★ 无臂集合恰好是那两件死工具（其余 52 件都已收敛到 Python 这一侧）",
      _unarmed == _DEAD, str(sorted(_unarmed ^ _DEAD)))
check("无臂工具的 preview 档是显式的兜底行（不是空串——过程行总得有字）",
      all(_pre(n) == f"执行 {n}" for n in _DEAD))
check("有臂工具的 full 档非空（空串=不写 action=悄悄退回老表，那条路只留给无臂的）",
      all(receipt_action(n, {}, {}) for n in _NAMES if n not in _DEAD))

# ══════════════════════════════════════════════════════════════════
print("\n④ 接线锁：Python 只对有臂的写 action；Rust 必须优先读它")

_g = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
check("graph.py 的 execute_node 写了 `rcpt[\"action\"]`",
      'rcpt["action"]' in _g)
check("  且只在**有臂**时才写（无臂不写 ⇒ 那份回执仍由 Rust 老表渲染）",
      re.search(r'act\s*=\s*\w*action_text\.receipt_action\(', _g) is not None
      and 'if act:\n                rcpt["action"] = act' in _g)
check("  且写在回执行 append 之前（写在后面＝这一行压根没落库）",
      'rcpt["action"] = act' in _g
      and _g.index('rcpt["action"] = act') < _g.index("receipts.append(rcpt)"))

_rs = _parent_repo.read(
    "src/routes/chat.rs",
    why="进程行/台账行的渲染**已经搬到 Python**（`agent/action_text.py`）：Rust 那半若"
        "不读 `row[\"action\"]`，这个字段就是个摆设——两侧测试都绿，而线上一个字都没变")
if _rs is not None:
    def _fn(src: str, name: str) -> str:
        """取顶格函数的函数体（到下一个顶格 `}` 为止）。取不到返回空串，
        让断言报 ❌ 而不是抛栈——**"改了名"和"删了"都该是红**，不该是崩。"""
        i = src.find(f"fn {name}(")
        if i < 0:
            return ""
        body = src[i:]
        j = body.find("\n}\n")
        return body[:j] if j > 0 else body

    _body = _fn(_rs, "render_exec_row")
    check("Rust 有 render_exec_row（渲染入口）", bool(_body))
    check("Rust render_exec_row 读了回执顶层的 action", 'row["action"]' in _body)
    check("  且没有把 action 忘了写成 `args[\"action\"]`（tools 的实参里没有这个键）",
          'args["action"]' not in _body)
    check("★ 活路径上**不再有按工具名的分支**（整张老表搬进 legacy_exec_row）",
          '"device_oled_display" =>' not in _body and "legacy_exec_row(row)" in _body,
          _body[:0])
    _legacy = _fn(_rs, "legacy_exec_row")
    check("  老表**仍在**（存量行 + 无臂工具的回落路径，删了会让历史行渲染成空）",
          '"create_tag" =>' in _legacy and '"device_oled_display" =>' in _legacy)
    check("  排版三件仍留在 Rust 侧（方括号归一 / 实体摘要拼接 / 列宽截断）",
          'replace(\'[\', "「")' in _body and 'row["digest"]' in _body
          and ".take(120)" in _body and ".take(300)" in _body)

print(f"\n{'全部通过' if not FAILS else f'失败 {len(FAILS)} 项'}")
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
