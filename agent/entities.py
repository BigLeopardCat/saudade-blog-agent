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


def _clip(text, n: int) -> str:
    """单行化 + 去引号（「」由摘要自己加）+ 截断。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:n]


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
    names = [_clip(r.get("title") or r.get("tagTitle") or "", 8) for r in rows[:_ITEM_MAX * 2]]
    names = [n for n in names if n]
    return f"{len(rows)} 个标签: " + _join(names) if names else ""


def _note_digest(data, label: str) -> str:
    rows = _rows(data)
    if not rows:
        return ""
    items = []
    for r in rows[:_TITLE_MAX]:
        nid = r.get("noteKey") or r.get("key") or r.get("id")
        title = _clip(r.get("noteTitle") or r.get("title") or "", 22)
        if nid is not None and title:
            items.append(f"{nid}《{title}》")
    return f"{label}: " + _join(items) if items else ""


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
}


def receipt_digest(tool: str, result: str) -> str:
    """数据工具返回 → 一行实体摘要（无摘要能力/解析失败 → ""）。"""
    digester = _DIGESTERS.get(tool or "")
    if digester is None:
        return ""
    try:
        out = digester(_parse(result))
    except Exception:                    # 摘要绝不能影响主链路（回执落库）
        return ""
    return (out or "")[:_DIGEST_MAX]
