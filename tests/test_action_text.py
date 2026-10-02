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
   抄在这里才可能在**改 Python 时立刻知道哪一条的字会变**（五处**有意**偏离都写在
   断言旁，理由见 `agent/action_text.py` 的模块头注）；抄归抄，**手抄的东西不会因为
   "那边改了"而变红**——真正把两边钉在一起的是 ⑤（从父仓测试源码现取期望值逐条对账）；
③ **通用不变量**：任何工具的空参行都不许退化成空书名号/空冒号——"Rust 读顶层 meta、
   Python 只有实参"那类分叉的症状就是它（`新建一级标签「」`）；
④ **接线**：Python 只在**有真臂**时才写 `rcpt["action"]`（没臂的留给 Rust 的老表，
   于是上线前后存量行的字一个字节都不变）；Rust 那半必须**优先读**它——不读的话
   这个字段就是个摆设，而且**看不出来**（两边都绿、线上字不变）。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _parent_repo  # noqa: E402
from agent import skills as S  # noqa: E402  （③ 末尾那条：死工具的可达性只认这两条通道）
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
# Python 接管渲染后必须一一对上。**抄件不会自己变红**——把"父仓改了那边就该红"
# 这句话变成判据的是 ⑤：它不读这份手抄表，而是现从父仓测试源码里取期望值对账。
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

# ── 五处**有意**偏离父仓那张表（理由在 `agent/action_text.py` 头注，都是"同一件事
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

# ── 「死工具」三个字是有判据的（20260928）──────────────────────────────────
# 上面把 `_DEAD` 当负例用，那就得先证明它真是死的：注册表之外的**每一条**能让 planner
# 点到名的通道（点名白名单 / 技能模板）都不许出现它们。这条锁的用处是"防止标签腐烂"——
# 谁哪天把这两件接回去，断言立刻红，逼他回来改标签、改负例、想清楚 gate 的词汇怎么办。
_dead_reachable = []
for _n in sorted(_DEAD):
    if _n in S._EXPLICIT_TOOLS or _n in S._CALLABLE_QUERY_TOOLS:
        _dead_reachable.append(f"{_n}:点名白名单")
    if any(_n == _t for _s in S.SKILLS for _t, _ in (_s.plan or ())):
        _dead_reachable.append(f"{_n}:技能模板")
check("★ 两件死工具在哪条点名/模板通道里都够不到（够不到 ⇒ 「死」字成立）",
      not _dead_reachable, "、".join(_dead_reachable))
check("★ ……但它们**仍留在注册表里**：gate 的具名工具声称核对词汇是从注册表派生的，"
      "删了这两行，模型声称「我调用了 get_chat_history 查了记录」就再没有判据可对",
      all(n in {t.name for t in _TOOL_REGISTRY} for n in _DEAD))

# ══════════════════════════════════════════════════════════════════
print("\n④ 接线锁：Python 只对有臂的写 action；Rust 必须优先读它，且老表只减不增")

_g = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
check("graph.py 的 execute_node 写了 `rcpt[\"action\"]`",
      'rcpt["action"]' in _g)
check("  且只在**有臂**时才写（无臂不写 ⇒ 那份回执仍由 Rust 老表渲染）",
      re.search(r'act\s*=\s*\w*action_text\.receipt_action\(', _g) is not None
      and 'if act:\n                rcpt["action"] = act' in _g)
check("  且写在回执行 append 之前（写在后面＝这一行压根没落库）",
      'rcpt["action"] = act' in _g
      and _g.index('rcpt["action"] = act') < _g.index("receipts.append(rcpt)"))

# ── 老表冻结（20260929）：Rust 那侧的措辞**只减不增** ─────────────────────────
# 老表一条臂的活路径只有两条：agent 回滚到 20260928 之前送进来的回执行、以及 Python
# 侧至今没有臂的工具（只有两件死工具）。**新工具两条都不满足**——旧 agent 里根本没有
# 这件工具，而新工具在 Python 侧一定有臂（③ 的 `_unarmed == 两件死工具` 就是这条）。
# 所以给新工具加一条 Rust 臂 = **永远走不到的死代码**，而它**看起来**像"两边都写了一
# 遍、互为对账锚点"——20260929 批 G 正是照着这个错觉给 `reschedule_dashboard_todo`
# 加过一臂（已撤：真正被钉住的是措辞本身，那在 `test_todo_schedule.py` ⑭ 里）。
# 这段把它变成机器判据：注册表里没有臂的工具**恰好**是下面这份声明 ⇒ "新工具进注册表"
# 会在这里现形，逼作者当场回答"这件的措辞归哪一侧"（答案永远是 Python 那一侧）。
_NO_LEGACY_ARM = _DEAD | {
    "reschedule_dashboard_todo",   # 20260929 批 G：冻结之后新增的第一件
    "approve_quota_request", "reject_quota_request", "reset_user_quota",
    "list_quota_requests",         # 额度四件（20260926）：从来没进过老表
    "get_note_stats",              # 20260930 文章流量报表：冻结之后新增，动作词只在 Python 侧
    "list_admin_board",            # 20261001 后台留言名册：同上（读 GET /api/protect/board）
    "get_note_periods",            # 20261001 文章分期报表：同上（读 stats/notes/periods）
    "set_account_role",            # 20261002 批 J 变更账号身份：冻结之后新增，动作词只在 Python 侧
}

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
    check("  老表**仍在**（回执缺 `action` 时才走它——那条路只留给回滚与两件无臂死工具；"
          "已落库的行不走它，`execution_log.detail` 存的就是渲染后的字）",
          '"create_tag" =>' in _legacy and '"device_oled_display" =>' in _legacy)

    # 臂的取值解析要认**或臂**（`"a" | "b" =>`）——只认单名会漏掉一半，而漏掉的那些
    # 恰好会被读成"没有臂"⇒ 有人给它们加一条单名臂时判据反而不响。
    _legacy_tools: set[str] = set()
    for _m in re.finditer(r'((?:"[a-z_0-9]+"\s*\|\s*)*"[a-z_0-9]+")\s*=>', _legacy):
        _legacy_tools |= set(re.findall(r'"([a-z_0-9]+)"', _m.group(1)))
    # 臂里还有嵌套 match 的取值（`doc_type` 的 board/talk/announcement），不是工具名。
    _legacy_tools &= set(_NAMES)
    _no_arm = set(_NAMES) - _legacy_tools
    check("★ 老表**只减不增**：注册表里没有臂的工具恰好是那份冻结声明"
          "（新工具的动作词只加 Python 那侧——加臂是死代码，且让对账显得比实际更严）",
          _no_arm == _NO_LEGACY_ARM,
          f"声明说没有却有了臂={sorted(_NO_LEGACY_ARM - _no_arm)}；"
          f"没有臂却不在声明里={sorted(_no_arm - _NO_LEGACY_ARM)}")
    check("  排版三件仍留在 Rust 侧（方括号归一 / 实体摘要拼接 / 列宽截断）",
          'replace(\'[\', "「")' in _body and 'row["digest"]' in _body
          and ".take(120)" in _body and ".take(300)" in _body)

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 两边真的比过：现从父仓测试源码取期望值，逐条与 Python 对账")

# ② 那份字表是**手抄**的，而手抄件不会因为"那边改了"而变红。这一节把 ② 的声明做成
# 判据：直接从 `src/routes/chat.rs` 的 `#[cfg(test)]` 里解析出
# `let row = json!{…}; assert_eq!(render_exec_row(&row), "…")` 的成对值，用同一份输入
# 调 Python 的 `receipt_action`，再套上 Rust 只**排版**的那两件（方括号归一 + 列宽
# 截断，见 ④ 的清单），与父仓期望逐字比。
#
# **为什么这是"真的比较两边"**：两份措辞此前是两份互不相干的实现，靠注释互相提醒；
# 光有"两边各自都有测试"抓不到分叉（各自都对自己那套字自洽）。这一节是唯一一处
# **一份输入走两侧、结果直接相等**的地方。
_RUST_PAIR_RE = re.compile(
    r'let (\w+) = json!\(\s*\{'
    r'|assert_eq!\(render_exec_row\(&(\w+)\)\s*,\s*'
    r'("(?:[^"\\]|\\.)*"(?:\s*\.to_string\(\))?(?:\s*,\s*"(?:[^"\\]|\\.)*")*)\s*,?\s*\)'
)


def _rust_pairs(src: str) -> list[tuple[dict, str]]:
    """按**出现顺序**取 (回执行, 期望字) 成对值。

    线性扫描就够：每个 `assert_eq!` 用的都是它前面最近一次 `let <ident> = json!`，
    同名 `row` 在不同 `#[test]` 里被反复赋值也分得开（文件顺序天然分隔）。
    解析不出来的 `json!`（带变量、`Row::default()` 之类）**丢掉**——丢多少有下限
    断言兜着，丢多了会红。
    """
    env: dict[str, dict] = {}
    out: list[tuple[dict, str]] = []
    for m in _RUST_PAIR_RE.finditer(src):
        if m.group(1):
            ident, start = m.group(1), m.end() - 1
            depth = 0
            for j in range(start, len(src)):
                if src[j] == "{":
                    depth += 1
                elif src[j] == "}":
                    depth -= 1
                    if depth == 0:
                        raw = src[start:j + 1]
                        break
            else:
                continue
            raw = re.sub(r"//[^\n]*", "", raw)        # json! 里允许行注释
            raw = re.sub(r",(\s*[}\]])", r"\1", raw)  # json! 允许尾逗号
            try:
                env[ident] = json.loads(raw)
            except ValueError:
                env.pop(ident, None)
        else:
            ident, lit = m.group(2), m.group(3)
            try:
                want = "".join(json.loads("[" + re.sub(r"\.to_string\(\)", "", lit) + "]"))
            except ValueError:
                continue
            if ident in env:
                out.append((env[ident], want))
    return out


def _meta_of(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in ("tool", "args")}


def _why_not_compared(row: dict) -> str:
    """这一对**结构上**不参与比较的理由；空串 = 应当参与。"""
    if row.get("digest") or row.get("principal_role"):
        return "排版附加件（身份前缀 / 实体摘要）只在 Rust 侧拼，Python 那档看不到"
    if "action" in row:
        return "合成输入：action 是手写的（含方括号 [12]），钉的是接线不是 Python 的输出"
    if not receipt_action(str(row.get("tool") or ""), row.get("args") or {}, _meta_of(row)):
        return "无臂工具（Python 不写 action，这份回执仍走 Rust 老表）"
    return ""


# **有意偏离**：只有父仓测试里**真有**对应行的才需要列在这里。另三处偏离（检索类写
# 「检索文章」、文章标签那句多一个「的」、before/after 全缺时不给空冒号）在父仓那张
# 表里没有对应测试行，因此这一节比不到它们——它们由 ② 的手抄表钉着。
_INTENTIONAL = [
    ("create_tag", "新建一级标签「」", "新建标签"),
    ("read_messages", "标记站内信已读（3、4、5 等 4 条）",
     "标记站内信已读（3、4、5 等 4 封）"),
]

if _rs is None:
    # ★ 这一节在这台机器上**没有比较过**（不是通过）。20260928 实测：CI 只 checkout
    # agent 仓（父仓私有）⇒ `_parent_repo.read` 返回 None，而本节原先照样往下跑断言
    # ⇒ 0 对、四条全红，且红得与 Python 侧一个字都没关系（本机恒绿、CI 恒红 = 判据在测
    # "这台机器有没有兄弟目录"）。所以这里不是"少跑一条"，是把"没评"与"通过"分开写：
    # 读到这行的人应当知道——这一节是这套跨语言契约在 CI 上的**唯一**比较点。
    # 夜间门禁设 `SAUDADE_REQUIRE_PARENT=1` ⇒ 上一节 `_parent_repo.read` 直接
    # SystemExit(1)，根本走不到这里。
    print("  ⏭ ⑤ 未评估：拿不到父仓源码 ⇒ 这一节**一次也没比过**（不是通过）。"
          "夜间门禁设 SAUDADE_REQUIRE_PARENT=1，那一侧会把它变成 ❌ 并退出 1。")
else:
    _pairs = _rust_pairs(_rs)
    check(f"从父仓测试源码现取成对期望（{len(_pairs)} 对，下限 26）",
          len(_pairs) >= 26, str(len(_pairs)))

    _same: list[str] = []
    _skipped: list[str] = []
    _diffs: list[tuple[str, str, str]] = []
    for _row, _want in _pairs:
        _why = _why_not_compared(_row)
        if _why:
            _skipped.append(f"{_row.get('tool')}【{_why}】")
            continue
        _got = receipt_action(str(_row.get("tool") or ""), _row.get("args") or {},
                              _meta_of(_row))
        _got = _got.replace("[", "「").replace("]", "」")[:120]  # Rust 只排版的两件
        if _got == _want:
            _same.append(_want)
        else:
            _diffs.append((str(_row.get("tool")), _want, _got))

    check(f"★ 逐条对上：{len(_same)} 条 Python 输出经 Rust 排版后与父仓期望**逐字相同**",
          len(_same) >= 20, str(len(_same)))
    check("★ 剩下的差异**恰好**是有意偏离（白名单外差一个字都红）",
          sorted(_diffs) == sorted(_INTENTIONAL), str(_diffs))
    check("  且白名单没有陈年条目（父仓哪天把某条改了，这条会红 ⇒ 删掉那行白名单）",
          all(d in _diffs for d in _INTENTIONAL))
    check("★ 每一对都有归宿（逐字相同 / 有意偏离 / 三类结构性跳过），没有悄悄漏掉的",
          len(_same) + len(_diffs) + len(_skipped) == len(_pairs),
          f"same={len(_same)} diff={len(_diffs)} skip={len(_skipped)} all={len(_pairs)}")
    check("  跳过的每一对都写明了理由（无臂 / 合成输入 / 排版附加件）",
          all(_why_not_compared(_row) for _row, _ in _pairs
              if f"{_row.get('tool')}【{_why_not_compared(_row)}】" in _skipped))

# ══════════════════════════════════════════════════════════════════
print("\n⑥ 新臂逐一钉住（冻结之后加的工具，动作词只住在 Python 这侧）")

# `list_admin_board`（20261001 后台留言名册）：它是"这一轮到底翻了哪些留言"的唯一
# 线索——**筛选条件必须出现在行里**，否则回执只说"查看后台留言名册"，而主人问的是
# "匿名的那些是谁发的"、agent 只用关键词捞了一小撮，这种偏差在台账里看不出来。
check("① 名册·无参：两档同字", _pre("list_admin_board") == "查看后台留言名册"
      == _full("list_admin_board"), _full("list_admin_board"))
check("① 名册·只看待审", _full("list_admin_board", {"status": "pending"})
      == "查看后台留言名册（只看待审的）")
check("① 名册·两条件都印（顺序 = 状态、关键词）",
      _full("list_admin_board", {"status": "rejected", "keyword": "垃圾"})
      == "查看后台留言名册（只看未通过的，关键词「垃圾」）")
check("① 名册·关键词在 preview 档截到 24 字、full 档给全",
      "「" + "长" * 24 + "」" in _pre("list_admin_board", {"keyword": "长" * 40})
      and "「" + "长" * 40 + "」" in _full("list_admin_board", {"keyword": "长" * 40}))
# ③ 那条通用不变量的具体形态：认不出的 status 不许渲染成「只看」后面跟个空
check("① 名册·认不出的 status 不硬凑（不许出现『只看）』这种半截）",
      _full("list_admin_board", {"status": "???", "keyword": "  "}) == "查看后台留言名册",
      _full("list_admin_board", {"status": "???", "keyword": "  "}))
check("① 名册·被 `$…` 引用时印的是中文来源名，不是内部工具名",
      _pre("get_article_detail", {"doc_type": "board", "article_id": "$list_admin_board[0].talkKey"})
      == "读取留言（上一步后台留言名册的第 1 条）",
      _pre("get_article_detail", {"doc_type": "board", "article_id": "$list_admin_board[0].talkKey"}))

print(f"\n{'全部通过' if not FAILS else f'失败 {len(FAILS)} 项'}")
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
