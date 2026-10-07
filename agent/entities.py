"""可引用实体的确定性摘要（20260920，探针驱动）。

动机（`/tmp/probe_entity_ref.py` 实测，2026-09-20 晚）：指代**解析**已经 4/4 正确
（文章类靠 doc_anchors、主题延续靠最近的读全文帧），但**取值**一律靠"把工具再跑一遍"
——「第二条写了什么」重跑 list_guestbook、「那个分类下面有几篇」重跑 list_categories。
根因是工具帧只活在当轮：跨轮历史里只有 user/assistant 文本（ToolMessage 不入库），
跨轮唯一的确定性事实源是 execution_log 的动作行，而动作行只写"查看留言板"，不写
那次取回了什么。于是"指代能定位、值取不到"。

本模块补上值：`receipt_digest()` 把数据工具返回压成一行实体摘要，随 checker 回执
走 `__EXEC__` → Rust `render_exec_row` 拼在动作行之后落 execution_log
（`查看留言板 — 最近3条: 1.〔诉〕「测试260905」 …`）→ 下轮作为 recent_executions 注入
planner 与 narrator ⇒ 指代可以**零调用直接取值**（见 graph.py 规则 6b）。

纪律：
  · 只搬事实、不做判断（摘要里的数字/条目原文必须与工具返回一致）；
  · 解析失败/形态不符一律返回空串——**绝不猜**（空摘要退化为改动前的行为）；
  · **id 一律带命名空间**（20260926 批 4）：`noteId:` / `notifId:` / `mailId:`
    （留言是 `talkId:`、用户是 `userId:`，见 tools/base.py::_board_label）。
    帧里几个 id 命名空间并存，裸数字没有东西说明它是哪一种——trace
    `20260924T030031` 实证 planner 把留言 id 当文章 id 去调 `get_article_detail`。
    别名与 tools/base.py / reports.py 的渲染同一套，改一处要全改。
  · 纯函数、零 LLM、可离线单测（tests/test_entities.py）。
"""
from __future__ import annotations

import ast
import json
import re

# 摘要总长上限：Rust 侧 detail 列 varchar(300)，动作行 + 摘要一起截断；
# 留足动作行空间（"查看留言板"约 10 字），并防 8 行窗口把注入串（上限 1500）撑爆。
# 20260926 批 4（id 具名，见下）给每条加了 5–8 字的命名空间前缀 ⇒ 满档时少列一条
# ——**这是知道的取舍**：150 这个数是按"8 行 × 150 < 1500 注入上限"定的，动它要连带
# 重算注入预算；而"id 被当别的物件用"是已经发生过的事故（trace 20260924T030031）。
_DIGEST_MAX = 150
_ITEM_MAX = 5          # 列表类最多列几条（"第N条"的 N 要数得出来，不截太狠）
_TITLE_MAX = 6         # 标题/id 候选最多列几个


def _parse(result: str):
    """工具返回文本 → Python 结构（解析不出返回 None，不猜）。

    工具返回是 `_shape(data)` 的产物：`str(list[dict])` 形态的 Python repr
    （单引号、True/False）——用 literal_eval 解；个别工具可能回 JSON，兜底 json。
    """
    if not result:
        return None
    text = result.strip()
    if text[:1] not in "[{":
        return None                      # 命令帧/纯文本（list_devices 等）不做摘要
    try:
        return ast.literal_eval(text)
    except Exception:
        try:
            return json.loads(text)
        except Exception:
            return None


_BRACKET_PAIRS = (("《", "》"), ("「", "」"), ("（", "）"), ("【", "】"), ("(", ")"), ("[", "]"))


def _clip(text, n: int) -> str:
    """单行化 + 去引号（「」由摘要自己加）+ 截断。

    截断点落在**成对括号内**时退回括号前（20260921 线上实测：《Saudade Blog AI Agent
    （泠月喵）架构文档》按 22 字截成 `Saudade Blog AI Agent（`，回执里出现
    `19《Saudade Blog AI Agent（》`——书名号里挂着半个圆括号，读起来像数据坏了）。
    宁可少几个字，也不要留半个括号。
    """
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    for op, cl in _BRACKET_PAIRS:
        while cut.count(op) > cut.count(cl):
            i = cut.rfind(op)
            if i <= 0:
                break
            cut = cut[:i]
    return cut.rstrip(" \t·、,，-—")


def _rows(data, key: str = "") -> list:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in (key, "data", "records", "list", "items"):
            if isinstance(data.get(k), list):
                return [x for x in data[k] if isinstance(x, dict)]
    return []


def _join(parts: list[str], max_len: int = _DIGEST_MAX) -> str:
    """保序拼接，超长即止（宁少列几条，也不切掉半条）。"""
    out: list[str] = []
    for p in parts:
        candidate = "/".join(out + [p])
        if len(candidate) > max_len and out:
            break
        out.append(p)
    return "/".join(out)


_NOTE_TOTAL_RE = re.compile(r"共 (\d+) 条")
# "第 61-120 条"——`_cap_rows` 在 offset>0（取回更早的那一页）时写进注记的号段。
_NOTE_RANGE_RE = re.compile(r"第 (\d+)-(\d+) 条")


def _note_total(data) -> int:
    """从帧尾的〔系统注记〕里读"共 N 条"（列表工具封顶时写进去的总数）。没有则 0。

    为什么摘要要读它：`tools/base.py::_cap_rows` 按上限裁行之后，`len(rows)` 只剩
    上限条数，而摘要这条要说的恰恰是**站点里一共多少条**。不读总数就等于让系统在
    跨轮执行记忆里把 137 条说成 60 条——那是**系统自己**说假话，比模型编还坏。
    注记是**非 dict 尾元素**（同 `_rows` 的过滤口径），所以这里显式跳过 dict 找字符串。
    """
    if not isinstance(data, list):
        return 0
    for x in data:
        if isinstance(x, dict):
            continue
        m = _NOTE_TOTAL_RE.search(str(x))
        if m:
            return int(m.group(1))
    return 0


def _note_range(data) -> tuple[int, int] | None:
    """从帧尾的〔系统注记〕里读"第 A-B 条"（取回更早那一页时的号段）。没有则 None。

    为什么摘要也要读它：`offset > 0` 时手上这 60 行**不是最近的那批**（是第 61-120 条），
    而 `_entry_digest` 原来只会按 `len(rows)` 说"最近 60 条"——那会让跨轮执行记忆把
    "翻页拉回来的那一页"错说成"最新的一页"，正是本仓最不能忍的那类假话（系统自己说的，
    比模型编还坏，同 `_note_total` 的理由）。注记里的号段是唯一能区分两者的信息。
    """
    if not isinstance(data, list):
        return None
    for x in data:
        if isinstance(x, dict):
            continue
        m = _NOTE_RANGE_RE.search(str(x))
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _entry_digest(data) -> str:
    """留言板/说说：序号 + 分类 + 内容首段（序号是"第二条"能对号的关键）。

    **分类要看得见是分类**（20261001）：`cat`/`talkTitle` 是心情词（寄/忆/诉/愿），
    裸印成 `4.诉「博主是大笨狗」` 会被读成"某人说了某话"——trace `20260930T235232`
    里模型就回了"那条留言是访客**诉**发的"。加 〔〕 之后它与正文（「」内）在字形上
    就分得开，与帧尾〔系统注记〕同一套标记。原文一字不改，只加了一层壳。
    """
    rows = _rows(data)
    if not rows:
        return ""
    items = []
    for i, r in enumerate(rows[:_ITEM_MAX], 1):
        cat = _clip(r.get("cat") or r.get("talkTitle") or "", 4)
        body = _clip(r.get("content") or r.get("talkContent") or "", 18)
        if not body:
            continue
        items.append(f"{i}.〔{cat}〕「{body}」" if cat else f"{i}.「{body}」")
    total = _note_total(data)
    span = _note_range(data)
    # 只在本轮**真的**被裁过（总数 > 手上条数）时才改口径，否则输出与从前逐字节相同。
    if span:
        # 取回更早那一页 ⇒ 手上这批**不是**最新的，绝不能说"最近 N 条"（见 `_note_range`）。
        head = f"第{span[0]}-{span[1]}条/共{total}条"
    elif total > len(rows):
        head = f"最近{len(rows)}条/共{total}条"
    else:
        head = f"最近{len(rows)}条"
    return f"{head}: " + _join(items) if items else ""


# 审核态 → 中文（与后台「我的河灯」页签的标签、复核用语同源：上游 `talks.rs` 的
# 口径是 1=通过 / 0=待审 / 2=未通过）。**读不出就写"状态未知"**，不许兜成"待审"
# ——那会把"这一格没读到"说成一条审核结论，正是本仓最不能忍的那类假话。
_BOARD_STATE_WORD = {1: "通过", 0: "待审", 2: "未通过"}


def _my_board_digest(data) -> str:
    """我的河灯（20261008）：`talkId` + 状态 + 分类 + 内容首段。

    比 `_entry_digest` 多两样，两样都是这条通道**独有**的：
      · **状态**（通过/待审/未通过）——这条通道存在的全部理由就是它（公开池只有
        已通过态，"我哪条留言通过了"在那边读不出来）；
      · **`talkId`**——`/guestbook?lid=<talkId>` 这个位置形态的取值就是它（见
        `agent/context.py::_item_link_fact()`）；摘要里带上，下一轮
        「带我去那条」才不用重查一遍。
    """
    rows = _rows(data)
    if not rows:
        return ""
    items = []
    for i, r in enumerate(rows[:_ITEM_MAX], 1):
        state = _BOARD_STATE_WORD.get(r.get("approved"), "状态未知")
        body = _clip(r.get("content") or r.get("talkContent") or "", 18)
        if not body:
            continue
        tid = r.get("talkId")
        head = f"{i}.talkId:{tid}" if isinstance(tid, int) else f"{i}."
        cat = _clip(r.get("cat") or r.get("talkTitle") or "", 4)
        items.append(f"{head}[{state}]" + (f"〔{cat}〕" if cat else "") + f"「{body}」")
    total = _note_total(data)
    head = f"最近{len(rows)}条/共{total}条" if total > len(rows) else f"最近{len(rows)}条"
    return f"我的河灯 {head}: " + _join(items) if items else ""


def _category_digest(data) -> str:
    rows = _rows(data)
    if not rows:
        return ""
    items = []
    for r in rows[:_ITEM_MAX * 2]:
        name = _clip(r.get("categoryTitle") or r.get("title") or "", 10)
        cnt = r.get("noteCount")
        if name and isinstance(cnt, int):
            items.append(f"{name} {cnt} 篇")   # 空格分隔：无空格时"Web3"+"0篇"读成"Web30篇"
        elif name:
            items.append(name)
    return f"{len(rows)} 个分类: " + _join(items) if items else ""


def _tag_digest(data) -> str:
    rows = _rows(data)
    if not rows:
        return ""
    # 两级标签（20260921）：list_tags 现在把 /tagone 与 /tagtwo 合在一张表里，
    # 计数必须分开——"9 个标签"里混着 5 个一级 4 个二级，用户问"编程下面有几个
    # 二级标签"时这行是唯一依据。二级写成 `父/子`（与前端 flattenTagOptions 的
    # 展示名一致），下轮"那个 Rust 标签"才指认得回来。
    def _lvl(r) -> str:
        return str(r.get("level") or "").strip()
    lv2 = [r for r in rows if _lvl(r) == "2"]
    items = []
    for r in rows[:_ITEM_MAX * 2]:
        name = _clip(r.get("title") or r.get("tagTitle") or "", 8)
        if not name:
            continue
        father = _clip(r.get("fatherTag") or "", 6) if _lvl(r) == "2" else ""
        label = f"{father}/{name}" if father else name
        # 每标签文章数（20260921，Rust 侧 `noteCount`）：口径 = 公开可见文章。
        # 只认 int——认不出就只写名字，不写"0 篇"（0 是结论，缺字段不是）。
        cnt = r.get("noteCount")
        items.append(f"{label} {cnt} 篇" if isinstance(cnt, int) else label)
    if not items:
        return ""
    head = f"{len(rows)} 个标签"
    if lv2:
        head += f"（一级 {len(rows) - len(lv2)}/二级 {len(lv2)}）"
    return f"{head}: " + _join(items)


def _note_digest(data, label: str) -> str:
    rows = _rows(data)
    if not rows:
        return ""
    items = []
    for r in rows[:_TITLE_MAX]:
        # `noteId` 是 `/api/protected/favorites` 的字段名（20260923 收藏列表复用
        # 本摘要器时补上）——与 noteKey/key 同一个东西，三个名字都认。
        nid = r.get("noteKey") or r.get("key") or r.get("noteId") or r.get("id")
        title = _clip(r.get("noteTitle") or r.get("title") or "", 22)
        if nid is not None and title:
            items.append(f"noteId:{nid}《{title}》")
    return f"{label}: " + _join(items) if items else ""


def _notification_digest(data) -> str:
    """站内通知/公告（20260923）：总条数 + 未读条数 + **id《标题》**（未读的标出来）。

    **正文刻意不进摘要**：通知正文是一段话（公告正文、驳回理由），挤进这 150 字
    只会把标题挤掉，而"第几条的标题是什么"才是跨轮指代的锚点；要正文就再调一次
    工具（摘要是"有哪些、哪条没看"，不是全文副本）。

    20260923 三轮：条目从「标题」补成「id《标题》」，抬头从「通知 N 条」改成
    「通知**共** N 条」。trace `20260923T130020_9` 实证 planner 把**条数**读成了
    **id**——它要填的是 `ids`，而摘要里唯一的数字是"3 条"（真实那条是 id 23），
    于是写下 `ids:[3]` ⇒ 一次注定 0 行的写 + 一次「服务不可用」的误报（原因码
    也改准了，见 tools/base.py 的 read_notifications）。"参数要 id 而摘要不给 id"
    这种缺什么就编什么的坑，只能靠**把 id 给足**来堵；"共"字是第二道，让 3 只能
    被读成条数。id 缺字段时不写假 id（缺字段绝不编，与 `_clip` 同一条纪律）。
    """
    rows = _rows(data)
    if not rows:
        return ""
    unread = sum(1 for r in rows if not r.get("isRead"))
    items = []
    for r in rows[:_TITLE_MAX]:
        title = _clip(r.get("title") or "", 18)
        if not title:
            continue
        nid = r.get("id")
        mark = "" if r.get("isRead") else "（未读）"
        if isinstance(nid, int) and not isinstance(nid, bool):
            items.append(f"notifId:{nid}《{title}》{mark}")
        else:
            items.append(f"{title}{mark}")
    head = f"通知共 {len(rows)} 条（未读 {unread}）"
    return f"{head}: " + _join(items) if items else head


def _unread_digest(data) -> str:
    """未读汇总（20260923）：两个数 + 总数；20260924 起**连带未读条目的 id《标题》**。

    只认 int（缺字段/形态不符返回空串，不写成 0——"0 条未读"是结论，
    "没读到字段"不是，两者必须分开）。key 名与 Rust `UnreadDto` 同源。

    条目那半取自同一次调用的 `unread_items`（工具已按未读过滤）——**id 必须给**：
    `read_notifications` 的实参是 id 列表，"摘要里不给 id"就只能靠编
    （20260923 三轮把条数当 id 的教训，见 `_notification_digest`）。
    读失败时工具带 `unread_items_note` 而条目为空，这里自然退化成计数版——
    不编条目，也不把"没读到"写成 0。
    """
    if not isinstance(data, dict):
        return ""
    vals = []
    for key in ("notifications", "messages", "total"):
        v = data.get(key)
        if not isinstance(v, int) or isinstance(v, bool):
            return ""
        vals.append(v)
    n, m, t = vals
    base = f"未读: 通知 {n} 条 / 私信 {m} 条（合计 {t}）"
    items = data.get("unread_items")
    if not isinstance(items, list):
        return base
    rows = [r for r in items if isinstance(r, dict)]
    parts = []
    for r in rows[:_TITLE_MAX]:
        title = _clip(r.get("title") or "", 18)
        if not title:
            continue
        nid = r.get("id")
        if isinstance(nid, int) and not isinstance(nid, bool):
            parts.append(f"notifId:{nid}《{title}》")
        else:
            parts.append(f"《{title}》")     # 缺 id 不写假 id（缺字段绝不编）
    return f"{base} — 未读通知: " + _join(parts) if parts else base


def _mailbox_digest(data) -> str:
    """站内信（20260923 批 8）：收件/发出各几封 + 未读几封 + 每封 `id《标题》寄自谁（未读）`。

    **id 必须给**（与通知摘要同一条教训，20260923 三轮）：`read_messages` 要的实参是
    id 列表，摘要里不给 id，"要标记哪几封"就只能靠编——trace `20260923T130020_9` 那次
    「把条数当 id」的误导正是这么来的。

    抬头刻意写「收件 N 封」而不是「收到 N 条」：**封**是信的量词、**条**是通知的，
    量词分开能让 planner（和 narrator）一眼看出这是另一个物件——两个工具的实参
    （通知 id / 信 id）来源不同，混了就是一次注定对不上的写。

    只认 list 形态；标题是真的空（历史信件 title 列是 NULL）就写「（无标题）」——
    那是事实，不是缺字段；id 不是整数则**不写编号**（缺字段绝不编，见本模块头注）。
    """
    if not isinstance(data, dict):
        return ""
    inbox, outbox = data.get("inbox"), data.get("outbox")
    if not isinstance(inbox, list) or not isinstance(outbox, list):
        return ""
    head = f"信箱: 收件 {len(inbox)} 封"
    unread = data.get("unread")
    if isinstance(unread, int) and not isinstance(unread, bool):
        head += f"（未读 {unread}）"
    head += f" / 发出 {len(outbox)} 封"
    items = []
    for r in inbox[:_ITEM_MAX]:
        if not isinstance(r, dict):
            continue
        title = _clip(r.get("title") or "", 14)
        mid = r.get("id")
        has_id = isinstance(mid, int) and not isinstance(mid, bool)
        if title:
            body = f"mailId:{mid}《{title}》" if has_id else f"《{title}》"
        else:
            # 标题是真的空（历史信件 title 列是 NULL）——那是事实不是缺字段，但
            # **别写成 `9《（无标题）》`**（括号套括号，会被读成标题就叫「（无标题）」）。
            body = f"mailId:{mid}（无标题信）" if has_id else "（无标题信）"
        peer = _clip(r.get("peerName") or "", 8)
        if peer:
            body += f"寄自{peer}"
        items.append(body + ("" if r.get("isRead") else "（未读）"))
    if not items:
        return head
    return f"{head}: " + _join(items)


def _announcement_digest(data) -> str:
    rows = _rows(data)
    if not rows:
        return ""
    items = []
    for r in rows[:3]:
        title = _clip(r.get("title") or "", 18)
        day = _clip(str(r.get("createdAt") or r.get("createTime") or "")[:10], 10)
        if title:
            items.append(f"{title}（{day}）" if day else title)
    return "公告: " + _join(items) if items else ""


# 工具名 → 摘要生成器（未列出的工具不产摘要：动作类没有"可取的值"，
# 文本型返回（list_devices/get_weather）不做结构化解析）
_DIGESTERS = {
    "list_guestbook": _entry_digest,
    "list_talks": _entry_digest,
    "list_categories": _category_digest,
    "list_tags": _tag_digest,
    "list_notes": lambda d: _note_digest(d, "文章列表"),
    "search_notes": lambda d: _note_digest(d, "搜索结果"),
    "get_top_notes": lambda d: _note_digest(d, "置顶文章"),
    "get_announcements": _announcement_digest,
    # 用户自己的数据（20260923）
    "list_my_favorites": lambda d: _note_digest(d, "我的收藏"),
    "list_notifications": _notification_digest,
    "get_unread_summary": _unread_digest,
    # 站内信（20260923 批 8）：`{inbox, outbox, unread}` 形态，不是"行列表"，
    # 所以进不了 `_rows` 那族摘要器
    "list_my_messages": _mailbox_digest,
    # 我自己的河灯（20261008）：与公开池 `list_guestbook` 共用 `_entry_digest` 会丢掉
    # 两样这条通道独有的东西——**状态**与 **`talkId`**，而"我哪条通过了""带我去那条"
    # 要的正是它们（见 `_my_board_digest`）。
    "list_my_board": _my_board_digest,
}


# ── 文本型摘要器（20260921，管理助手报表）────────────────────────────
# 报表类工具刻意返回**渲染好的中文报表**而不是 `_shape(data)`（WHY 见
# agent/reports.py 头注），所以它们进不了上面那族结构化摘要器（`_parse` 对纯文本
# 返回 None）。这里退一步按**文本正则**抽几个关键数字——目的只有一个：跨轮
# 「刚才磁盘占用多少」能零调用取值（rule 6b），**不是**复述整张报表。
# 抽不到就返回空串（退化成"没有摘要"，与改动前一致），绝不猜。

def _find(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text or "")
    return m.group(1) if m else None


def _server_status_digest(text: str) -> str:
    parts = []
    cpu = _find(text, r"- CPU：.*?使用率 ([\d.]+%)")
    if cpu:
        parts.append(f"CPU {cpu}")
    load = _find(text, r"- 负载：1 分钟 ([\d.]+)")
    if load:
        parts.append(f"负载 {load}")
    mem = _find(text, r"- 内存：.*?（(\d+%)）")
    if mem:
        parts.append(f"内存 {mem}")
    disks = re.findall(r"- 磁盘 (\S+)：.*?（(\d+%)）", text or "")
    if disks:
        parts.append("磁盘 " + "、".join(f"{p} {v}" for p, v in disks[:2]))
    return "服务器状态: " + "／".join(parts) if parts else ""


def _service_health_digest(text: str) -> str:
    parts = []
    # 两个分支都要抽：读不到的服务若不进摘要，就变成"摘要里没有这个服务"，
    # 下轮问"agent 服务怎么样"时 narrator 只能重跑工具（或者更糟——照摘要答，
    # 把一个没读到的服务说成没事）。抽成 `agent=读不到` 至少是如实的一条。
    states = re.findall(r"- (saudade-\w+)：(?:(\S+?)/|(读不到))", text or "")
    if states:
        parts.append("服务 " + " ".join(
            f"{u.removeprefix('saudade-')}={r or unread}" for u, r, unread in states))
    warn, fail = _find(text, r"WARN (\d+) 条"), _find(text, r"FAIL (\d+) 条")
    if warn is not None or fail is not None:
        parts.append(f"心跳 WARN {warn or 0}/FAIL {fail or 0}")
    rounds = _find(text, r"- 今日对话：(\d+) 轮")
    if rounds is not None:
        abnormal = _find(text, r"异常收尾 (\d+) 轮")
        parts.append(f"今日 {rounds} 轮" + (f"（异常 {abnormal}）" if abnormal else ""))
    return "服务健康: " + "；".join(parts) if parts else ""


def _moderation_digest(text: str) -> str:
    total = _find(text, r"- 总计 (\d+) 条")
    if total is None:
        return ""
    parts = [f"留言 {total} 条"]
    detail = _find(text, r"- 总计 \d+ 条：待审 (\d+)、已通过 (\d+)、已驳回 (\d+)")
    if detail:
        m = re.search(r"待审 (\d+)、已通过 (\d+)、已驳回 (\d+)", text or "")
        parts.append("待审 {}、通过 {}、驳回 {}".format(*m.groups()))
    need = _find(text, r"仍待审 (\d+) 条")
    if need:
        parts.append(f"AI 拦下待审 {need}")
    return "审核: " + "；".join(parts)


def _user_stats_digest(text: str) -> str:
    total = _find(text, r"- 用户总数 (\d+)")
    if total is None:
        return ""
    parts = [f"用户 {total} 人"]
    conv, msg = _find(text, r"- 会话 (\d+) 个"), _find(text, r"消息 (\d+) 条")
    if conv is not None and msg is not None:
        parts.append(f"会话 {conv}、消息 {msg}")
    active = _find(text, r"近 7 天 (\d+) 人")
    if active is not None:
        parts.append(f"近 7 天活跃 {active} 人")
    return "用户数据: " + "；".join(parts)


def _note_traffic_digest(text: str) -> str:
    """文章流量报表 → 一行实体摘要（20260930）。

    为什么值得单列一条：主人问"哪几篇最火"时，答案里**必须带标题**——而工具帧不跨轮
    持久化，下一轮问"刚才第一名那篇有多少赞"就只能靠这一行（rule 6b 同一条道理：
    能照抄摘要作答就别重跑报表）。所以摘要把**榜首的标题与它的数**带上，不只是合计。

    三处口径与报表渲染同源（`agent/reports.py::render_note_stats`）：
      · 合计只取"全站合计"那一行的数（榜上的行加起来只会比真值小）；
      · 榜首 = 各榜的第一行（`第 1 名`），抽不到就是没有榜（那时退化成只有合计）；
      · 抽不出任何东西 → 空串（退化为只有动作行，不编）。
    """
    parts = []
    views = _find(text, r"全站合计[^\n]*?阅读 (\d+)")
    likes = _find(text, r"全站合计[^\n]*?点赞 (\d+)")
    if views is not None:
        parts.append(f"阅读合计 {views}" + (f"、点赞 {likes}" if likes is not None else ""))
    # 每个榜首**只在自己那张榜的块里找**：榜上的行同时带着三个数（点赞榜第一行里
    # 也有"阅读 40"），不按块切就会把点赞榜第一名报成"阅读榜首"——一个看起来完全
    # 正常的假事实（首版就是这么写的，被这条注释挡下）。
    for label in ("阅读", "点赞", "收藏"):
        block = re.search(rf"^- {label}榜（[^\n]*\n(?P<rows>(?:  · [^\n]*\n?)*)",
                          text or "", re.M)
        if not block:
            continue
        first = re.search(r"第 1 名 《(?P<title>[^》]{1,40})》（noteId \d+）(?P<tail>[^\n]*)",
                          block.group("rows"))
        if not first:
            continue
        # 数从**这一行的尾巴**里取（`阅读 120／点赞 8／…`）；取不到就只给标题
        n = _find(first.group("tail"), rf"{label} (\d+)") or ""
        parts.append(f"{label}榜首《{first.group('title')}》{n}")
    return "文章流量: " + "；".join(parts) if parts else ""


def _notice_read_digest(text: str) -> str:
    """标记已读的回执摘要（20260923 批 7）。

    这一条**不是**给"跨轮取值"用的（写操作的取值是文章 id / 条数，动作行里已经有了），
    而是给**下一轮的真实性追问**用的：主人问「你真给我标了吗」，执行记忆里那一行必须
    带着"标了几条、现在还剩几条未读"——否则 planner 只能看到"标记站内通知已读"，
    回答"标好了"与"没标"在记录里长得一样。

    抽不到数字（工具的 noop 路径："本来就是已读"）→ 空串（退化为只有动作行）。
    """
    marked = _find(text, r"已把 (\d+) 条通知标记为已读")
    if marked is None:
        return ""
    parts = [f"标记已读 {marked} 条"]
    n = _find(text, r"现在未读：通知 (\d+) 条")
    m = _find(text, r"现在未读：通知 \d+ 条 / 私信 (\d+)")
    if n is not None:
        parts.append(f"未读 通知 {n} 条" + (f"/私信 {m}" if m is not None else ""))
    return "；".join(parts)


def _admin_notes_digest(text: str) -> str:
    """后台文章清单的摘要（20261007）。

    这一条补的是**跨轮取值通道的一格**：`list_admin_notes` 此前没有摘要，回执行于是
    只剩动作「查看后台文章列表」，**那个数只活在 narrator 上一轮的散文里**。生产实证
    （trace `20261007T191440` → `20261007T191543`）：19:14 真查到「后台文章共 21 篇」、
    也照实说了；下一轮用户只说了句闲聊，narrator 却**主动撤回**自己那句话（原话里带着
    "这一轮系统没有查过文章列表，我手上也没有那个数字"）——不是幻觉，是**系统事实里
    没有那个数**，于是"零帧不得声称"的诚实纪律只能让它否认自己的散文。数补进台账行，
    规则 6b 才有东西可抄。

    两支**必须分开说**（`render_admin_notes` 自己那段注写明了为什么）：带 keyword 的
    那支是**搜索口径**（"匹配 N 篇"），不是站内总量。摘要是跨轮取值的来源，把搜索条数
    写成总量，就是"看着有据的错数"。同理，清单被截断（行尾有「另有 N 篇未列出」）时
    **不报状态分布**——那是前 `limit` 条的分布。
    """
    m = _ADMIN_NOTES_HEAD_RE.search(text or "")
    if not m:
        return ""
    n, kw = m.group("n"), m.group("kw")
    head = (f"后台文章列表（按「{kw}」筛） — 匹配 {n} 篇" if kw
            else f"后台文章列表 — 共 {n} 篇")
    out = head
    if "未列出" not in (text or ""):
        rows = _ADMIN_NOTES_ROW_RE.findall(text or "")
        counts = [(s, sum(1 for r in rows if s in r)) for s in ("公开", "私密", "草稿")]
        # 只有"每一行都认得出状态、且行数就是总数"时才敢报分布（认不出的状态、
        # 少列的行都会让三项之和 ≠ 总数 ⇒ 报出来就是拿局部冒充全量）。
        if len(rows) == int(n) and sum(c for _, c in counts) == len(rows):
            out += "（" + " / ".join(f"{s} {c}" for s, c in counts) + "）"
    return out


# 头行的两种形态（`render_admin_notes`）：无词「后台文章共 21 篇」、带词
# 「后台文章里匹配「x」的共 3 篇」。两条都认，且**把 keyword 记下来**（下游要分开说）。
_ADMIN_NOTES_HEAD_RE = re.compile(
    r"后台文章(?:里匹配「(?P<kw>[^」]*)」的)?共\s*(?P<n>\d+)\s*篇")
# 行形态：`- noteId=54 [公开/置顶]《…》标签：…`（状态在方括号里，可能带"置顶"）。
_ADMIN_NOTES_ROW_RE = re.compile(r"^- noteId=\d+ \[([^\]]*)\]", re.M)


_TEXT_DIGESTERS = {
    "get_server_status": _server_status_digest,
    "get_service_health": _service_health_digest,
    "get_moderation_status": _moderation_digest,
    "get_user_stats": _user_stats_digest,
    "get_note_stats": _note_traffic_digest,
    "read_notifications": _notice_read_digest,
    # 后台文章清单（20261007）：管理员读后台的取值此前**没有任何跨轮来源**
    "list_admin_notes": _admin_notes_digest,
}


def receipt_digest(tool: str, result: str) -> str:
    """数据工具返回 → 一行实体摘要（无摘要能力/解析失败 → ""）。"""
    name = tool or ""
    try:
        if name in _DIGESTERS:
            out = _DIGESTERS[name](_parse(result))
        elif name in _TEXT_DIGESTERS:
            out = _TEXT_DIGESTERS[name](result or "")
        else:
            return ""
    except Exception:                    # 摘要绝不能影响主链路（回执落库）
        return ""
    return (out or "")[:_DIGEST_MAX]
