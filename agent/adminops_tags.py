# -*- coding: utf-8 -*-
"""标签、分类、文章状态与后台文章列表的确定性层。

批 3a 从 `agent/adminops.py` 按域拆出（原文逐字节保留）。这里住的是配色表、
中文色名、状态/置顶的归一、`note.tags` 编解码、标签与分类的"名字↔id"解析，
以及这一族写操作的卡面与回执。三条与前端同源的契约见 `note.tags` 编解码那一节。"""

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


# ── 中文色名 ↔ 站内 8 色板（20260921 颜色预览）────────────────────────
# 用户拍板：「站内 8 色板 + 中文色名映射」——**不开放任意色值**。理由是这张表
# 同时是前端色块装饰器的白名单（`chatMarkdown.ts::chatColorPalette`）：色板外的
# 色值不画色块，所以"agent 说得出"的色必须就是"前端画得出"的色，两边同源。
#
# 规范名（`_COLOR_CANON` 的值）是**回程渲染用**的名字，与别名分开：
# `黄` 落在 `#a0d911`（站内没有正黄），回程必须说「黄绿」——用户说"黄"可以，
# 但系统回一句"颜色：黄（#a0d911）"就是在骗他（屏幕上明明是黄绿）。
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
    """颜色实参 → 规整的 `#rrggbb`；认不出 → None（分类用，比标签宽一档）。

    与 `match_tag_color` 的差别：**任意 6 位 hex 都收**。理由是前端色块白名单
    已经放宽成"任意 6 位 hex 都画色块"（20260921 父仓 ec6b090），所以"agent 说得
    出、前端画不出"这一档不一致在分类上不存在；标签侧仍走 8 色板（那张表同时是
    中文色名的映射源，用户点名的"粉色"必须落到同一个色值上）。
    """
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


# ── 河灯留言的人工复核（20260922 第六轮）────────────────────────────
# 三个词表的分工必须分清（数字方向传反 = 该驳回的给放行了，是这套写面里
# 后果最重的一类错）：
#   · `normalize_verdict`：模型说的话 → "pass" / "reject"（认不出 → None，调用方拒绝）
#   · `BOARD_VERDICT_APPROVED`：DB 里 approved 的目标值（1 通过 / 2 未通过；0 是**待审**）
#   · `BOARD_VERDICT_BODY`：审核端点的**请求体**值（1 通过 / **0 驳回**）——端点内部
#     把 0 落成 approved=2。两个数字表刻意分开写：合并成一张表就等着有人把
#     "reject → 2" 直接发进请求体（那会让端点把 2 当"通过"放行）。
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
            # ⚠️ **ASCII** 数字类，不是 `\d`（20261013）：`\d` 在 Python 里默认认
            # Unicode 十进制数字——`"１２"`（全角）、`"٣"`、`"१२"` 都会命中，而前端的
            # `Number()` **一个都不认**（`frontend/src/utils/noteTags.ts::toTagId`）。
            # 于是这一侧会把它念成 id=12 的标签、屏幕上却没有那个标签（同
            # `_COLOR_CANON` 那族"说得出、画不出"）。判据 =
            # `tests/test_note_tags_codec.py` ④：**本侧认下的每一个值，前端也认**。
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
        # 站内色板色值（20260921）；接口没给色时为空串——渲染时只说名字，
        # **不拿哈希补一个**（那是"编一个它其实没有的颜色"）
        self.color = color
        # 挂在这个标签上的**公开可见**文章数（20260921，接口 `noteCount`）。
        # **None ≠ 0**：接口没给这个字段时是 None（"这次读不到"，删除/移动的影响面
        # 就不报数字——把读不到渲染成"0 篇文章"，用户会以为删掉它无损失）。
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
    """两个公开标签接口 → **一份扁平的两级清单**（20260921）。

    为什么：`list_tags` 此前只读 `/tagone` ⇒ 结构上就看不见二级标签
    （164641 实测答"站内没有二级标签"——数据一直在 `/tagtwo`，缺的只是把它读出来）。

    形态：一级行原样 + 紧随其后的子行（`/tagtwo` 的行自带 `level:2/fatherTag/fatherKey`，
    字段名一个不改），父 id 对不上任何一级标签的**悬空二级**行排在最后——静默吞掉
    它们正是"标签莫名不见了"的来源（同 render_tag_list 对悬空 id 的处置）。
    两级混在一张表里靠 `level` 区分，narrator 照读即可。

    返回值保持**纯字面量**（ToolsResult 出口用 `_shape` = `str()`、跨轮实体摘要用
    `ast.literal_eval` 回读）——渲染成人话会让实体摘要静默失效。
    """
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
        # 折叠计数是**后缀**不是并列项："Python、架构 等 4 个"（曾经被当成第 3 个
        # 标签 join 成 "Python、架构、等 4 个"——读起来像真有个叫"等 4 个"的标签）。
        return f"{head} 等 {len(ids)} 个" if head else f"等 {len(ids)} 个"
    return head if head else "（无标签）"


# ── 渲染：后台文章列表 / 变更前后 ─────────────────────────────────────

def render_admin_notes(notes, index: dict[int, TagInfo] | None, limit: int = 60,
                       keyword: str = "", category: str = "") -> str:
    """后台文章列表（含草稿与私密）→ 给 planner 看的清单。

    这一屏的价值全在**id 与标题的对应**：管理员接着说"把《X》设为私密"时，
    planner 只有在这一轮真读到了 id，才有据可写（execute 层的目标校验会拦
    "没读过就写一个凭记忆的 id"）。

    `keyword` / `category` 给了就改说"匹配 N 篇"/「X」分类下共 N 篇（**筛过的口径，
    不是总数**）：这张单子可能是从全站文章里筛出来的一小撮，写成"后台文章共 N 篇"
    会让读的人把筛出来的条数当成站内总量（缺数 ≠ 零的同族错误，只是方向反过来）。

    **每行末尾印分类名**（20261009）：`categoryTitle` 本来就在 DTO 里（Rust
    `NoteDto`），只是这一屏此前没印 ⇒ 模型就算把整张清单读回来，也**看不出哪几篇
    归在某个分类下**（生产现场 trace `20261008T234509`：主人要"把「测试」分类下的
    文章全部转私密"，模型只能按 keyword 搜出标题带"测试"的 8 篇，然后如实说手上
    没有那份清单）。键在不在场照旧是判据：**键缺席 = 这一路没带分类信息 ⇒ 不印那一格**
    （缺键不编），键在但为空 = 这篇文章真的没有分类 ⇒ 印「（无）」。
    """
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
        # 尾句此前写着「可用关键词检索」而**关键词参数根本不存在**——那是一句把读的
        # 人（和模型）指向空处的假话。现在 keyword 真在（`list_admin_notes`），两句
        # 分开说：筛过的说"换个词/写具体些"，没筛的才提关键词这条路。
        lines.append(f"（另有 {rest} 篇匹配「{kw}」的未列出，把关键词写具体些再看）" if kw
                     else (f"（另有 {rest} 篇「{cat}」分类下的未列出）" if cat
                           else f"（另有 {rest} 篇未列出，可用 keyword 收窄）"))
    return "\n".join(lines)


# ── 渲染：写操作的返回（工具文本 + 回执 meta）──────────────────────────

def level_cn(info: TagInfo) -> str:
    return "二级" if info.level == 2 else "一级"


def render_tag_reuse(info: TagInfo) -> str:
    """已存在 → 复用。带上它**现有的**颜色（20260921）：用户说"建个粉色的 X"而
    X 已存在且是蓝色时，narrator 得说得出这个差别（否则回复里给个"粉色"、屏幕上
    却是个蓝标签）。颜色由接口给，没给就只说名字。"""
    color = f"，现有颜色：{describe_color(info.color)}" if info.color else ""
    return (f"标签「{info.label}」已经存在（id={info.id}，{level_cn(info)}{color}），"
            f"直接复用它，没有新建。")


def render_tag_created(info: TagInfo) -> str:
    """新建成功的人话。**颜色名 + 色值一起给**（20260921）：narrator 只有拿到
    这一对，才能在回复里同时写出"粉色"和 `#eb2f96`——前端据此画色块（色块由
    前端按 hex 画，模型不画符号）。色值缺失时只说名字，不补一个。

    **这段文本里不许出现任何工具名**（20260921 生产事故）：原尾句是
    「要挂到文章上用 set_article_tags」——narrator 照抄了它，而 5c 具名声称闸
    判定"点名了本轮没执行的工具" ⇒ 整条回复被换成"这一轮什么都没执行"，
    与该标签**真的建成了**这件事当面矛盾（165645）。工具名是系统内部词汇，
    一句给访客看的人话里出现它，就等着被复述成"我调用了它"。同源 lint 见
    tests/test_workflows.py::test_no_tool_name_in_user_facing_text。"""
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
    """**密表/帧**里的字段截断（Rust 侧 detail 列宽有限，先在这边收口，避免被截在
    半截字符上）。

    ⚠️ **不许拿它量确认卡的面**（20260926 的现场）：卡面、以及"下一轮 narrator 唯一
    取值来源"的那条回执行，都必须印主人要核对的那段字**全文**——`clip` 截了还不带
    节选标注，读起来就是完整的一句。真实现场（trace `20260926T094843`）：主人要记的
    待办 150 字，卡面与回执都给了「…/20260926/2…」，narrator 只能照着念，主人问
    "为什么和我要的内容不一样、显示截断了"。要截的一面必须有**明确的节选标注**
    （正面例子见 `render_notice_status`：「（节选，共 N 字）」）。
    """
    s = str(text)
    return s if len(s) <= limit else s[:limit] + "…"


# ── 渲染：改标签 / 删标签（20260921 第四轮）──────────────────────────
# 这批写操作的共同点：**影响面是"数据"而不只是"这一行"**。删一个一级标签会连带
# CASCADE 掉它的子标签、并把所有文章上的引用摘掉（`prune_note_tags`，**不可回滚**）；
# 所以这几段文本的第一职责是把影响面**说全**：管理员点「确定」之前有权知道会动到
# 多少篇文章、会不会连带删掉别的标签。数字全部来自标签/分类字典，**读不到就一个字
# 都不说**（绝不写 0——那是"删了没损失"的错误结论，见 count_phrase）。

def article_where(notes: dict[int, dict] | None, article_id) -> str:
    """文章在问句里的**可核对指称**：`《标题》（现在：草稿、未置顶）`。

    20260922 第七轮：这之前文章写操作的问句**只有内部编号**（「修改文章 46」），是
    整套写面里唯一的盲签——标签/分类/公告/留言都写了名字，只有文章让主人在看不到
    标题、看不到现状的情况下点「确定」。而"确认框点一下"正是文章这类写操作**唯一**
    的人类兜底（评估意见："有证据 ≠ 目标唯一"），所以这一句必须能让主人自己认出来
    "是不是我说的那篇"。

    `notes is None`（这次没读到清单）→ **空串**：退回原来的「修改文章 46」，
    **绝不因此不弹窗**（那会退回"判成分歧就追问"的死路形态）。id 不在清单里
    （编辑修改稿行/已删除）→ 如实写出来，主人一眼能看出系统没对上一篇真实的文章。
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


# ── 分类（20260921 第四轮）：与标签同一条"名字↔id"的解析纪律 ──────────

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
