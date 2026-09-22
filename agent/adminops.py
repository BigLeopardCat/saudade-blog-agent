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


# ── 站内公告（20260922）────────────────────────────────────────────────
# 公告**没有 id 稳定指称**（用户从来只说标题），也没有层级/父级，所以这里的渲染
# 比标签/分类那批简单得多：一律按标题指认，篇数之类的"影响面"也不存在。
# 但有一条独有纪律：**公告正文是对全体访客说的话**，回执行里绝不摘要正文
# （只报"标题改成了什么"），免得把主人的原话改了样子念出去。
def announcement_title(row) -> str:
    return str((row or {}).get("title") or "").strip() or "（无标题）"


def render_announcement_created(row) -> str:
    """发布成功。**不带工具名**（同 render_tag_created 的血案）。"""
    return (f"已发布公告「{announcement_title(row)}」（id={(row or {}).get('id')}）。"
            f"首页现在就能看到它。")


def render_announcement_updated(before, after) -> str:
    """修改成功。**不复述正文**：只报标题怎么变的，正文变更只说"已更新"——
    正文可能有几百字，回执行只留列宽（300），塞进去会把摘要挤掉。"""
    b, a = announcement_title(before), announcement_title(after)
    what = f"标题「{b}」→「{a}」" if b != a else "正文已更新"
    return f"已修改公告「{a}」（{what}）（后台已复核读到新值）"


def render_announcement_noop(row) -> str:
    """要改的内容与现状一致 ⇒ 什么都不用做。**也走 ok**：这不是失败，
    "现状就是这样"是事实本身（narrator 照它答即可）。"""
    return f"公告「{announcement_title(row)}」现在就是这个样子，无需改动"


def render_announcement_deleted(row) -> str:
    """删除成功。公告是真删（没有外键、没有回收站）——回执行必须说清"取不回来"，
    否则用户以为还能撤销。"""
    return (f"已删除公告「{announcement_title(row)}」（删除后取不回来）"
            f"（后台已复核：公告列表里已经没有它）")


# ── 河灯留言的人工复核：回执行措辞（20260922 第六轮）────────────────
# 与写工具同一条纪律：**措辞里不带工具名**（带了会让 narrator 复述内部名字），
# 且必须把"这一动作可逆/不可逆"说清——审核可以改判（驳回的能再放行），删除不行。
def _board_ref(row) -> str:
    """回执行里的留言指称：`#id 「正文片段」（作者）`。

    正文片段是**这条留言在跨轮记忆里唯一的可认物**（下轮主人说"把刚才那条删了"
    要能对上号），作者名字同理；时间不进回执行（列宽 300，读侧只留最近 8 行）。
    两段都经消毒（访客可控文本）。
    """
    from agent.reports import sanitize_untrusted
    body = clip(sanitize_untrusted((row or {}).get("content") or "", 30), 30)
    who = clip(sanitize_untrusted((row or {}).get("author") or "", 16), 16)
    tail = f"（{who} 的留言）" if who else ""
    return f"#{row.get('talkKey')} 「{body}」{tail}"


def render_board_audited(row, verdict: str) -> str:
    """人工复核成功。**说清这是可改判的**（不是删除）——否则主人会以为留言没了。"""
    cn = BOARD_VERDICT_CN.get(verdict, verdict)
    tail = ("留言现在对所有访客可见" if verdict == "pass"
            else "留言已隐藏，作者在「我的河灯」里看到的是未通过")
    return f"已把留言 {_board_ref(row)} 人工复核为**{cn}**——{tail}（后台已复核读到新状态）"


def render_board_audit_noop(row, verdict: str) -> str:
    """现状与目标一致 ⇒ 什么都不用做。**也走 ok**："现在就是这样"是事实本身。"""
    cn = BOARD_VERDICT_CN.get(verdict, verdict)
    return f"留言 {_board_ref(row)} 现在就是「{cn}」状态，无需改动"


def render_board_deleted(row) -> str:
    """删除成功。留言是真删（没有回收站）——回执行必须说清"取不回来"。"""
    return (f"已删除留言 {_board_ref(row)}（删除后取不回来）"
            f"（后台已复核：留言列表里已经没有它）")


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


def _match_board(boards, quote) -> dict | None:
    """在留言清单快照里按正文片段找**唯一**一条（问句渲染用）。

    与 `tools.base._find_board_comment` 同一判据（原文子串 → 去空白子串兜底），
    但**只用于渲染**：这里返回 None 只是"问句里写不出具体是哪一条"，表示"没核对上"
    （读不到清单 / 命中 0 条 / 命中多条都算），渲染方据此如实标注、不装作核对过。
    真正的"能不能动"由规划轮的目标预检与工具侧的复核各自独立判一次。
    """
    if not boards:
        return None
    want = str(quote or "").strip()
    if not want:
        return None
    rows = list(boards.values()) if isinstance(boards, dict) else list(boards)
    hits = [r for r in rows if want in str(r.get("content") or "")]
    if not hits:
        squashed = re.sub(r"\s+", "", want)
        if squashed:
            hits = [r for r in rows
                    if squashed in re.sub(r"\s+", "", str(r.get("content") or ""))]
    return hits[0] if len(hits) == 1 else None


def _confirm_one(spec: dict, index=None, cats=None, boards=None, notes=None) -> str:
    """单条写 spec → 「做什么」的人话（与 server._tool_action_text 同口径）。

    `index` = 可选的标签字典（`{id: TagInfo}`，见 build_tag_index）：给得起就
    **把父标签的名字写进问句**，给不起退回 id。写二级标签时只写「新建二级标签
    「Rust」」等于让用户盲签——他看不到这个 Rust 会挂到哪个爸爸底下，而"挂错
    父标签"正是本轮要修的参数对调事故（20260921）。名字比 id 可靠：id 是系统
    内部编号，用户点确定时没法核对。

    `cats` = 可选的分类字典（`{id: CategoryInfo}`）：删分类要报"有多少篇文章会
    变成没有分类"，只有它能给。**给不起就不说篇数**（不写 0）。

    20260921 第四轮起，**标签/分类一律按名字**（planner 写名字、工具确定性解析
    成 id）：问句里出现的名字就是用户说的那个名字，不再有"id 对不上名字"的
    中间层。

    `notes` = 可选的后台文章清单快照（`{id: 行}`，见 tools.base._note_index）：
    文章写操作的问句**此前只有内部 id**（全写面唯一的盲签），给得起快照就把
    `《标题》（现在：状态、置顶）` 写进去。给不起（读到 None）→ 退回只写 id。
    """
    tool = str(spec.get("tool") or "")
    a = spec.get("args") or {}
    index = index or {}
    where_article = article_where(notes, a.get("article_id"))
    if tool == "create_tag":
        title = str(a.get("title") or "").strip() or "（未命名）"
        pname = str(a.get("parent_tag") or "").strip()
        level = "二级" if pname else "一级"
        where = ""
        if pname:
            hit, cands = find_tag(index, pname)
            if index and hit is None:
                # 父标签名对不上：如实写在问句里。用户点确定之前就该看到
                # "这个爸爸不存在"，而不是点完再被告知没建成。
                where = f"（标签字典里没有叫「{pname}」的一级标签）"
            else:
                where = f"（挂在「{pname}」下）"
        hexval = match_tag_color(a.get("color")) if a.get("color") else None
        color = f"，颜色 {describe_color(hexval)}" if hexval else "（按名字自动配色）"
        return f"新建{level}标签「{title}」{where}{color}"
    if tool in ("update_tag", "delete_tag"):
        name = str(a.get("name") or "").strip() or "（未命名）"
        hit, _ = find_tag(index, name)
        label = hit.label if hit is not None else name
        miss = "（标签字典里没有这个名字）" if (index and hit is None) else ""
        if tool == "update_tag":
            bits = []
            new_title = str(a.get("new_title") or "").strip()
            if new_title:
                bits.append(f"改名为「{new_title}」")
            hexval = match_tag_color(a.get("color")) if a.get("color") else None
            if hexval:
                bits.append(f"颜色改为 {describe_color(hexval)}")
            to_level = str(a.get("to_level") or "").strip()
            pname = str(a.get("parent_tag") or "").strip()
            if pname:
                bits.append(f"移到「{pname}」下面")
            elif to_level == "one":
                bits.append("改成一级标签")
            elif to_level == "two":
                bits.append("改成二级标签")
            body = "、".join(bits) if bits else "（没说要改什么）"
            note = tag_note_phrase(hit)
            return f"修改标签「{label}」{miss}：{body}" + (f"；{note}" if note else "")
        # delete_tag：一级标签会**连带 CASCADE 掉它的子标签**，这是不可回滚的
        # （prune_note_tags 会把所有文章上的引用摘掉）。问句必须把这件事说全。
        if hit is not None and hit.level == 1:
            kids = children_of(index, hit.id)
            if kids:
                names = "、".join(k.name for k in kids)
                head = (f"删除一级标签「{label}」，它下面还有 {len(kids)} 个二级标签"
                        f"（{names}）**会一起删除**")
                tail = tag_note_phrase(hit)
                return head + (f"，删除后这些标签在文章上的引用都会被摘掉（它自己挂在 "
                               f"{hit.note_count} 篇文章上）" if tail else "")
            return f"删除一级标签「{label}」" + (f"，{tag_note_phrase(hit)}" if tag_note_phrase(hit) else "")
        level = "二级" if (hit is not None and hit.level == 2) else ""
        note = tag_note_phrase(hit)
        return (f"删除{level}标签「{label}」" + (f"，并把它从 {hit.note_count} 篇文章上摘掉" if note else "")
                + miss)
    if tool in ("create_category", "update_category", "delete_category"):
        title = str(a.get("new_title") or a.get("title") or a.get("name") or "").strip() or "（未命名）"
        if tool == "create_category":
            extra = f"，路径 {a.get('path_name')}" if str(a.get("path_name") or "").strip() else ""
            return f"新建分类「{title}」{extra}"
        hit, _ = find_category(cats or {}, str(a.get("name") or "").strip())
        label = hit.name if hit is not None else title
        if tool == "update_category":
            bits = []
            if str(a.get("new_title") or "").strip():
                bits.append(f"改名为「{a['new_title']}」")
            for key, cn in (("path_name", "路径"), ("introduce", "简介"),
                            ("icon", "图标"), ("color", "颜色")):
                if str(a.get(key) or "").strip():
                    bits.append(f"{cn}改为「{a[key]}」")
            body = "、".join(bits) if bits else "（没说要改什么）"
            miss = "（分类列表里没有这个名字）" if (cats and hit is None) else ""
            return f"修改分类「{label}」{miss}：{body}"
        # 删除分类：FK 是 ON DELETE SET NULL ⇒ **文章不会被删**，但会变成没有分类。
        # 这一句必须写出来——"删分类"听起来像"删掉分类里的文章"，而事实相反。
        cnt = hit.note_count if hit is not None else None
        tail = (f"，它有 {cnt} 篇文章，删掉后这些文章会变成没有分类"
                if cnt is not None else "，删掉后原本属于它的文章会变成没有分类")
        return f"删除分类「{label}」{tail}"
    if tool in ("create_announcement", "update_announcement", "delete_announcement"):
        # 公告按**标题**指认（用户从来只说标题；公告没有 id 稳定指称）。
        # 弹窗里必须带上正文预览：主人是一眼扫过就点确定的，只写「发布公告「维护通知」」
        # 等于让他签一份没看过的公告——而这段文字会直接对全体访客说出去。
        title = str(a.get("title") or "").strip() or "（未命名）"
        if tool == "create_announcement":
            body = clip(str(a.get("content") or ""), 60)
            return f"发布公告「{title}」，正文：{body}"
        if tool == "update_announcement":
            bits = []
            new_title = str(a.get("new_title") or "").strip()
            if new_title:
                bits.append(f"标题改为「{new_title}」")
            if str(a.get("content") or "").strip():
                bits.append(f"正文改为：{clip(str(a.get('content')), 60)}")
            body = "、".join(bits) if bits else "（没说要改什么）"
            return f"修改公告「{title}」：{body}"
        # 删公告：真删、没有回收站，且访客首页立刻看不到。
        return f"删除公告「{title}」（删掉后首页立刻看不到，且取不回来）"
    if tool in ("audit_board_comment", "delete_board_comment"):
        # 留言按**正文片段**指认（见 tools.base._find_board_comment）。问句里必须
        # 把**匹配到的那一条**写出来（#id + 作者 + 原文），否则主人签的是"一段话"
        # ——而这段话在站内可能出现在好几条留言里，他无从核对要动的到底是哪一条。
        # `boards` 给不起（读不到清单）时退回片段原文 + 明说"没核对上"，不装作核对过。
        quote = str(a.get("quote") or "").strip()
        shown = clip(quote, 40) or "（没有给出片段）"
        # 状态词表只有一份，在 tools/base.py（它描述的是 DB 里 approved 那三个取值，
        # 与工具的读回复核同源）——这里只借来渲染，不另抄一张。
        from tools.base import BOARD_APPROVED_CN
        hit = _match_board(boards, quote)
        if hit is None:
            where = "（没能核对上站内具体是哪一条：留言列表没读到，或含这段话的不止一条）"
            head = f"「{shown}」"
        else:
            where = ""
            head = f"#{hit.get('talkKey')}「{clip(str(hit.get('content') or ''), 40)}」"
            who = clip(str(hit.get("author") or hit.get("nickname") or ""), 16)
            if who:
                head += f"（{who} 的留言）"
            head += f"（现在：{BOARD_APPROVED_CN.get(hit.get('approved'), '状态未知')}）"
        if tool == "delete_board_comment":
            return f"删除留言 {head}{where}（删掉取不回来）"
        v = normalize_verdict(a.get("verdict"))
        what = BOARD_VERDICT_FULL.get(v, f"复核为「{a.get('verdict')}」")
        return f"把留言 {head}{where} 人工复核为 {what}"
    if tool == "set_article_status":
        head = f"修改文章 {a.get('article_id')}{where_article}"
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
        # 带上《标题》之后不再补那个分隔空格：`…（现在：草稿、未置顶） 的标签` 读起来
        # 像两个并列短语。读不到清单时（where_article 为空）一个字符都不变。
        sep = " 的标签" if not where_article else "的标签"
        return f"修改文章 {a.get('article_id')}{where_article}{sep}{tail}"
    return f"执行 {tool}"


def render_confirm_question(specs, index=None, cats=None, boards=None, notes=None) -> str:
    """确认框的问题行：**把要发生的事说全**（含颜色名与色值），再问一句。

    用户点的是"确定"，他有权在点之前从这句话里看出自己将同意什么——
    说漏了颜色、说漏了是哪一篇、**说漏了挂在哪个父标签下**、**说漏了会连带删掉
    几个子标签**，这个按钮就变成了盲签。
    （`index`/`cats`/`notes` 见 _confirm_one；读不到字典时退化成名字原文或 id，
    不因此不弹窗——这一轮的价值就是让主人确认，读不到就少说，不是不弹。）
    """
    acts = "；".join(_confirm_one(s, index, cats, boards, notes) for s in (specs or []))
    return f"要{acts}吗？点「确定」我就去办。"


def render_confirm_text(specs, index=None, cats=None, boards=None, notes=None) -> str:
    """弹窗那一轮的**对话气泡正文**（系统给的，不经 narrator）。

    刻意写得像"在等你的意思"而不是"已经在办了"：这一轮零执行。给一个明确
    的操作路径（点按钮 / 直接打字），两条路都通向同一条写通道。
    """
    acts = "；".join(_confirm_one(s, index, cats, boards, notes) for s in (specs or []))
    # 不说"上面/下面"：20260921d 起确认卡片渲染在**对话流里**（问句气泡之后），
    # 方位词只会随排版漂移——只点按钮名，两侧 UI 都能对上
    return (f"好呀，这一步要动到站内数据，我先跟你确认一下：\n\n"
            f"**{acts}**\n\n"
            f"点「确定」我就去办；点「取消」就当没说过，"
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
