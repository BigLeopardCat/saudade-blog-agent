# -*- coding: utf-8 -*-
"""标签、分类、文章状态与后台文章列表的确定性层。"""

from __future__ import annotations

import re

# ── 标签配色（与前端 NEW_TAG_COLORS 同源同序；顺序变了颜色就全变）──────────
NEW_TAG_COLORS = ['#1677ff', '#52c41a', '#fa8c16', '#eb2f96',
                  '#722ed1', '#13c2c2', '#f5222d', '#a0d911']


def _utf16_units(text: str) -> list[int]:
    """字符串 → UTF-16 码元序列（等价 JS `str.charCodeAt(i)` 的逐个取值）。

    补充平面字符（emoji 等）在 JS 里是**两个**代理码元，用 Python 的 `ord()`
    只会得到一个码点 ⇒ 哈希不同 ⇒ 与前端算出不同颜色（同一个词在 agent 建的
    标签和用户手建的标签上会呈现两种颜色）。
    """
    raw = text.encode("utf-16-le")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def color_for_name(name: str) -> str:
    """按标签名取色（逐字符 UTF-16 码元滚动哈希，模 100000 后取模 8）。"""
    h = 0
    for code in _utf16_units(name):
        h = (h * 31 + code) % 100000
    return NEW_TAG_COLORS[h % len(NEW_TAG_COLORS)]


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
# hex → 规范中文名（与上面同源；NEW_TAG_COLORS 的顺序即展示顺序）
# 带「色」字：这张表是**回程渲染**用的（「颜色 粉色（#eb2f96）」），
# 单字「粉」在正文里会被读成半个词——匹配侧的别名表（_TAG_COLOR_ALIASES）
# 才是收口语说法的地方，两张表各管一头。
_COLOR_CANON: dict[str, str] = {
    "#1677ff": "蓝色", "#52c41a": "绿色", "#fa8c16": "橙色", "#eb2f96": "粉色",
    "#722ed1": "紫色", "#13c2c2": "青色", "#f5222d": "红色", "#a0d911": "黄绿色",
}
# 色板上第一处出现的"更适合当标签名"的别名（认色失败时颜色照给，名字宁可弱描述）
# —— 刻意不写"认不出来就不给颜色"：色值是确定的，名字只是表述。


# 给提示词/追问用的色板清单（顺序 = 展示顺序 = NEW_TAG_COLORS）
TAG_COLOR_SPEC = "、".join(
    f"{_COLOR_CANON[h]}（{h}）" for h in NEW_TAG_COLORS)


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


def resolve_tag_color(spec, name: str) -> str:
    """颜色说法 → 站内色板 hex；认不出（或压根没说）→ 按名字哈希取色。

    `color_for_name` 的既有契约（同名同色）在"没说颜色"时一字不变：这条路径
    就是它的兜底，前端手建标签走的是同一个哈希。
    """
    return match_tag_color(spec) or color_for_name(name)


def match_any_color(spec) -> str | None:
    """颜色实参 → 规整的 `#rrggbb`；认不出 → None（分类用，比标签宽一档）。"""
    picked = match_tag_color(spec)
    if picked:
        return picked
    s = str(spec or "").strip().lower()
    if not s:
        return None
    if not s.startswith("#"):
        s = "#" + s
    return s if re.fullmatch(r"#[0-9a-f]{6}", s) else None


def color_cn(hexval: str) -> str:
    """色板 hex → 规范中文名；色板之外 → 空串（**不编名字**）。"""
    return _COLOR_CANON.get(str(hexval or "").strip().lower(), "")


def describe_color(hexval: str) -> str:
    """hex → 人话（`粉色（#eb2f96）`）；色板外只有色值可给。"""
    name = color_cn(hexval)
    return f"{name}（{hexval}）" if name else str(hexval or "")


# ── 文章状态 ─────────────────────────────────────────────────────────
# 后台那三个单选值（frontend AllNotes 的 Radio：公开/私密/草稿）→ 存储值。
STATUS_CN = {"public": "公开", "private": "私密", "draft": "草稿"}
# 口语/别名 → 存储值。刻意宽容（planner 可能把用户说的"公开"原样当参数传下来），
# 但**只认这张表**：认不出来就拒绝，绝不猜（写错状态的代价比多问一句大得多）。
_STATUS_ALIASES = {
    "public": "public", "published": "public", "公开": "public", "发布": "public",
    "publish": "public", "1": "public",
    "private": "private", "私密": "private", "隐藏": "private",
    "hidden": "private", "0": "private",
    "draft": "draft", "草稿": "draft", "drafted": "draft",
}

# 置顶开关（note.is_top 是 tinyint，前台 Radio 是 是(1)/否(0)）。
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


def normalize_level(value) -> str | None:
    """层级实参 → `"one"` / `"two"`；认不出返回 None。

    词表与后端同源（`tags.rs::DeleteTagsRequest.level` / `parse_level` 用的就是
    这两个词）。顺带收口语说法（「一级」「1」）：planner 从用户话里摘出来的往往是
    中文词，让它卡在"必须拼对 one/two"上没有意义——认不出的**才**拒绝。
    """
    s = str(value or "").strip().lower()
    if s in ("one", "1", "一", "一级"):
        return "one"
    if s in ("two", "2", "二", "二级"):
        return "two"
    return None


def level_of(info: TagInfo | None) -> str | None:
    """TagInfo → `"one"` / `"two"`（None 进 None 出）。"""
    if info is None:
        return None
    return "one" if info.level == 1 else "two"


def normalize_top(value) -> int | None:
    """任意写法 → 1 | 0；认不出来 → None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if value in (0, 1) else None
    return _TOP_ALIASES.get(str(value).strip().lower())


def status_cn(value) -> str:
    """状态 → 中文（认不出来就把原值带引号吐回去，不假装它是个已知状态）。"""
    s = normalize_status(value)
    return STATUS_CN[s] if s else f"未知状态「{value}」"


def top_cn(value) -> str:
    t = normalize_top(value)
    return "置顶" if t == 1 else ("未置顶" if t == 0 else f"未知置顶值「{value}」")


_VERDICT_ALIASES = {
    "pass": "pass", "approve": "pass", "approved": "pass", "ok": "pass", "yes": "pass",
    "通过": "pass", "放行": "pass", "显示": "pass", "过": "pass", "1": "pass",
    "reject": "reject", "rejected": "reject", "deny": "reject", "no": "reject",
    "驳回": "reject", "不通过": "reject", "未通过": "reject", "拒绝": "reject",
    "隐藏": "reject", "0": "reject",
}
BOARD_VERDICT_APPROVED = {"pass": 1, "reject": 2}
BOARD_VERDICT_BODY = {"pass": 1, "reject": 0}
BOARD_VERDICT_CN = {"pass": "通过", "reject": "驳回"}
BOARD_VERDICT_FULL = {"pass": "通过（放行，所有访客都能看到）",
                      "reject": "驳回（隐藏，只有作者自己在「我的河灯」看到未通过）"}


def normalize_verdict(value) -> str | None:
    """任意写法 → 'pass' | 'reject'；认不出来 → None（调用方拒绝执行）。"""
    if value is None or isinstance(value, bool):
        return None
    return _VERDICT_ALIASES.get(str(value).strip().lower())


# ── note.tags 编解码（与前端 utils/noteTags.ts 逐条同源）───────────────

def parse_tag_ids(raw) -> list[int]:
    """读：脏值容错 → 去重后的正整数 id 列表（保序）。

    容忍 `"1,2"` / `""` / `"1,1,,"` / `[1,2]` / None / 数字与字符串混装。
    `parseInt('12abc')=12` 那种脏值悄悄当合法 id 的行为刻意不学（前端也改成了
    用 Number 严格判）——悬空/畸形的 id 会被当成真标签去解析名字，然后把
    「12abc」渲染成一个不存在的标签名。
    """
    if raw is None:
        return []
    parts = raw if isinstance(raw, list) else str(raw).split(",")
    out: list[int] = []
    for part in parts:
        if isinstance(part, bool) or part is None:
            continue
        if isinstance(part, int):
            n = part
        elif isinstance(part, str):
            s = part.strip()
            if not s or not re.fullmatch(r"[0-9]+", s):
                continue
            n = int(s)
        else:
            continue
        if n > 0 and n not in out:
            out.append(n)
    return out


def join_tag_ids(ids) -> str:
    """写：id 列表 → 后端要的字符串。空列表 → `''`（**语义是"清空标签"**）。

    唯一允许产出空串的地方——调用方必须先明确"用户要求清空"（见
    tools.set_article_tags 的 replace=[] 分支），其余路径一律不产出空串。
    """
    return ",".join(str(i) for i in parse_tag_ids(ids))


# ── 标签索引（名字 ↔ id）─────────────────────────────────────────────

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


def children_of(index: dict[int, TagInfo], father_id: int) -> list[TagInfo]:
    """某个一级标签下的二级标签（按 id 排序，名单稳定）。"""
    return sorted((t for t in index.values()
                   if t.level == 2 and t.father_id == father_id),
                  key=lambda t: t.id)


def count_phrase(n) -> str:
    """篇数 → 人话；**None（这次读不到）就一个字都不说**，绝不写成 0。

    这个区别在删除场景是要命的：「它挂在 0 篇文章上」与「没读到它挂在几篇上」
    给管理员的是两个完全相反的判断（前者让他放心点确定）。
    """
    return "" if n is None else f"{n} 篇"


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


def find_tag(index: dict[int, TagInfo], name: str, parent_id: int | None = None,
             level: int | None = None) -> tuple[TagInfo | None, list[TagInfo]]:
    """按名字找标签 → (唯一命中 | None, 同名候选列表)。

    判定 = 去空白后**完全相等**（不做包含/模糊匹配：给文章挂错标签是写错数据，
    而"没找到"只是多问一句）。`parent_id` 给了就只在那个父标签下找二级标签；
    `level`（1/2）给了就只在该层找（planner 能说清"改的是一级还是二级"时，
    用它消歧比追问一轮便宜）；都没给则两级都找——此时若命中多个（不同父下同名
    二级标签），返回 (None, 候选) 让调用方去追问，**不替用户选一个**。
    """
    want = (name or "").strip()
    if not want:
        return None, []
    if parent_id:
        hits = [t for t in index.values()
                if t.level == 2 and t.father_id == parent_id and t.name == want]
    elif level in (1, 2):
        hits = [t for t in index.values() if t.level == level and t.name == want]
    else:
        hits = [t for t in index.values() if t.name == want]
    if len(hits) == 1:
        return hits[0], hits
    return None, sorted(hits, key=lambda t: t.id)


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


# ── 渲染：后台文章列表 / 变更前后 ─────────────────────────────────────

# 后台文章清单一次最多印几行。**唯一一份**：渲染的缺省值与 `tools/note_admin.py`
# 交给跨轮摘要的那一格都取它——摘要要据此判"这份清单被截断了吗"（截断时行数比总数
# 少，状态分布只覆盖前 limit 条，报出来就是拿局部冒充全量）。
ADMIN_NOTES_LIMIT = 60


def render_admin_notes(notes, index: dict[int, TagInfo] | None,
                       limit: int = ADMIN_NOTES_LIMIT,
                       keyword: str = "", category: str = "") -> str:
    """渲染后台文章页，保留草稿、私密状态和分页范围；页内计数不冒充全库统计。"""
    kw = str(keyword or "").strip()
    cat = str(category or "").strip()
    sel = (f"「{cat}」分类下" if cat else "") + (f"匹配「{kw}」的" if kw else "")
    if sel:
        lines = [f"后台文章里{sel}共 {len(notes)} 篇"
                 f"（含草稿/私密；「编辑修改稿」不在其中）:"]
    else:
        lines = [f"后台文章共 {len(notes)} 篇（含草稿/私密；「编辑修改稿」不在其中）："]
    for n in notes[:limit]:
        if not isinstance(n, dict):
            continue
        title = str(n.get("noteTitle") or "").strip() or "（无标题）"
        if len(title) > 36:
            title = title[:36] + "…"
        marks = [status_cn(n.get("status"))]
        if normalize_top(n.get("isTop")) == 1:
            marks.append("置顶")
        tags = render_tag_list(n.get("noteTags"), index)
        # 分类只在**键在场**时才印：后台那两个端点回的是同一个 DTO，键恒在
        # ⇒ 键缺席只可能来自别的调用方（测试夹具/旧形载荷），那时不印这一格
        # （`cname` 为空串 ⟺ 键缺席，所以判它就够）。
        cname = "" if "categoryTitle" not in n else (
            str(n.get("categoryTitle") or "").strip() or "（无）")
        cat_txt = f" ｜分类：{cname}" if cname else ""
        # 命名空间名（`noteId=`）而不是裸 `id=`：这一屏是 **planner 的取值来源**，
        # 而它在别处看到的同族 id 都叫 `noteId`（帧出口统一改名，见
        # `tools/base.py::_FRAME_ID_KEYS`）。旧形 ` id=` 正是 id_namespaces 那批
        # 锁点名要清掉的东西——同一个数字两种写法，下一步的参数就可能填错族。
        lines.append(f"- noteId={n.get('noteKey')} [{'/'.join(marks)}]《{title}》"
                     f"标签：{tags}{cat_txt}")
    if len(notes) > limit:
        rest = len(notes) - limit
        lines.append(f"（另有 {rest} 篇匹配「{kw}」的未列出，把关键词写具体些再看）" if kw
                     else (f"（另有 {rest} 篇「{cat}」分类下的未列出）" if cat
                           else f"（另有 {rest} 篇未列出，可用 keyword 收窄）"))
    return "\n".join(lines)


# ── 渲染：写操作的返回（工具文本 + 回执 meta）──────────────────────────

def level_cn(info: TagInfo) -> str:
    return "二级" if info.level == 2 else "一级"


def render_tag_reuse(info: TagInfo) -> str:
    """显示复用标签的现有颜色，不将用户请求颜色当成已生效颜色。"""
    color = f"，现有颜色：{describe_color(info.color)}" if info.color else ""
    return (f"标签「{info.label}」已经存在（id={info.id}，{level_cn(info)}{color}），"
            f"直接复用它，没有新建。")


def render_tag_created(info: TagInfo) -> str:
    """显示已创建标签的名称、颜色名与色值。"""
    color = f"，颜色：{describe_color(info.color)}" if info.color else ""
    return (f"已新建{level_cn(info)}标签「{info.label}」（id={info.id}{color}）。"
            f"它目前还挂在标签字典里、没有挂到任何文章上——"
            f"要挂到某篇文章上，告诉我挂哪一篇就行。")


def render_status_ok(aid: int, title: str, before: str, after: str) -> str:
    """写成功的人话。**标题只出现在这段文本里**（给 narrator 看懂改的是哪篇），
    不进回执 meta——回执会经 execution_log 注入下一轮的上下文，带《标题》容易被
    读成"我读过这篇文章"的证据（rule 6b 的取值指代）。"""
    head = f"已修改文章 {aid}"
    if title:
        head += f"《{title}》"
    return f"{head}：{before} → {after}（后台已复核读到新值）"


def render_tags_ok(aid: int, title: str, before: str, after: str) -> str:
    head = f"已修改文章 {aid}"
    if title:
        head += f"《{title}》"
    return f"{head} 的标签：{before} → {after}（后台已复核读到新值）"


def render_change(pairs) -> tuple[str, str]:
    """[(前, 后), …] → ("私密 / 未置顶", "公开 / 未置顶")。

    只渲染**本次真的动了的字段**：一个只改置顶的操作，写成"私密 → 公开"就是
    把没发生的事记进了跨轮执行记忆（下一轮 narrator 会照着念）。
    """
    before = " / ".join(str(b) for b, _ in pairs)
    after = " / ".join(str(a) for _, a in pairs)
    return before, after


def clip(text: str, limit: int = 60) -> str:
    """截断密表和回执字段，避免超过后端列宽；用户正文与模型事实另按自身契约处理。"""
    s = str(text)
    return s if len(s) <= limit else s[:limit] + "…"



def article_where(notes: dict[int, dict] | None, article_id, *, include_status: bool = True) -> str:
    """用已读取的文章标题展示目标；公开状态只在相关操作中展示。

    没有快照时返回空串；快照缺少目标时明确说明，避免生成无法核对的指称。
    """
    if notes is None:
        return ""
    try:
        rid = int(article_id)
    except (TypeError, ValueError):
        return ""
    row = notes.get(rid)
    if row is None:
        return "（后台清单里没有这一篇）"
    title = clip(str(row.get("noteTitle") or "").strip() or "（无标题）", 36)
    if not include_status:
        return f"《{title}》"
    marks = [status_cn(row.get("status"))]
    if normalize_top(row.get("isTop")) is not None:
        marks.append(top_cn(row.get("isTop")))
    return f"《{title}》（现在：{'、'.join(marks)}）"


def tag_note_phrase(info: TagInfo | None) -> str:
    """「它挂在 N 篇文章上」；读不到篇数 → 空串（不说）。"""
    if info is None or info.note_count is None:
        return ""
    return f"它目前挂在 {info.note_count} 篇文章上"


def render_tag_updated(label: str, before: str, after: str) -> str:
    return f"已修改标签「{label}」：{before} → {after}（后台已复核读到新值）"


def render_tag_deleted(label: str, extra: str = "") -> str:
    tail = f"：{extra}" if extra else ""
    return f"已删除标签「{label}」{tail}（后台已复核：标签字典里已经没有它）"


def render_tag_moved(label_before: str, label_after: str, extras=()) -> str:
    """换层级 / 换父级成功。**前后两个展示名都写出来**——「已移动」而不说移到
    哪里，等于让用户自己回去核对。`extras` 是影响面（引用被改写多少篇等）。"""
    head = (f"已把标签「{label_before}」调整为「{label_after}」"
            if label_after != label_before else f"标签「{label_before}」已在目标位置")
    tail = "；".join(x for x in (extras or []) if x)
    return f"{head}（后台已复核读到新值）" + (f"。{tail}" if tail else "")


def move_impact(res: dict) -> list[str]:
    """移动端点的返回 → 影响面的人话（只说不为空的那几条）。

    `idChanged` 是**这个操作最需要交代的事**：id 变了意味着所有引用它的文章都被
    改写过（`rewrittenNotes` 行），而文章列表、sitemap 的排序不受影响（`updated_at`
    被显式保留）。`warnings` 由 Rust 侧生成（同名父标签/同名兄弟/提交后仍有残留），
    原样透出——它们是"做成了，但有话要说"。
    """
    out: list[str] = []
    if res.get("idChanged"):
        n = res.get("rewrittenNotes")
        out.append(f"标签 id 由 {res.get('fromId')} 变成 {res.get('toId')}"
                   + (f"，{n} 篇文章上的引用已同步改写" if isinstance(n, int) else ""))
    warn = res.get("warnings")
    if isinstance(warn, list):
        out.extend(str(w) for w in warn if str(w or "").strip())
    return out



class CategoryInfo:
    """一个分类的渲染/解析所需信息（`GET /api/category` 的行）。"""

    __slots__ = ("id", "name", "path_name", "introduce", "icon", "color", "note_count")

    def __init__(self, id, name, path_name="", introduce="", icon="", color="",
                 note_count=None):
        self.id = id
        self.name = name
        self.path_name = path_name
        self.introduce = introduce
        self.icon = icon
        self.color = color
        self.note_count = note_count      # None = 这次读不到（≠ 0），同上

    def __repr__(self) -> str:  # 测试/日志可读
        return f"CategoryInfo(id={self.id}, name={self.name!r})"


def build_category_index(rows) -> dict[int, CategoryInfo]:
    """`GET /api/category` 的返回 → {id: CategoryInfo}。

    分类**没有层级**（一张平表），所以这里不需要 build_tag_index 那种父子建树，
    只要 id → 名字 + 篇数。`noteCount` 口径 = 该分类下**非修改稿**的文章数
    （Rust 侧已滤 `draft_of is null`，见 categories.rs）。
    """
    index: dict[int, CategoryInfo] = {}
    for c in (rows or []):
        if not isinstance(c, dict):
            continue
        cid = c.get("categoryKey")
        if not (isinstance(cid, int) and cid > 0):
            continue
        index[cid] = CategoryInfo(
            cid, str(c.get("categoryTitle") or "").strip(),
            path_name=str(c.get("pathName") or "").strip(),
            introduce=str(c.get("introduce") or "").strip(),
            icon=str(c.get("icon") or "").strip(),
            color=_api_color(c), note_count=_api_count(c))
    return index


def find_category(index: dict[int, CategoryInfo], name: str):
    """按名字找分类 → (唯一命中 | None, 同名候选列表)。

    判据与 `find_tag` 同源：去空白后**完全相等**，不做模糊。分类名没有唯一约束
    （`category.name` 无 UNIQUE），所以"重名"是真会出现的形态——命中多个时返回
    (None, 候选) 让调用方追问，**不替用户挑一个**（删错分类会连带让一批文章失去
    分类）。"""
    want = (name or "").strip()
    if not want:
        return None, []
    hits = [c for c in index.values() if c.name == want]
    if len(hits) == 1:
        return hits[0], hits
    return None, sorted(hits, key=lambda c: c.id)


def render_category_created(info: CategoryInfo) -> str:
    """新建分类成功。**不带任何工具名**（同 render_tag_created 的血案：工具名会被
    narrator 照抄成"我调用了它"，而它不在本轮执行集里 ⇒ 具名声称闸判编造）。
    也不报篇数：刚建的分类必然 0 篇，报出来是废话（更糟的是把"0"念成了影响面）。"""
    extra = f"，路径 {info.path_name}" if info.path_name else ""
    return (f"已新建分类「{info.name}」（id={info.id}{extra}）。"
            f"要往这个分类里放文章，告诉我放哪几篇就行。")


def render_category_updated(name: str, before: str, after: str) -> str:
    return f"已修改分类「{name}」：{before} → {after}（后台已复核读到新值）"


def render_category_deleted(name: str, note_count=None) -> str:
    """删除分类成功。篇数**只有读到了才说**：`note_count` 是删之前统计的公开文章数，
    删完之后那些文章的 category_id 被置 NULL（FK ON DELETE SET NULL，文章还在，
    只是没有分类了）。"""
    tail = f"，{note_count} 篇文章已变成没有分类" if note_count is not None else ""
    return f"已删除分类「{name}」{tail}（后台已复核：分类列表里已经没有它）"
