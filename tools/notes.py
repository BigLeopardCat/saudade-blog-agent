# -*- coding: utf-8 -*-
"""Public article, taxonomy and activity reads with bounded source evidence."""

import logging
from typing import Annotated, Literal

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.base import _own_get
from tools.result import (
    _LIST_FIELD_MAX,
    _LIST_ROWS_MAX,
    ToolResult,
    _board_text_keys,
    _cap_rows,
    _clip_fields,
    _rows_with_note,
    _shape,
    empty,
    not_found,
    ok,
    unavailable,
)

# Public exports; helper names stay private to their modules.
__all__ = ["logger", "list_notes", "search_notes", "DOC_TYPE_CN", "get_article_detail",
           "rag_search", "get_top_notes", "list_categories", "list_tags", "get_announcements",
           "list_guestbook", "list_my_board", "list_talks", "get_blog_info", "get_social_links"]

logger = logging.getLogger(__name__)


# Resolve shared transports at call time so consumer overrides and test doubles apply.
def _get(path: str, *, not_found_text: str = "",
         payload: dict | None = None) -> dict | list | ToolResult:
    """Resolve the shared public transport at call time, preserving its result type."""
    return _base._get(path, not_found_text=not_found_text, payload=payload)


# Article metadata and body reads.

# Project metadata to the fields needed by the tool; preserve stable article IDs and titles.

# Missing public counters are omitted, never replaced with zero.
_NOTE_COUNT_KEYS = ("views", "likes", "favorites")


def _note_counts(row) -> dict:
    """行里的三个计数 → **只带真实存在的那些键**（认不出/缺键 ⇒ 不带）。

    `bool` 显式排除：它是 `int` 的子类，`True` 会被 `isinstance(v, int)` 放行成 1。
    非数（字符串、None、脏值）一律当"没有这个数"——把 `"120"` 转成 120 属于编数。
    """
    out = {}
    for k in _NOTE_COUNT_KEYS:
        v = row.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        out[k] = int(v)
    return out


def _public_tag_index():
    """公开标签字典 → `{id: TagInfo}`；读不到返回 **None**（≠"没有标签"）。"""
    one = _get("/tagone")
    two = _get("/tagtwo")
    if isinstance(one, ToolResult) or isinstance(two, ToolResult):
        return None
    from agent import adminops_tags as _tags  # 局部导入：tools → agent 的反向依赖
    return _tags.build_tag_index(one, two)


def _slim_note_rows(rows):
    """note 行数组 → 精简行数组（**认不出形态返回 None**，调用方原样透出）。

    只对"看得出是 note 行"的数组动手（行里有 `noteKey`）：其它工具的列表
    （留言/说说/设备…）字段完全不同，套同一把刀会砍掉它们的正文。
    """
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        return None
    if not any("noteKey" in r for r in rows):
        return None
    from agent import adminops_tags as _tags  # 局部导入：同上
    index = None
    if any(r.get("noteTags") for r in rows):
        index = _base._public_tag_index()
    out = []
    for r in rows:
        tag_txt = _tags.render_tag_list(r.get("noteTags"), index)
        slim = {
            "noteKey": r.get("noteKey"),
            "noteTitle": r.get("noteTitle") or "",
            "status": r.get("status") or "",
            "isTop": r.get("isTop") or 0,
            "tags": "" if tag_txt == "（无标签）" else tag_txt,
        }
        slim.update(_note_counts(r))
        out.append(slim)
    return out


@tool
def list_notes(
    page: Annotated[int, "Page number, default 1"] = 1,
    page_size: Annotated[int, "Items per page, default 10"] = 10,
) -> str:
    """获取文章列表，按页返回。每篇给出：id（noteId）、标题、状态、是否置顶、标签名。"""
    data = _get(f"/notes?page={page}&page_size={page_size}")
    slim = _slim_note_rows(data)
    return _shape(slim if slim is not None else data)

@tool
def search_notes(keyword: Annotated[str, "搜索关键词"]) -> str:
    """按关键词匹配文章标题、正文和标签，返回按相关度排序的文章元数据。

    按标题或标签查找文章用本工具，按内容主题检索可用 rag_search。
    候选标题和链接可直接用于推荐；解读、总结或比较正文时需用 get_article_detail 读取正文。
    """
    data = _get("/notes/search", payload={"keyword": keyword})
    if isinstance(data, ToolResult):
        return data
    # Successful empty results remain distinct from transport failures.
    if not data:
        return empty("[]")
    slim = _slim_note_rows(data)
    return _shape(slim if slim is not None else data)

def _read_section(data: dict, article_id, want: str) -> str:
    from rag.reading import article_page
    return _shape(article_page(data, article_id, section=want))


def _note_row_with_tag_names(row):
    """Add displayable tag names while preserving internal tag IDs.

    Unknown row shapes pass through unchanged. Unavailable tag dictionaries retain
    an ID label rather than asserting that the tag does not exist."""
    if not isinstance(row, dict) or not isinstance(row.get("noteTags"), str):
        return row
    from agent import adminops_tags as _tags  # 局部导入：tools → agent 的反向依赖
    index = _base._public_tag_index() if row.get("noteTags") else None
    out = dict(row)
    out["tags"] = _tags.render_tag_list(row["noteTags"], index)
    return out


def _note_public_counts(article_id) -> dict:
    """Read non-incrementing public article counters.

    Private or unavailable statistics return no fields, never fabricated zeros.
    The actor-specific liked flag is omitted because this is a public service read."""
    data = _get(f"/notes/{article_id}/stats")
    if not isinstance(data, dict):
        return {}
    return _note_counts(data)


# Document labels must stay aligned with host execution-receipt rendering.
DOC_TYPE_CN = {
    "note": "文章",
    "talk": "说说",
    "board": "留言",
    "announcement": "公告",
}


_ARTICLE_OFFSET_DESC = "正文范围起点；用返回中的 nextOffset 继续读取"
_ARTICLE_LIMIT_DESC = "本次最多读取字符数，1..12000，默认 6000"
_ARTICLE_REVISION_DESC = "继续读取时照抄 contentRevision，防止跨版本拼接"


@tool
def get_article_detail(
    article_id: Annotated[
        int,
        "文档的唯一 ID（note 为 noteId，talk/board 为 talkId，announcement 为 id）",
    ],
    doc_type: Annotated[
        Literal["note", "talk", "board", "announcement"],
        "文档类型：note（文章，默认）/ talk（说说）/ board（留言）/ announcement（公告）",
    ] = "note",
    section: Annotated[
        str,
        "只读该文章的某一小节：优先用检索的 section id 并带 revision；支持唯一标题/编号",
    ] = "",
    offset: Annotated[int, _ARTICLE_OFFSET_DESC] = 0,
    max_chars: Annotated[int, _ARTICLE_LIMIT_DESC] = 6000,
    revision: Annotated[str, _ARTICLE_REVISION_DESC] = "",
) -> str:
    """读取指定文章、说说、留言或公告的正文证据。

    文章支持按小节或字符范围读取；续读须带返回的 contentRevision，使用 nextOffset
    继续读取，readComplete 表示本范围读完。其他文档返回公开列表中的完整短文本。
    tags 是可展示的标签名，noteTags 是内部编号。views/likes/favorites 缺失表示未取得，
    不代表零。不同文档类型的编号不通用；not_found 表示本次未找到，不代表服务故障。
    """
    if doc_type == "note":
        data = _get(f"/notes/{article_id}", not_found_text=(
            f"站内没有 id={article_id} 这篇文章，本次**没有**读到任何正文。"
            "另注意 id 不是同一套：通知/留言板里的 id 是留言 id，不是文章 id。"
            "按标题找文章用 search_notes 或 rag_search；"
            "读留言/说说用 list_guestbook / list_talks。"))
        if isinstance(data, dict):
            data = _note_row_with_tag_names(data)
            counts = _note_public_counts(article_id)
            if counts:
                data = {**data, **counts}
        if not isinstance(data, dict):
            return _shape(data)
        from rag.reading import RevisionChanged, article_page
        from tools.result import invalid_args
        try:
            return _shape(article_page(data, article_id, section, offset, max_chars, revision))
        except RevisionChanged as exc:
            from rag.search import invalidate_document
            invalidate_document(article_id, revision)
            return unavailable(str(exc))
        except ValueError as exc:
            return invalid_args(str(exc))
    endpoint, key_field = {
        "talk": ("/talk", "talkKey"),
        "board": ("/board", "talkKey"),
        "announcement": ("/announcements", "id"),
    }[doc_type]
    rows = _get(endpoint)
    if not isinstance(rows, list):
        return rows
    for it in rows:
        if str(it.get(key_field)) == str(article_id):
            return _shape(it)
    # 公开列表有范围及审核过滤，缺席不能证明全站不存在。
    return not_found(
        f"公开列表里没有 {key_field}={article_id} 这一条（河灯留言只放行审核通过的，"
        "待审与被驳回的不在列表里；列表也可能只回最近若干条）。所以这是『我没找到』，"
        "不是『站内一定没有』。另注意 id 不是同一套：通知链接里的 lid 是留言 id，不是文章 id。")


def _rag_hit(index, hit):
    title = hit["title"][:24].replace("\n", " ").replace("\r", " ")
    text = f"{index}. type={hit['type']} id={hit['id']} score={hit['score']} title={title}"
    hints = []
    for position, label in enumerate((hit.get("sections") or [])[:3]):
        label = label[:80].replace("\n", " ").replace("\r", " ")
        hint = f"命中节={label}"
        identifiers = hit.get("section_ids") or []
        if position < len(identifiers) and hit.get("document_revision"):
            ident = identifiers[position]
            match = next((m for m in hit.get("matches", []) if m.get("section_id") == ident), {})
            hint += f" section={ident} revision={hit['document_revision']}"
            offset = match.get("offset")
            if type(offset) is int and offset >= 0:
                hint += f" offset={offset}"
        hints.append(hint)
    return text + (" " + " | ".join(hints) if hints else "")


@tool
def rag_search(
    query: Annotated[str, "检索关键词（用户问题中希望从博客内容里找到答案的核心表述）"],
    top_k: Annotated[int, "返回候选文档数量，默认 8"] = 8,
) -> str:
    """按相关性检索博客文章（语料仅限线上可见文章），返回候选文档列表（标题+类型+分数+命中节），不返回全文。

    用于查找某主题的文章，可直接提供候选标题与链接。
    按主题解读时从命中节中选择相关 section 并带 revision，调用 get_article_detail 读取正文，
    无需从头续读整篇；全文总结或比较时按所需范围继续读取。
    （doc_type 取候选的 type）；候选不支持正文结论。
    注意：说说/留言/公告不在检索语料内——询问"最新留言/说说/公告内容"时直接用
    list_guestbook / list_talks / get_announcements 数据工具，不要用本工具检索。
    """
    try:
        from rag.search import last_route, search
        hits = search(query, top_k=top_k)
        if hits is None:
            return unavailable("检索索引未就绪（语料为空或重建失败），暂时无法检索")
        if not hits:
            return empty("检索无结果")
        text = "\n".join(_rag_hit(i + 1, hit) for i, hit in enumerate(hits))
        # Retrieval mode belongs to telemetry; candidate format is shared by both routes.
        route = last_route()
        from rag.search import get_index
        corpus = get_index().status()
        if corpus["stale"]:
            text += ("\n注：语料刷新未完成，本次候选使用最近一次完整索引；"
                     "正文读取仍核对当前公开版本。")
        return ok(text, data=hits, meta={"corpus": corpus, "rag_mode": route["mode"],
                              "rag_reason": route["reason"],      # degraded 时的原因码
                              "vector_hits": route["vectors"],    # 向量路贡献了几条候选
                              "vector_missing": route["missing"]})
    except Exception as exc:
        logger.error("rag_search failed: %s", exc)
        return unavailable(f"检索服务不可用（{type(exc).__name__}）")

@tool
def get_top_notes() -> str:
    """获取置顶文章列表（每篇给出 id、标题、状态、是否置顶、标签名）。"""
    data = _get("/topnotes")
    slim = _slim_note_rows(data)
    return _shape(slim if slim is not None else data)

# Category and tag reads.

@tool
def list_categories() -> str:
    """获取全部分类列表，包含分类名称、颜色、图标、文章数量。"""
    data = _get("/category")
    return _shape(data)

@tool
def list_tags() -> str:
    """获取站内**全部标签**：一级标签 + 它下面的二级标签（二级互不重名，但不同
    一级标签下可以有同名二级标签，靠 fatherKey/fatherTag 区分）。"""
    # 两级都要读：只读 /tagone 时结构上就看不见二级标签，
    # 线上实测答出"站内没有二级标签"。两个端点都是公开只读、无需鉴权。
    one = _get("/tagone")
    two = _get("/tagtwo")
    if isinstance(one, ToolResult):
        # 任一路读不到 → **不合并半份**：半份清单会把"这次没读到二级标签"伪装成
        # "站内没有二级标签"，正是上面那个结论的来源。如实报不可用。
        return one
    if isinstance(two, ToolResult):
        return two
    # 局部导入：agent.graph → tools 的反向依赖会构成循环导入（同 _tag_index）
    from agent import adminops_tags as _tags
    return _shape(_tags.merge_tag_rows(one, two))

# Announcements.

@tool
def get_announcements(
    offset: Annotated[int, "（可选）跳过最新的多少条，用于取回更早的公告；默认 0"] = 0,
) -> str:
    """获取博客公告列表（默认带回最新的那批，注记里写明一共多少条）。

    列表比一次能带的多时，只带回最新的一批；帧尾注记会写明一共多少条、**以及**
    取回更早那一页要填的 `offset`——照注记写的填即可，不必自己猜。"""
    data = _get("/announcements")
    page = None
    if isinstance(data, list):
        # Cap announcement rows without truncating each body; detail reads share this source.
        data, capped, page = _cap_rows(data, _LIST_ROWS_MAX, "公告", offset)
        if capped:
            data = _rows_with_note(data, capped)
    return _shape(data, meta={"page": page} if page else None)

# Guestbook reads.

@tool
def list_guestbook(
    offset: Annotated[int, "（可选）跳过最新的多少条，用于取回更早的留言；默认 0"] = 0,
) -> str:
    """获取留言板（河灯留言）列表（默认带回最新的那批，注记里写明一共多少条）。

    留言比一次能带的多时，只带回最新的一批；帧尾注记会写明一共多少条、**以及**
    取回更早那一页要填的 `offset`——照注记写的填即可，不必自己猜。

    ⚠ **只列已通过审核的留言**（上游 `talks.rs::list_by_src` 恒 `filter(Approved=1)`，
    与留言板页面看到的完全一致）。所以"列表里没有"**不等于"这条留言不存在"**——
    它可能还在待审、或者被驳回了。要说"站内没有这条留言"，前提是**没有任何号段**
    能对上；手上的 id 在列表里查无此条时，正确的说法是"这条不在公开列表里（未通过
    审核或已删除），后台管理视图里能看到"，**不要**据此反推"站内没有"。
    ⚠ **本视图看不到发表账号**：每行里的 `author` 是留言人自己在留名框里填的
    **自由文本**（可以填任何字、也可以留空——留空就是匿名），**不是账号**，
    拿它或正文去认人会认错。要回答"这条留言是谁发的"，走后台名册
    `list_admin_board`（放灯必须登录，那里每条都有真实账号，匿名也溯得到）。
    留言板页面 /guestbook 叫「河灯集」（留言簿）：页面下方有留言输入框（提示语
    「此刻想说的话…」），在框里写好内容即可放灯；留名框在输入框旁，默认预填
    当前登录账号昵称，清空留名或点「匿名」则以无名/匿名身份放灯——无需注册或
    邮箱，输入框一直可见；放灯后可在「我的河灯」页签查看自己放过的灯（与
    graph.py GUESTBOOK_GUIDE 同源，改动须两端同步）。访客问"怎么留言/留言板
    怎么用/哪里能放河灯"时按此流程引导。
    注意：留言板与说说（碎语）是两个独立的数据源——查询"博客里有没有人聊过 X"
    这类问题时，需同时调用 list_talks 检查说说内容，两个都查全后才能回答。
    """
    data = _get("/board")
    page = None
    if isinstance(data, list):
        # The public list includes approved rows only; absence does not prove nonexistence.
        data, capped, page = _cap_rows(data, _LIST_ROWS_MAX, "留言", offset)
        data = _board_text_keys(data)
        data = _drop_dead_mine(data)
        data = _clip_fields(data, _LIST_FIELD_MAX)
        data = _rows_with_note(
            data, (capped + " " if capped else "")
                  + "本列表只含**已通过审核**的留言（与留言板页面一致）；"
                  "待审/被驳回的留言不在这里，查无此条不等于不存在。"
                  "每行的「留名」是留言人**自己填的字**（自由文本，可留空=匿名），"
                  "**不是账号**——别拿它认人；要知道「这条是谁发的」走后台名册 "
                  "list_admin_board（放灯必须登录，那里逐条带真实账号）。"
                  "**本列表也不带「哪条是你自己放的」**——「我自己放过的灯（含待审/"
                  "未通过）」是另一条通道：`list_my_board`")
    return _shape(data, meta={"page": page} if page else None)


# Public reads have no actor identity, so their mine flag is not evidence of ownership.
# Use the authenticated own-message endpoint for ownership queries.
def _drop_dead_mine(rows: list) -> list:
    return [{k: v for k, v in r.items() if k != "mine"}
            if isinstance(r, dict) else r for r in rows]


@tool
def list_my_board(config: RunnableConfig) -> str:
    """列出**当前登录用户自己**放的全部河灯留言（返回 talkId / 内容 / 留名 /
    `approved` 状态 / 驳回理由 / 时间）——**含待审与未通过的那些**，不只公开池里
    已放行的。

    访客问"我放过的河灯/我写的留言有哪些""我哪条留言通过了""那条为什么没通过"
    时用。与 `list_guestbook` 的分工是**公开池 vs 我自己那一份**：公开池只有
    已通过审核的（别人的灯也在里面），这一条只有自己的（待审/被驳回也在里面）。

    `approved`：1=已通过（公开页面上看得见）/ 0=待审 / 2=未通过（只有你在
    「我的河灯」里看得到，`rejectReason` 是驳回理由、只有这一态有）。
    要带主人去**某一条**的位置，用这一行里的 `talkId` 拼 `/guestbook?lid=<talkId>`
    交给 navigate——**别用公开列表的序号**，也**不必**自己找那盏灯在河面上的坐标。
    没携带身份时如实告知读不到。
    """
    data = _own_get("/api/protect/board/mine", config)
    page = None
    if isinstance(data, list):
        # 与公开池同一套封顶。总数**走结构**（`page` → `meta["page"]`，批 G2）：跨轮摘要
        # 读的是那一格，不再从注记的「共 N 条」里抠——注记是给人看的那份渲染。
        data, cap, page = _cap_rows(data, _LIST_ROWS_MAX, "你的河灯")
        data = _board_text_keys(data)
        # The authenticated list already establishes ownership; omit its redundant mine flag.
        data = _drop_dead_mine(data)
        data = _clip_fields(data, _LIST_FIELD_MAX)
        data = _rows_with_note(
            data, (cap + " " if cap else "")
                  + "这是**你自己**放的全部河灯（含待审与未通过——公开列表里没有这两种，"
                  "所以那里查无此条不等于不存在）。`approved`：1=已通过 / 0=待审 / "
                  "2=未通过（驳回理由在 rejectReason，只有这一态有）。"
                  "每行的「留名」是你当时自己填的字（自由文本），不是账号。")
    return _shape(data, meta={"page": page} if page else None)

# Talks and activity.

@tool
def list_talks(
    offset: Annotated[int, "（可选）跳过最新的多少条，用于取回更早的说说；默认 0"] = 0,
) -> str:
    """获取说说（动态/碎语）列表（默认带回最新的那批，注记里写明一共多少条）。

    说说比一次能带的多时，只带回最新的一批；帧尾注记会写明一共多少条、**以及**
    取回更早那一页要填的 `offset`——照注记写的填即可，不必自己猜。

    ⚠ **只列已通过审核的**（同上游过滤器）：列表里没有**不等于**这条不存在，别据此
    下"站内没有"的结论（理由同 list_guestbook）。
    注意：说说与留言板（河灯留言）是两个独立的数据源——查询"博客里有没有人聊过 X"
    这类问题时，需同时调用 list_guestbook 检查留言板内容，两个都查全后才能回答。
    """
    data = _get("/talk")
    page = None
    if isinstance(data, list):
        data, capped, page = _cap_rows(data, _LIST_ROWS_MAX, "说说", offset)
        data = _board_text_keys(data)
        data = _clip_fields(data, _LIST_FIELD_MAX)
        data = _rows_with_note(
            data, (capped + " " if capped else "")
                  + "本列表只含**已通过审核**的说说；待审/被驳回的不在这里，"
                  "查无此条不等于不存在。每行的「留名」是发表人**自己填的字**"
                  "（自由文本，可留空=匿名），**不是账号**——别拿它认人")
    return _shape(data, meta={"page": page} if page else None)

# Site information.

@tool
def get_blog_info() -> str:
    """获取博客基本信息：作者、头像、签名、ICP备案号等。"""
    data = _get("/user")
    return _shape(data)

@tool
def get_social_links() -> str:
    """获取社交链接（QQ、GitHub、BILIBILI等）。"""
    data = _get("/social")
    return _shape(data)
