# -*- coding: utf-8 -*-
"""后台写操作的纯函数层（20260921，管理助手第二轮：写）。

与 agent/reports.py、agent/hostinfo.py 同一条纪律：**能算的都不交给 LLM**。
这里放的是"把后台数据翻译成人话、把关名字↔id 的对应、判参数是否合法、算出
变更前→变更后"这类确定性逻辑；tools/base.py 里的 @tool 只做 IO 与拼装。

无网络、无 LLM、无状态：test_admin_write.py 全部秒级复跑。

三条与前端**同源**的契约（改一侧必须同步另一侧，否则同一件事在两个界面上
表现不一致——前端那两处是 `frontend/src/utils/noteTags.ts` 与
`frontend/src/components/NoteTagSelect/index.tsx`）：

  ① `note.tags` 的编解码 = **逗号分隔的正整数 id 串**（`"12,10000"`）。
     读要容忍一切脏值（`"1,1,,"`），写要走同一个出口（去重、丢非法、保序）。
     `noteTags: ""` 真的是"清空标签"，所以只有调用方明确要清空时才允许产出空串。
  ② 标签 id 是**两级共用**的一个扁平空间（`tag_one.id` 与 `tag_two.id` 各自自增，
     迁移 tag_autoincrement_20260919 之后二级从 10000 起，不再撞号）。
  ③ 新建标签的配色 = 按名字哈希取色（同名永远同色）。哈希逐字符走 **UTF-16
     码元**（SQL 侧的 JS `charCodeAt` 语义）：BMP 字符下等价于 `ord()`，但 emoji
     这类补充平面字符在 JS 里是两个代理码元——用 `ord()` 算会与前端算出**不同的
     颜色**，所以这里显式按 UTF-16 解，不做"中文够用就行"的近似。
"""

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
            if not s or not re.fullmatch(r"\d+", s):
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

    __slots__ = ("id", "name", "level", "father_id", "father_name", "color")

    def __init__(self, id, name, level, father_id=None, father_name="", color=""):
        self.id = id
        self.name = name
        self.level = level
        self.father_id = father_id
        self.father_name = father_name
        # 站内色板色值（20260921）；接口没给色时为空串——渲染时只说名字，
        # **不拿哈希补一个**（那是"编一个它其实没有的颜色"）
        self.color = color

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
            index[tid] = TagInfo(tid, name, 1, color=_api_color(t))
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
                             color=_api_color(t))
    return index


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


def find_tag(index: dict[int, TagInfo], name: str,
             parent_id: int | None = None) -> tuple[TagInfo | None, list[TagInfo]]:
    """按名字找标签 → (唯一命中 | None, 同名候选列表)。

    判定 = 去空白后**完全相等**（不做包含/模糊匹配：给文章挂错标签是写错数据，
    而"没找到"只是多问一句）。`parent_id` 给了就只在那个父标签下找二级标签；
    没给则两级都找——此时若命中多个（不同父下同名二级标签），返回 (None, 候选)
    让调用方去追问，**不替用户选一个**。
    """
    want = (name or "").strip()
    if not want:
        return None, []
    if parent_id:
        hits = [t for t in index.values()
                if t.level == 2 and t.father_id == parent_id and t.name == want]
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

def render_admin_notes(notes, index: dict[int, TagInfo] | None, limit: int = 60) -> str:
    """后台文章列表（含草稿与私密）→ 给 planner 看的清单。

    这一屏的价值全在**id 与标题的对应**：管理员接着说"把《X》设为私密"时，
    planner 只有在这一轮真读到了 id，才有据可写（execute 层的目标校验会拦
    "没读过就写一个凭记忆的 id"）。
    """
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
        lines.append(f"- id={n.get('noteKey')} [{'/'.join(marks)}]《{title}》标签：{tags}")
    if len(notes) > limit:
        lines.append(f"（另有 {len(notes) - limit} 篇未列出，可用关键词检索）")
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
    test_skills.py::test_no_tool_name_in_user_facing_text。"""
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
    """回执字段截断（Rust 侧 detail 列宽有限，先在这边收口，避免被截在半截字符上）。"""
    s = str(text)
    return s if len(s) <= limit else s[:limit] + "…"


# ── 写操作确认框（20260921）：问句与回复文本都是**确定性中文**────────────
# 与 agent/reports.py 同一条纪律：能算的都不交给 LLM。这两段文本会直接进
# ①确认框的问题行 ②那一轮的对话气泡，都是用户一眼看到的东西——让模型写它，
# 就又多了一处"它可能把没执行的说成已执行"的地方（而这一轮恰好什么都没执行）。
def _name_list(value) -> list[str]:
    """spec 里的标签名列表 → 干净字符串列表（渲染用；非列表/空值一律当空）。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(x).strip() for x in value if str(x or "").strip()]


def _confirm_one(spec: dict, index=None) -> str:
    """单条写 spec → 「做什么」的人话（与 server._tool_action_text 同口径）。

    `index` = 可选的标签字典（`{id: TagInfo}`，见 build_tag_index）：给得起就
    **把父标签的名字写进问句**，给不起退回 id。写二级标签时只写「新建二级标签
    「Rust」」等于让用户盲签——他看不到这个 Rust 会挂到哪个爸爸底下，而"挂错
    父标签"正是本轮要修的参数对调事故（20260921）。名字比 id 可靠：id 是系统
    内部编号，用户点确定时没法核对。
    """
    tool = str(spec.get("tool") or "")
    a = spec.get("args") or {}
    if tool == "create_tag":
        title = str(a.get("title") or "").strip() or "（未命名）"
        pid = str(a.get("parent_id") or "").strip()
        level = "二级" if pid else "一级"
        where = ""
        if pid:
            parent = None
            try:
                parent = (index or {}).get(int(pid))
            except Exception:
                parent = None
            where = f"（挂在「{parent.name}」下）" if parent is not None else f"（挂在标签 id={pid} 下）"
        hexval = match_tag_color(a.get("color")) if a.get("color") else None
        color = f"，颜色 {describe_color(hexval)}" if hexval else "（按名字自动配色）"
        return f"新建{level}标签「{title}」{where}{color}"
    if tool == "set_article_status":
        head = f"修改文章 {a.get('article_id')}"
        bits = []
        st = normalize_status(a.get("status"))
        if st:
            bits.append(f"状态改为 {STATUS_CN.get(st, st)}")
        top = normalize_top(a.get("is_top"))
        if top is not None:
            bits.append("置顶" if top == 1 else "取消置顶")
        return f"{head}：{'、'.join(bits)}" if bits else head
    if tool == "set_article_tags":
        # 把**动的是哪几个标签**写进问句（20260921 第三轮）：只说「修改文章 12 的
        # 标签」，用户没法核对"去掉的到底是不是我说那个"——同族于本函数上面那句
        # 「说漏了颜色/哪一篇就是盲签」（探针 ⑤ 的现场：主人说摘「摄影」，若 planner
        # 填了别的标签，问句里看不出来）。
        bits = []
        add = _name_list(a.get("add"))
        rem = _name_list(a.get("remove"))
        if add:
            bits.append("加上 " + "、".join(add))
        if rem:
            bits.append("去掉 " + "、".join(rem))
        if "replace" in a:
            rep = _name_list(a.get("replace"))
            bits.append(f"整体换成 {'、'.join(rep)}" if rep else "清空全部标签")
        tail = "：" + "、".join(bits) if bits else ""
        return f"修改文章 {a.get('article_id')} 的标签{tail}"
    return f"执行 {tool}"


def render_confirm_question(specs, index=None) -> str:
    """确认框的问题行：**把要发生的事说全**（含颜色名与色值），再问一句。

    用户点的是"确定"，他有权在点之前从这句话里看出自己将同意什么——
    说漏了颜色、说漏了是哪一篇、**说漏了挂在哪个父标签下**，这个按钮就变成了盲签。
    （`index` 见 _confirm_one；读不到标签字典时退化成 id，不因此不弹窗。）
    """
    acts = "；".join(_confirm_one(s, index) for s in (specs or []))
    return f"要{acts}吗？点「确定」我就去办。"


def render_confirm_text(specs, index=None) -> str:
    """弹窗那一轮的**对话气泡正文**（系统给的，不经 narrator）。

    刻意写得像"在等你的意思"而不是"已经在办了"：这一轮零执行。给一个明确
    的操作路径（点按钮 / 直接打字），两条路都通向同一条写通道。
    """
    acts = "；".join(_confirm_one(s, index) for s in (specs or []))
    return (f"好呀，这一步要动到站内数据，我先跟你确认一下：\n\n"
            f"**{acts}**\n\n"
            f"点上面的「确定」我就去办；不想改了就把这个框关掉，"
            f"或者直接告诉我改成别的。")


# ── 后台写：目标校验（"没读到过就不许写"）────────────────────────────
# 形态与 agent/authz.py 的三件套（REASON_* / *_frame / *_error_reason）一致——
# 原因码是 planner 的输入（rule5 按原因码决定改参还是去问），帧是给 planner 读的，
# 取回函数给 checker 用（判 BLOCK 并把原因码带进 blocked 链路）。
REASON_UNKNOWN_TARGET = "unknown_target"
# 目标与用户点名不一致（20260921 第三轮）：与 unknown_target 分开——那条是"根本没读到过"，
# 这条是"读到了、但读的是别的"（planner 把清单第一行当成了用户点的那一篇）。
REASON_TARGET_MISMATCH = "target_mismatch"


_NAMED_ARTICLE_RES = re.compile(r"(?:文章|笔记|帖子|文档)\s*[#＃No.、]?\s*(\d{1,7})")
_NAMED_ORDINAL_RES = re.compile(r"第\s*(\d{1,7})\s*(?:篇|章|条|个)")
_NAMED_ID_RES = re.compile(r"\bid\s*[=:：]?\s*(\d{1,7})", re.I)
# 枚举延伸（"文章 12 和 14"/"文章 12、13"）：第一个点名之后紧跟连接词的数字
# 也算点名——**漏认的代价是不对称的**：少认一个，合法命令里那第二篇会被
# target_mismatch 判成"改错篇"而拒执行（用户点了三篇只改一篇）；多认一个只是
# 放宽（命中任一即算对上）。故宁可延伸，但**量词守卫照旧**（"文章 12 和 3 个要点"
# 里的 3 仍是计数，不是 id）。
_NAMED_LIST_ITEM_RE = re.compile(r"\s*(?:和|与|跟|及|以及|还有|、|,|，)\s*[#＃]?\s*(\d{1,7})")
_COUNT_TAIL_RE = re.compile(r"^\s*(?:篇|个|条|次|字|行|块|张|页)")


def user_named_article_ids(user_msg: str) -> set[int]:
    """用户**本轮原话里点名**的文章 id 集合——写侧目标的唯一权威（空集 = 没点名）。

    只认明确指称，不猜：`文章 12` / `文章#12` / `第 12 篇` / `id=12`；同义名词
    （笔记/帖子/文档）一并认；**枚举也认**（"文章 12 和 14"、"文章 12、13"，
    见 `_named_list_tail`）——但必须有**第一个带标记的点名**打头，裸数字不猜。
    宽松的方向是有意的：多认一个只是放宽（命中任一即算对上），少认一个却会让
    合法命令里那第二篇被判成"改错篇"而拒执行。**计数形态不算**：数字在名词之前
    （"读了 12 篇文章"）结构上不匹配，"文章 12 篇"/"文章 12 和 3 个要点"这种后面
    跟量词的显式排除（见 `_COUNT_TAIL_RE`），否则"这篇文章 3 个要点"会被读成
    点了 id=3。
    """
    text = str(user_msg or "")
    out: set[int] = set()
    for rx in (_NAMED_ARTICLE_RES, _NAMED_ORDINAL_RES, _NAMED_ID_RES):
        for m in rx.finditer(text):
            try:
                n = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if n <= 0 or n > 9_999_999:
                continue
            if rx is _NAMED_ARTICLE_RES and _COUNT_TAIL_RE.match(text[m.end():m.end() + 2]):
                continue  # 「文章 12 篇」= 计数，不是目标
            out.add(n)
            if rx is _NAMED_ARTICLE_RES:
                out |= _named_list_tail(text, m.end())
    return out


def _named_list_tail(text: str, pos: int) -> set[int]:
    """点名之后的枚举延伸（"文章 12 **和 14**"）——从 pos 起逐个吃，断了就停。

    只在第一个点名**成立**之后调用（量词守卫已过），逐项仍要过量词守卫，否则
    "文章 12 和 3 个要点里的那篇"会把 3 认成文章 id。
    """
    out: set[int] = set()
    while True:
        m = _NAMED_LIST_ITEM_RE.match(text, pos)
        if not m:
            return out
        if _COUNT_TAIL_RE.match(text[m.end():m.end() + 2]):
            return out
        n = int(m.group(1))
        if 0 < n <= 9_999_999:
            out.add(n)
        pos = m.end()


def target_named(article_id, named: set[int]) -> bool:
    """id 是否落在用户点名的集合里。**空集 = 判据不启用**（恒 True）。"""
    if not named:
        return True
    try:
        return int(article_id) in named
    except (TypeError, ValueError):
        return False


def target_mentioned(article_id, texts) -> bool:
    """文章 id 是否在**本轮可见的材料**里作为独立数字出现过。

    `(?<!\\d)N(?!\\d)` 两端都不许挨着数字：id=12 不能被 "id=123"、"1912" 里的
    子串冒充命中（那正是"凭记忆写了个 id，恰好与列表里另一篇的编号部分重合"）。

    这是**供方**判据（调用方把本轮读过的帧 + 页面上下文 + 用户消息拼成 texts），
    不是"id 一定对"的保证——它拦的是"整轮什么都没读却写一个凭记忆的 id"。
    """
    try:
        aid = int(article_id)
    except (TypeError, ValueError):
        return False
    if aid <= 0:
        return False
    rx = re.compile(rf"(?<!\d){aid}(?!\d)")
    return any(rx.search(str(t)) for t in texts if t)


def unknown_target_frame(name: str) -> str:
    """目标无据时的错误帧（checker 判 BLOCK，planner 先读再写）。"""
    return (f"__ERROR__: 目标未经确认[{REASON_UNKNOWN_TARGET}]"
            f"（{name} 要改的那篇文章这一轮没有读到过——先读后台文章列表或正文拿到 id，"
            f"或让主人点名是哪一篇，再改；不要凭记忆写一个 id）")


def target_conflict_frame(name: str, named: set[int], got) -> str:
    """写目标与用户点名不一致时的错误帧（checker 判 BLOCK）。

    20260921 第三轮活体探针实证：管理员说「把文章 1 置顶」，planner 把
    `list_admin_notes` 返回的**第一行**（id=46）填进了 article_id——它读到了 46，
    所以"目标有据"那条判据放行（46 确实在本轮帧里），但那不是用户点的那一篇。
    用户原话点名的 id 是权威：不一致就不执行，把"应该改哪一篇"明确写回帧里，
    让 planner 下一轮自己改回来（禁止由系统替它改写参数——目标必须由用户/planner
    决定，系统只做否决）。
    """
    want = "、".join(str(x) for x in sorted(named))
    return (f"__ERROR__: 目标与主人点名的不是同一篇[{REASON_TARGET_MISMATCH}]"
            f"（{name} 这一轮填的是文章 {got}，而主人点名的是文章 {want}——"
            f"以主人点名的为准，下一轮把 article_id 改成 {want} 再改；"
            f"若主人确实要动文章 {got}，先把这一点问清楚）")


def target_error_reason(text: str) -> str | None:
    """从错误帧取回原因码（非本族帧 → None），供 _check_spec 用。"""
    s = str(text or "")
    if f"[{REASON_TARGET_MISMATCH}]" in s:
        return REASON_TARGET_MISMATCH
    return REASON_UNKNOWN_TARGET if f"[{REASON_UNKNOWN_TARGET}]" in s else None
