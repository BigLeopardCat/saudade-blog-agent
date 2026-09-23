"""可引用实体的确定性摘要（20260920，探针驱动）。

动机（`/tmp/probe_entity_ref.py` 实测，2026-09-20 晚）：指代**解析**已经 4/4 正确
（文章类靠 doc_anchors、主题延续靠最近的读全文帧），但**取值**一律靠"把工具再跑一遍"
——「第二条写了什么」重跑 list_guestbook、「那个分类下面有几篇」重跑 list_categories。
根因是工具帧只活在当轮：跨轮历史里只有 user/assistant 文本（ToolMessage 不入库），
跨轮唯一的确定性事实源是 execution_log 的动作行，而动作行只写"查看留言板"，不写
那次取回了什么。于是"指代能定位、值取不到"。

本模块补上值：`receipt_digest()` 把数据工具返回压成一行实体摘要，随 checker 回执
走 `__EXEC__` → Rust `render_exec_row` 拼在动作行之后落 execution_log
（`查看留言板 — 最近3条: 1.诉「测试260905」 …`）→ 下轮作为 recent_executions 注入
planner 与 narrator ⇒ 指代可以**零调用直接取值**（见 graph.py 规则 6b）。

纪律：
  · 只搬事实、不做判断（摘要里的数字/条目原文必须与工具返回一致）；
  · 解析失败/形态不符一律返回空串——**绝不猜**（空摘要退化为改动前的行为）；
  · 纯函数、零 LLM、可离线单测（test_entities.py）。
"""
from __future__ import annotations

import ast
import json
import re

# 摘要总长上限：Rust 侧 detail 列 varchar(300)，动作行 + 摘要一起截断；
# 留足动作行空间（"查看留言板"约 10 字），并防 8 行窗口把注入串（上限 1500）撑爆。
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


def _entry_digest(data, label: str) -> str:
    """留言板/说说：序号 + 分类 + 内容首段（序号是"第二条"能对号的关键）。"""
    rows = _rows(data)
    if not rows:
        return ""
    items = []
    for i, r in enumerate(rows[:_ITEM_MAX], 1):
        cat = _clip(r.get("cat") or r.get("talkTitle") or "", 4)
        body = _clip(r.get("content") or r.get("talkContent") or "", 18)
        if not body:
            continue
        items.append(f"{i}.{cat}「{body}」")
    return f"最近{len(rows)}条: " + _join(items) if items else ""


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
            items.append(f"{nid}《{title}》")
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
            items.append(f"{nid}《{title}》{mark}")
        else:
            items.append(f"{title}{mark}")
    head = f"通知共 {len(rows)} 条（未读 {unread}）"
    return f"{head}: " + _join(items) if items else head


def _unread_digest(data) -> str:
    """未读汇总（20260923）：两个数 + 总数。

    只认 int（缺字段/形态不符返回空串，不写成 0——"0 条未读"是结论，
    "没读到字段"不是，两者必须分开）。key 名与 Rust `UnreadDto` 同源。
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
    return f"未读: 通知 {n} 条 / 私信 {m} 条（合计 {t}）"


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
    "list_guestbook": lambda d: _entry_digest(d, "留言"),
    "list_talks": lambda d: _entry_digest(d, "说说"),
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


_TEXT_DIGESTERS = {
    "get_server_status": _server_status_digest,
    "get_service_health": _service_health_digest,
    "get_moderation_status": _moderation_digest,
    "get_user_stats": _user_stats_digest,
    "read_notifications": _notice_read_digest,
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
