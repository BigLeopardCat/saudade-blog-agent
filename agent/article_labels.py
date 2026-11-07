"""Display labels and public article-tag metadata; no write preparation or execution."""

from __future__ import annotations

import re

from agent.identifiers import normalize_target_id

_TAG_COLOR_ALIASES: dict[str, str] = {
    "蓝": "#1677ff", "蓝色": "#1677ff",
    "绿": "#52c41a", "绿色": "#52c41a",
    "橙": "#fa8c16", "橙色": "#fa8c16", "橘": "#fa8c16", "橘色": "#fa8c16",
    "粉": "#eb2f96", "粉色": "#eb2f96", "粉红": "#eb2f96", "粉红色": "#eb2f96",
    "洋红": "#eb2f96", "品红": "#eb2f96", "桃红": "#eb2f96",
    "紫": "#722ed1", "紫色": "#722ed1",
    "青": "#13c2c2", "青色": "#13c2c2",
    "红": "#f5222d", "红色": "#f5222d", "大红": "#f5222d",
    "黄绿": "#a0d911", "黄绿色": "#a0d911",
    "黄": "#a0d911", "黄色": "#a0d911",   # 站内没有正黄：落到黄绿，回程如实称「黄绿」
}
_COLOR_CANON: dict[str, str] = {
    "#1677ff": "蓝色", "#52c41a": "绿色", "#fa8c16": "橙色", "#eb2f96": "粉色",
    "#722ed1": "紫色", "#13c2c2": "青色", "#f5222d": "红色", "#a0d911": "黄绿色",
}


def match_tag_color(spec) -> str | None:
    """颜色说法 → 站内色板 hex；**认不出返回 None**（不猜、不回落）。

    刻意**不做子串/模糊匹配**：「天蓝」「浅蓝」含「蓝」，按子串会命中 `#1677ff`
    ——用户点名要的颜色被换成一个他没说的颜色，而这种错在界面上看不出来（色块
    画的是那个被换过的色）。认不出就不认，让调用方去说清可选的 8 种。
    """
    want = str(spec or "").strip()
    if not want:
        return None
    key = want.lstrip("#").lower()
    if want in _TAG_COLOR_ALIASES:
        return _TAG_COLOR_ALIASES[want]
    if key in _TAG_COLOR_ALIASES:
        return _TAG_COLOR_ALIASES[key]
    hexval = "#" + key
    return hexval if hexval in _COLOR_CANON else None


def color_cn(hexval: str) -> str:
    """色板 hex → 规范中文名；色板之外 → 空串（**不编名字**）。"""
    return _COLOR_CANON.get(str(hexval or "").strip().lower(), "")


def describe_color(hexval: str) -> str:
    """hex → 人话（`粉色（#eb2f96）`）；色板外只有色值可给。"""
    name = color_cn(hexval)
    return f"{name}（{hexval}）" if name else str(hexval or "")


STATUS_CN = {"public": "公开", "private": "私密", "draft": "草稿"}
_STATUS_ALIASES = {
    "public": "public", "published": "public", "公开": "public", "发布": "public",
    "publish": "public", "1": "public",
    "private": "private", "私密": "private", "隐藏": "private",
    "hidden": "private", "0": "private",
    "draft": "draft", "草稿": "draft", "drafted": "draft",
}

_TOP_ALIASES = {
    "1": 1, "true": 1, "yes": 1, "on": 1, "是": 1, "置顶": 1, "top": 1,
    "0": 0, "false": 0, "no": 0, "off": 0, "否": 0, "取消置顶": 0, "取消": 0,
    "不置顶": 0, "untop": 0,
}


def normalize_status(value) -> str | None:
    """任意写法 → 'public' | 'private' | 'draft'；认不出来 → None（调用方拒绝执行）。"""
    if value is None:
        return None
    key = str(value).strip().lower()
    return _STATUS_ALIASES.get(key)


def normalize_top(value) -> int | None:
    """任意写法 → 1 | 0；认不出来 → None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if value in (0, 1) else None
    return _TOP_ALIASES.get(str(value).strip().lower())


_VERDICT_ALIASES = {
    "pass": "pass", "approve": "pass", "approved": "pass", "ok": "pass", "yes": "pass",
    "通过": "pass", "放行": "pass", "显示": "pass", "过": "pass", "1": "pass",
    "reject": "reject", "rejected": "reject", "deny": "reject", "no": "reject",
    "驳回": "reject", "不通过": "reject", "未通过": "reject", "拒绝": "reject",
    "隐藏": "reject", "0": "reject",
}
BOARD_VERDICT_CN = {"pass": "通过", "reject": "驳回"}


def normalize_verdict(value) -> str | None:
    """任意写法 → 'pass' | 'reject'；认不出来 → None（调用方拒绝执行）。"""
    if value is None or isinstance(value, bool):
        return None
    return _VERDICT_ALIASES.get(str(value).strip().lower())


def parse_tag_ids(raw) -> list[int]:
    """Read unique signed-i32 IDs in source order; malformed values are not tags."""
    if raw is None:
        return []
    parts = raw if isinstance(raw, list) else str(raw).split(",")
    out = []
    for part in parts:
        value = normalize_target_id(part)
        if value is not None and value not in out:
            out.append(value)
    return out


class TagInfo:
    """一个标签的渲染/解析所需全部信息（两级共用同一 id 空间）。"""

    __slots__ = ("id", "name", "level", "father_id", "father_name", "color", "note_count")

    def __init__(self, id, name, level, father_id=None, father_name="", color="",
                 note_count=None):
        self.id = id
        self.name = name
        self.level = level
        self.father_id = father_id
        self.father_name = father_name
        self.color = color
        self.note_count = note_count

    @property
    def label(self) -> str:
        """展示名：一级 = 名字；二级 = `父 / 子`（与前端 flattenTagOptions 的标签一致）。"""
        if self.level == 2 and self.father_name:
            return f"{self.father_name} / {self.name}"
        return self.name

    def __repr__(self) -> str:  # 测试/日志可读
        return f"TagInfo(id={self.id}, label={self.label!r}, level={self.level})"


def build_tag_index(tags_one, tags_two) -> dict[int, TagInfo]:
    """两个公开标签接口的返回 → {id: TagInfo}。

    接口形态（src/routes/tags.rs）：一级 `{tagKey,title,color,level}`；
    二级 `{tagKey,title,color,level,fatherTag(父名),fatherKey(父 id)}`。
    **建树/归属一律按 fatherKey（父 id）**，不按 fatherTag（父名）——一级标签
    改名不该让子标签集体失联。
    """
    index: dict[int, TagInfo] = {}
    one_by_id: dict[int, str] = {}
    for t in (tags_one or []):
        if not isinstance(t, dict):
            continue
        tid = t.get("tagKey")
        if isinstance(tid, int) and tid > 0:
            name = str(t.get("title") or "").strip()
            index[tid] = TagInfo(tid, name, 1, color=_api_color(t),
                                 note_count=_api_count(t))
            one_by_id[tid] = name
    for t in (tags_two or []):
        if not isinstance(t, dict):
            continue
        tid = t.get("tagKey")
        if not (isinstance(tid, int) and tid > 0):
            continue
        fid = t.get("fatherKey")
        fid = fid if isinstance(fid, int) and fid > 0 else None
        index[tid] = TagInfo(tid, str(t.get("title") or "").strip(), 2,
                             fid, one_by_id.get(fid, "") if fid else "",
                             color=_api_color(t), note_count=_api_count(t))
    return index


def merge_tag_rows(tags_one, tags_two) -> list[dict]:
    """将两个公开标签接口合并为扁平两级清单，保留层级与颜色。"""
    one = [t for t in (tags_one or []) if isinstance(t, dict)]
    two = [t for t in (tags_two or []) if isinstance(t, dict)]
    kids: dict = {}
    for t in two:
        kids.setdefault(t.get("fatherKey"), []).append(t)
    rows: list[dict] = []
    for t in one:
        rows.append(t)
        rows.extend(kids.pop(t.get("tagKey"), []))
    for rest in kids.values():        # 悬空二级（父已删/读不到）不丢
        rows.extend(rest)
    return rows


def _api_color(tag: dict) -> str:
    """接口返回里的色值（`color` 字段，Rust 侧存 String 不校验）→ 规整的 `#rrggbb`。

    认不出形态就返回空串（渲染时只说名字）——**不猜**：把 `red` 说成 `#f5222d`
    是替后端编一个它没给的值。
    """
    raw = str(tag.get("color") or "").strip().lower()
    if not raw:
        return ""
    if not raw.startswith("#"):
        raw = "#" + raw
    return raw if re.fullmatch(r"#[0-9a-f]{6}", raw) else ""


def _api_count(tag: dict):
    """接口返回里的 `noteCount` → 非负整数；字段缺失/形态不对 → **None**。

    None 的语义是"这次读不到这个数"，不是 0（见 TagInfo.note_count）。只认整数
    与纯数字字符串：`12abc` 这种脏值当读不到，不学 `parseInt` 的截断语义。
    """
    if "noteCount" not in tag:
        return None
    raw = tag.get("noteCount")
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    s = str(raw).strip()
    return int(s) if re.fullmatch(r"\d+", s) else None


def render_tag_list(ids, index: dict[int, TagInfo] | None, limit: int = 6) -> str:
    """id 列表 → 人话（悬空 id 如实标注，**不静默吞掉**——那正是"标签莫名不见了"的来源）。

    `index=None` = **标签字典没读到**（不是"标签不存在"，是"这次读不到名字"）。两者
    必须分开说：把"读不到字典"渲染成「（已失效 id=12）」是在编造一个"这个标签已失效"
    的结论，而事实只是这会儿查不到名字。
    """
    ids = parse_tag_ids(ids)
    if index is None:
        return "、".join(f"id={i}" for i in ids[:limit]) if ids else "（无标签）"
    out = []
    for i in ids[:limit]:
        info = index.get(i)
        out.append(info.label if info else f"（已失效 id={i}）")
    head = "、".join(out)
    rest = len(ids) - limit
    if rest > 0:
        return f"{head} 等 {len(ids)} 个" if head else f"等 {len(ids)} 个"
    return head if head else "（无标签）"


