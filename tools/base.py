"""Custom tool definitions for the LangChain agent.

Each tool is a @tool-decorated function with type hints, docstrings,
and error handling for production reliability.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time

import httpx
from typing import Annotated
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

# 端到端链路关联（图改进Ⅳ）：当前请求的 trace_id（utils.logging contextvar，
# run_in_executor 由 _submit_with_context 的 copy_context 传播），随 X-Request-Id
# 透传 device-service → cmd payload → ESP32 cmd/ack 回执，四端日志可对账
from utils.logging import get_trace_id

logger = logging.getLogger(__name__)

API_BASE = "https://saudade.site/api/public"

# ── 工具返回值的结构化"两类"（20260916 加固）──
# 背景：工具失败时返回的是**人话字符串**，与正常内容同型；`_get` 更是把所有上游故障
# 吞成 `[]`。"当前用户还没有绑定任何 IoT 设备"（空结果）与"查询设备列表失败:
# Connection refused"（服务不可用）在 checker 眼里都是"非空文本" ⇒ 双双 PASS 并被记成
# **系统确认事实**（receipt）——于是"服务挂了"被当成"查到了、就是空的"进了跨轮执行
# 记忆，narrator 也可能照着念。
#
# 最小解：返回值**仍然是字符串**（人话，给 narrator 与用户看，下游所有 `str()`/
# 切片/拼接照旧），只是多带一个机器可读的 kind：
#   ok          正常数据
#   empty       服务正常、结果就是空（"你还没绑定设备"是**事实**，照常 PASS 进回执）
#   unavailable 服务不可用 / 鉴权失败 / 超时（**不是事实**：checker 判 BLOCK，
#               planner 据 reason=unavailable 决定重试还是如实告知）
# 只覆盖"能碰外部服务或可能查空"的工具；命令类工具（导航/特效/暗色/屏显）本来就是
# 命令帧契约（cmd_shape 校验），不动。
class ToolResult(str):
    kind: str = "ok"
    # 机器可读的副产物（20260921）：给**写操作**用——文本是给人/narrator 看的人话，
    # `meta` 是给回执（execution_log.detail）看的**结构**：谁执行的、改了什么字段、
    # 从什么变成什么。为什么不让 narrator/Rust 去解析人话：那是在拿正则啃中文，
    # 改一次措辞就静默失配（回执是落库的审计，不能靠"读起来差不多"）。
    # 与 kind 同一条生命线，经 `.invoke()` 透传（test_reports/test_admin_write 有锁）。
    meta: dict = {}

    def __new__(cls, text: str, kind: str = "ok",
                meta: dict | None = None) -> "ToolResult":
        obj = str.__new__(cls, text)
        obj.kind = kind
        obj.meta = dict(meta) if meta else {}
        return obj


def ok(text: str, meta: dict | None = None) -> ToolResult:
    return ToolResult(text, "ok", meta)


def empty(text: str, meta: dict | None = None) -> ToolResult:
    return ToolResult(text, "empty", meta)


def unavailable(text: str, meta: dict | None = None) -> ToolResult:
    return ToolResult(text, "unavailable", meta)


def not_found(text: str, meta: dict | None = None) -> ToolResult:
    """目标不存在 / 不属于你（20260923 三轮）——**第三个失败族**，与 unavailable 分开。

    为什么必须分开（trace `20260923T130033_9` 实证）：planner 拿"条数"当 id 填进
    `ids:[3]`，服务端按 3 匹配 0 行，写后复核（未读数没降）如实报"未确认生效"——
    但那是 `unavailable`，过程行按 `_REASON_CN` 显示成**「服务不可用」**。用户看到
    的是"系统挂了"，而真问题是"你要标的第 3 条根本不存在"。两者的**应对**也相反：
    服务不可用 = 稍后再试；目标不存在 = 换个 id 或如实问主人（planner 拿到这个
    原因码才会去读列表/追问，而不是重试同一条）。

    与 `empty` 也分开：empty 是"查到了、就是空的"（是事实，checker 判 PASS 进回执），
    这里是"你要动的东西不在我能确认的范围内"（不是"没有这条"的结论——可能是列表
    只回最近 100 条，措辞里必须如实交代这个边界）。
    """
    return ToolResult(text, "not_found", meta)


# 上游故障哨兵：`_get` 失败时返回它而不是 `[]`（"故障伪装成空"的源头就在那）。
# 工具的出口要用 `_shape(data)` 而不是 `str(data)`——理由见 _shape 的注释。
UPSTREAM_DOWN = unavailable("服务暂时不可用，请稍后再试")


def _shape(data) -> str:
    """`_get` 结果的统一出口。**别写 `str(data)`**：`str()` 作用在 str 子类上会退化成
    普通 str（CPython 行为），kind 标记就丢了——20260916 加 kind 时踩过这个坑，
    单测里有一条专门盯"经 .invoke() 透传后标记仍在"。"""
    return data if isinstance(data, ToolResult) else str(data)


# httpx 客户端复用。
# ⚠️ **不要加 `verify=False`**（20260916 加固）：这里不只打自家站点的公开 API，还打
# **第三方** `https://wttr.in`（天气工具）——关掉校验等于把 TLS 降级成"加密但不可
# 认证"，第三方那条尤其说不通（中间人可替换响应体，而响应会进 prompt）。
# 两个域名证书链都正常（实测 verify=True 均 200），所以关校验从来不是"必需"，只是
# 早期图省事。tests/test_hardening.py 里有一条盯着校验开关的断言。
_client = httpx.Client(timeout=15)

def _get(path: str, *, not_found_text: str = "") -> dict | list | ToolResult:
    """Helper: call API and return data field.

    ⚠️ 失败返回 `UPSTREAM_DOWN`（kind=unavailable）而**不是** `[]`——见上面 ToolResult
    的注释：把故障吞成空列表，会让"服务挂了"伪装成"查到了、就是空的"进入执行回执。
    绝大多数调用方 `return _shape(data)`，不改也能拿到人话（只是这时带 unavailable 标记）；
    要迭代结果的（如 get_article_detail）必须自己先判 `isinstance(data, list)`。

    `not_found_text` 非空时，**HTTP 404 是另一个族**（20260924）：传进来的那句话原样
    变成 `not_found`（kind=not_found，checker 判 BLOCK + 原因码 target_not_found），
    而不是"服务不可用"。这两件事的应对是相反的——查无此物要换 id 或如实问主人，
    服务不可用才等一会儿再试；而 404 恰好是"查无此物"最常见的传法。
    不传则维持原样（404 仍算 unavailable）：只有知道"这个 id 查不到意味着什么"的
    调用方（现已带话术的那几个）才该打开它，否则会把路由写错/参数写错也读成"没有"。
    其余失败码（5xx/超时/连不上/坏 JSON）一律仍是 unavailable。
    """
    try:
        resp = _client.get(f"{API_BASE}{path}")
        if not_found_text and resp.status_code == 404:
            logger.info("API 404: %s", path)
            return not_found(not_found_text)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") == 200:
            return body["data"]
        logger.warning("API error: %s", body.get("message"))
        return UPSTREAM_DOWN
    except Exception as exc:
        logger.error("API call failed: %s", exc)
        return UPSTREAM_DOWN

# ---------------------------------------------------------------------------
# 笔记 / 文章 工具
# ---------------------------------------------------------------------------

# ── note 行瘦身（20260921）──────────────────────────────────────────────
# 为什么必须瘦：`/notes` 一行 477 字符（描述/封面/6 个焦点缩放字段/两个时间戳/重复的
# `key` 与空 `content` 全在里面），10 行 ≈ 4.8KB —— 而 `_frame_texts` 给 planner 的
# 单帧预算只有 300 字符，planner 看到的是**第一行的前 300 字符**（半截 JSON）：既拿不到
# "全部标题 ↔ id"的对应，也看不出后面还有 9 条（无声截断，20260920 那类"看不见的洞"）。
# 瘦身只留决策要用的五个：id / 标题 / 状态 / 置顶 / 标签名。
# **两条下游契约不许动**：`agent/entities.py::_note_digest`（跨轮实体摘要）与
# `agent/decisions.py::_candidate_detail_plan`（候选改读闸的 `literal_eval`）都只读
# `noteKey`/`noteTitle`——瘦身保留这两个键，两处照常工作。
_NOTE_SLIM_KEYS = ("noteKey", "noteTitle", "status", "isTop")


def _public_tag_index():
    """公开标签字典 → `{id: TagInfo}`；读不到返回 **None**（≠"没有标签"）。"""
    one = _get("/tagone")
    two = _get("/tagtwo")
    if isinstance(one, ToolResult) or isinstance(two, ToolResult):
        return None
    from agent import adminops as A      # 局部导入：tools → agent 的反向依赖
    return A.build_tag_index(one, two)


def _slim_note_rows(rows):
    """note 行数组 → 精简行数组（**认不出形态返回 None**，调用方原样透出）。

    只对"看得出是 note 行"的数组动手（行里有 `noteKey`）：其它工具的列表
    （留言/说说/设备…）字段完全不同，套同一把刀会砍掉它们的正文。
    """
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        return None
    if not any("noteKey" in r for r in rows):
        return None
    from agent import adminops as A      # 局部导入：同上
    index = None
    if any(r.get("noteTags") for r in rows):
        index = _public_tag_index()
    out = []
    for r in rows:
        tag_txt = A.render_tag_list(r.get("noteTags"), index)
        out.append({
            "noteKey": r.get("noteKey"),
            "noteTitle": r.get("noteTitle") or "",
            "status": r.get("status") or "",
            "isTop": r.get("isTop") or 0,
            "tags": "" if tag_txt == "（无标签）" else tag_txt,
        })
    return out


@tool
def list_notes(
    page: Annotated[int, "Page number, default 1"] = 1,
    page_size: Annotated[int, "Items per page, default 10"] = 10,
) -> str:
    """获取文章列表，按页返回。每篇给出：id（noteKey）、标题、状态、是否置顶、标签名。"""
    data = _get(f"/notes?page={page}&page_size={page_size}")
    slim = _slim_note_rows(data)
    return _shape(slim if slim is not None else data)

@tool
def search_notes(keyword: Annotated[str, "搜索关键词"]) -> str:
    """站内关键词搜索：匹配**标题、正文、标签名**，返回文章列表（每篇给出 id、标题、
    状态、是否置顶、标签名），按相关度排序（标题命中权重最高）。

    与 rag_search 的分工：本工具是"关键词命中"，**按标签或标题定位一批文章**时最有效
    （标签名只有这里有）；想知道"某篇文章里写了什么、站内有没有写过某话题"用 rag_search
    （它按词法给分节命中片段，能读到正文里的表述）。本工具只回元数据，要答案还得按 id
    调 get_article_detail 读全文。两三个字的词、或中英混排都能搜（分词在服务端做）。"""
    try:
        resp = _client.post(
            f"{API_BASE}/notes/search",
            json={"keyword": keyword},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", [])
        # 空列表 = "查到了，就是空的"（事实，checker PASS、进回执）——用 empty() 标 kind，
        # 别写 str(data)（20260916 契约：str() 会退化成普通 str 丢 kind）。
        # 20260920 之所以要这个标记：真实事故里 narrator 把 `返回: []` 读成了"本轮没有
        # 执行任何工具"（_NO_EXEC_CLAIM_RE 是兜底，渲染侧已同步标注"已执行，结果为空"）。
        if not data:
            return empty("[]")
        slim = _slim_note_rows(data)
        return _shape(slim if slim is not None else data)
    except Exception as exc:
        logger.error("Search failed: %s", exc)
        return unavailable(f"搜索服务暂时不可用（{type(exc).__name__}），请稍后再试")

def _read_section(data: dict, article_id, want: str) -> ToolResult:
    """按小节取回文章片段（get_article_detail 的 section 分支，20260920）。

    返回仍是 **Python repr 的 dict**（与全文分支同形），两个键不能少：
      - `noteTitle`：`agent/decisions.py::_doc_title` 从 repr 里正则抠它做跨轮
        指代锚点（"读取文章 19《架构文档》"），换掉键名会让执行记忆只剩 id；
      - `noteKey`/`sectionText`：planner 的"未展开小节"清单要照抄这个 id 再读一次。
    取不到小节时**不返回空**：把候选小节名列出来才是可行动的（模型改一次指称即可），
    说"没找到"而不给候选，等于让它再赌一次。
    """
    from agent.sections import candidates, pick     # 惰性：别让 tools 层启动即拉 agent 包
    content = data.get("noteContent") or data.get("content") or ""
    title = data.get("noteTitle") or data.get("title") or ""
    hit = pick(content, want, title)
    if hit is None:
        return ok(str({
            "noteKey": article_id, "noteTitle": title, "readSection": str(want),
            "sectionText": "",
            "availableSections": candidates(content, want, title)[:40],
            "note": "该文章没有标题匹配此指称的小节（本节未读到任何内容）；"
                    "可用小节见 availableSections，请照其中的名字或编号重试。",
        }))
    return ok(str({
        "noteKey": article_id, "noteTitle": title, "readSection": hit["section"],
        "sectionText": hit["text"],
        "note": f"本节为节选读取（只含《{hit['section']}》这一小节，不含文章其他部分）。",
    }))


def _note_row_with_tag_names(row):
    """note 行 → 补一个 `tags`（中文标签名）。**认不出形态原样返回**。

    详情接口给的 `noteTags` 是**标签 id 串**（`'5'` / `'5,10000'`）——那是内部
    id，不是标签名，narrator 只能照帧念 id（20260921 22:34 实证：反问"这篇都有什么
    标签呀"，回了「标签是 noteTags: '5'」，用户只好说"查查吧"，白烧两轮才拿到
    「Python（挂在 编程 下）」）。这里把名字算好放进 `tags`，`noteTags` 原样保留
    （写操作与 `$tool[N].noteTags` 这类参数引用仍按原字段取值）。
    名字读不到时 render_tag_list 如实渲染成 `id=5`——"这次读不到字典"与"标签不存在"
    不混为一谈。
    """
    if not isinstance(row, dict) or not isinstance(row.get("noteTags"), str):
        return row
    from agent import adminops as A      # 局部导入：tools → agent 的反向依赖
    index = _public_tag_index() if row.get("noteTags") else None
    out = dict(row)
    out["tags"] = A.render_tag_list(row["noteTags"], index)
    return out


@tool
def get_article_detail(
    article_id: Annotated[int, "文档的唯一 ID（note 为 noteKey，talk/board 为 talkKey，announcement 为 id）"],
    doc_type: Annotated[str, "文档类型：note（文章，默认）/ talk（说说）/ board（留言）/ announcement（公告）"] = "note",
    section: Annotated[str, "只读该文章的某一小节（标题全称/编号/唯一子串，如 \"9\" 或 \"9. 部署与运维\"）；留空读全文"] = "",
) -> str:
    """获取指定文档的内容（路线 B 契约的解读段：检索只定位、解读读全文）。
    note 走 /notes/:id；talk/board/announcement 无单条详情端点，从列表接口按 key
    过滤（列表已带全文，量小，全量扫描可接受）。

    `section`：文章过长时全文帧只带得回部分小节（帧尾会列出未展开的小节名），
    用本参数按小节名取回被略去的那一节（20260920 超长文章修复的读取侧）。只对
    note 生效——说说/留言/公告本来就是短文本。

    标签：返回里的 `tags` 是**中文标签名**（如「编程 / Python」，含层级）；`noteTags`
    是内部 id 串，**回答"这篇有什么标签"要用 `tags`**，别把 id 念给访客。

    **查无此篇不是故障**（20260924）：id 不存在时返回的是"站内没有这篇文章"这句
    事实（kind=not_found），并且**点明 id 的来路**——通知/留言板里的 id 是留言 id，
    与文章 id 不是同一套（trace `20260924T030031` 实证：planner 把通知链接里的留言
    id 当文章 id 读了，拿到"服务暂时不可用"，于是一边重试一边准备说系统挂了）。
    应对是换 id 或如实告知，不是"稍后再试"。
    """
    if doc_type == "note":
        data = _get(f"/notes/{article_id}", not_found_text=(
            f"站内没有 id={article_id} 这篇文章，本次**没有**读到任何正文。"
            "另注意 id 不是同一套：通知/留言板里的 id 是留言 id，不是文章 id。"
            "按标题找文章用 search_notes 或 rag_search；读留言/说说用 list_guestbook / list_talks。"))
        if isinstance(data, dict):
            data = _note_row_with_tag_names(data)
        if not section or not isinstance(data, dict):
            return _shape(data)          # 故障（unavailable）与查无此篇（not_found）原样透出，都不伪装成空
        return _read_section(data, article_id, section)
    endpoint, key_field = {
        "talk": ("/talk", "talkKey"),
        "board": ("/board", "talkKey"),
        "announcement": ("/announcements", "id"),
    }[doc_type]
    rows = _get(endpoint)
    if not isinstance(rows, list):          # 上游故障：如实说服务不可用，别说"没找到"
        return rows
    for it in rows:
        if str(it.get(key_field)) == str(article_id):
            return str(it)
    # 列表里没这一条：同样是"查无此物"（20260924 与上面 note 分支同办），但措辞必须
    # 交代边界——这些列表接口可能只回最近的若干条，且河灯留言只放行**审核通过**的
    # （`talks.rs::list_by_src` 恒过滤 approved=1），"不在列表里"与"真的不存在"
    # 不是一回事（not_found 的语义，见它的定义）。此前这里给 empty："查到了、就是
    # 空的"会 PASS 进回执，跨轮执行记忆里就多出一条"读取文章 N"的假事实。
    return not_found(
        f"公开列表里没有 {key_field}={article_id} 这一条（河灯留言只放行审核通过的，"
        "待审与被驳回的不在列表里；列表也可能只回最近若干条）。所以这是『我没找到』，"
        "不是『站内一定没有』。另注意 id 不是同一套：通知链接里的 lid 是留言 id，不是文章 id。")


@tool
def rag_search(
    query: Annotated[str, "检索关键词（用户问题中希望从博客内容里找到答案的核心表述）"],
    top_k: Annotated[int, "返回候选文档数量，默认 8"] = 8,
) -> str:
    """按相关性检索博客文章（语料仅限线上可见文章），返回候选文档列表（标题+类型+分数+命中节），不返回全文。

    用于定位候选：拿到候选后调用 get_article_detail 读取相关文档全文（doc_type 取候选的 type）再回答。
    仅适用于"文章内容知识型问题"（想知道某篇文章写了什么、博客里是否写过某话题）。
    注意：说说/留言/公告不在检索语料内——询问"最新留言/说说/公告内容"时直接用
    list_guestbook / list_talks / get_announcements 数据工具，不要用本工具检索。
    """
    try:
        from rag.search import search
        # 「索引不可用」与「没命中」必须分开（20260917 审计指出）：`search()` 两种情况
        # 都返回 []，此前一律包成 empty("检索无结果") —— 语料拉取失败时会被 checker
        # 记成"检索过、确实没有"的**事实**，正是我上轮给 HTTP 工具修掉的那类问题。
        hits = search(query, top_k=top_k)
        if hits is None:
            return unavailable("检索索引未就绪（语料为空或重建失败），暂时无法检索")
        if not hits:
            return empty("检索无结果")
        # 行式结构化候选摘要（20260831）：精简为 type/id/score/title/首个命中节，
        # 8 候选 ≈ 400-600 字——候选选择信息不丢失且体积可控，模型与反射器视野
        # 一致（此前 JSON 全文被 _build_trace 截断 [:100]，反射器只见 top-1 候选，
        # 误判"读了不存在的文档"，见问题记录 1.26）
        return "\n".join(
            f"{i + 1}. type={h['type']} id={h['id']} score={h['score']} "
            f"title={h['title'][:24]}" + (f" 命中节={h['sections'][0][:12]}" if h["sections"] else "")
            for i, h in enumerate(hits)
        )
    except Exception as exc:
        logger.error("rag_search failed: %s", exc)
        return unavailable(f"检索服务不可用（{type(exc).__name__}）")

@tool
def get_top_notes() -> str:
    """获取置顶文章列表（每篇给出 id、标题、状态、是否置顶、标签名）。"""
    data = _get("/topnotes")
    slim = _slim_note_rows(data)
    return _shape(slim if slim is not None else data)

# ---------------------------------------------------------------------------
# 分类 / 标签 工具
# ---------------------------------------------------------------------------

@tool
def list_categories() -> str:
    """获取全部分类列表，包含分类名称、颜色、图标、文章数量。"""
    data = _get("/category")
    return _shape(data)

@tool
def list_tags() -> str:
    """获取站内**全部标签**：一级标签 + 它下面的二级标签（二级互不重名，但不同
    一级标签下可以有同名二级标签，靠 fatherKey/fatherTag 区分）。"""
    # 两级都要读（20260921 修）：只读 /tagone 时结构上就看不见二级标签，
    # 线上实测答出"站内没有二级标签"。两个端点都是公开只读、无需鉴权。
    one = _get("/tagone")
    two = _get("/tagtwo")
    if isinstance(one, ToolResult) or isinstance(two, ToolResult):
        # 任一路读不到 → **不合并半份**：半份清单会把"这次没读到二级标签"伪装成
        # "站内没有二级标签"，正是上面那个结论的来源。如实报不可用。
        return UPSTREAM_DOWN
    # 局部导入：agent.graph → tools 的反向依赖会构成循环导入（同 _tag_index）
    from agent import adminops as A
    return _shape(A.merge_tag_rows(one, two))

# ---------------------------------------------------------------------------
# 公告 工具
# ---------------------------------------------------------------------------

@tool
def get_announcements() -> str:
    """获取博客公告列表。"""
    data = _get("/announcements")
    return _shape(data)

# ---------------------------------------------------------------------------
# 留言板 工具
# ---------------------------------------------------------------------------

@tool
def list_guestbook() -> str:
    """获取留言板（河灯留言）列表。

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
    return _shape(data)

# ---------------------------------------------------------------------------
# 说说 / 动态 工具
# ---------------------------------------------------------------------------

@tool
def list_talks() -> str:
    """获取说说（动态/碎语）列表。

    注意：说说与留言板（河灯留言）是两个独立的数据源——查询"博客里有没有人聊过 X"
    这类问题时，需同时调用 list_guestbook 检查留言板内容，两个都查全后才能回答。
    """
    data = _get("/talk")
    return _shape(data)

# ---------------------------------------------------------------------------
# 站点信息 工具
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# 导航 / 引导工具
# ---------------------------------------------------------------------------

@tool
def get_site_map() -> str:
    """返回博客功能结构图，用于引导用户了解博客有哪些功能及其位置。"""
    return """
博客功能结构：
- 首页 (/) — 展示置顶文章、最新文章列表、个人简介
- 归档 (/times) — 按时间轴归档展示所有文章
- 分类 (/category/:name) — 按分类查看文章
- 说说 (/talk) — 动态/碎语
- 留言板 (/guestbook) — 「河灯集」留言簿：页面下方输入框（提示语「此刻想说的话…」）
  写内容即可放灯；留名框默认预填昵称、可匿名，无需注册邮箱
- 关于我 (/about) — 个人介绍
- 文章详情 (/article/:id) — 查看文章全文，支持 Mermaid 图表
- 后台管理 (/dashboard) — 登录后可管理文章、分类、标签、公告等
- 物联网控制台 (/device-console) — 管理访客自己的 IoT 设备（ESP32 OLED 屏幕显示等），需登录
"""

# ---------------------------------------------------------------------------
# 聊天历史工具
# ---------------------------------------------------------------------------

@tool
def get_chat_history(
    limit: Annotated[int, "Number of recent messages to fetch"] = 10,
) -> str:
    """Get recent chat history for the current user."""
    return "对话历史已由系统自动注入到当前请求的上下文中（最近 20 条消息 + 滚动摘要），无需额外查询，直接依据系统上下文作答即可。"

# ---------------------------------------------------------------------------
# 知识库工具
# ---------------------------------------------------------------------------

@tool
def search_knowledge_base(
    query: Annotated[str, "Search keywords for knowledge base"],
) -> str:
    """Search knowledge base for documents matching the query."""

    try:
        resp = _client.get(f"{API_BASE}/knowledge")
        if resp.status_code != 200:
            return unavailable(f"知识库查询失败: HTTP {resp.status_code}")
        items = resp.json()
        if not items:
            return empty("知识库中暂无内容")
        results = []
        q = query.lower()
        for item in items:
            title = item.get("title", "")
            content = item.get("content", "")
            category = item.get("category", "")
            if q in title.lower() or q in content.lower() or q in category.lower():
                results.append(f"[{category}] {title}\n{content[:500]}")
        if results:
            return "\n---\n".join(results[:5])
        return empty(f"知识库中未找到与「{query}」相关的内容")
    except Exception as e:
        return unavailable(f"知识库查询失败: {e}")

# ---------------------------------------------------------------------------
# 时间 / 天气工具
# ---------------------------------------------------------------------------

@tool
def get_current_time() -> str:
    """Get current date and time."""
    from datetime import datetime
    now = datetime.now()
    weekdays = ['星期一','星期二','星期三','星期四','星期五','星期六','星期日']
    return now.strftime(f"%Y年%m月%d日 {weekdays[now.weekday()]} %H:%M")

# 城市名的形状闸（20260925 审计）：`location` 是 planner 从用户话里填的参数，直接拼进
# URL 的路径段。host 固定 ⇒ 不是 SSRF；能改的只是 wttr.in 那一侧的 path/query（例如换
# 返回格式）。这一闸不是"防注入"（响应正文本来就不可信、照常进 prompt），而是别让任意
# 字符串进 URL——中英文 + 空格 + 少量标点是城市名的实际形态，其余一律拒（不猜、不默认）。
_WEATHER_LOC_RE = re.compile(r"[A-Za-z一-龥 ,.'\-]{1,40}")


@tool
def get_weather(
    location: Annotated[str, "City name"] = "Beijing",
) -> str:
    """Query weather for a city using wttr.in."""
    from urllib.parse import quote
    loc = str(location or "").strip()
    if not _WEATHER_LOC_RE.fullmatch(loc):
        return unavailable(f"城市名「{loc[:40]}」不合法（只接受中英文、空格与 , . ' -），未查询")

    try:
        resp = _client.get(f"https://wttr.in/{quote(loc)}?format=%C+%t+%w+%h",
                           timeout=10)
        if resp.status_code == 200:
            return f"{loc}天气: {resp.text.strip()}"
        return unavailable(f"天气服务返回 HTTP {resp.status_code}")
    except Exception as e:
        return f"Weather query failed: {e}"

# ---------------------------------------------------------------------------
# 导航工具
# ---------------------------------------------------------------------------

# 导航目标白名单（工具层硬校验——本常量是单一事实来源，skills.py 的
# NAV_VALID_PATHS 从这里同源导入，规划侧约束 = NAV_MAP 别名映射 + instantiate_plan
# 校验 + planner 提示词规则）：模型不可信，工具必须自证。
# 教训：模型曾把"友链板块"猜成 /links 直接发出去（真实页是 /guestbook）。
# 后台子页（20260926：转跳要能定位到后台各板块）在这里**逐个列出**，对应
# skills.py 的 DASHBOARD_PANELS（面板名与路径的第二处，由 tests/test_skills.py
# 的两侧覆盖断言锁住同源）。**不放开 `/dashboard/` 前缀**：前缀放行等于让模型
# 自己拼子路径，而前端 /dashboard 下没有通配子路由——猜出来的路径渲染的是一片
# 空白（不是 NotFound 页），比"拒绝并回真实清单"糟得多。
_NAV_EXACT_PATHS = {
    "/", "/about", "/guestbook", "/talk", "/times", "/login", "/dashboard",
    "/dashboard/notes", "/dashboard/comments", "/dashboard/albums",
    "/dashboard/announcement", "/dashboard/users", "/dashboard/analytics",
    "/dashboard/usercontrol",
    "/device-console/",
}
_NAV_PREFIX_PATHS = ("/category/", "/article/")


@tool
def navigate_to(
    path: Annotated[str, "Page path to navigate to, e.g. / /times /category/tech /article/3 /talk /guestbook /about /dashboard/notes"],
    confirm: Annotated[bool, "Whether user confirmation is needed. false=direct nav, true=ask user"] = True,
) -> str:
    """导航到博客页面。页面跳转只能通过调用本工具生效：调用后返回 NAVIGATE:/AUTO_NAVIGATE: 前缀命令，由系统执行跳转。
    严禁在回复正文中自行输出命令前缀文本——那不是工具调用，不会产生任何跳转，属于违规输出，会触发 gate 声称检查（fallback 如实文本收尾）。"""
    p = path.strip()
    # /category/*、/article/* 要求至少带一个 id 段（/category/ 裸前缀不算有效页面）
    valid = p in _NAV_EXACT_PATHS or (p.startswith(_NAV_PREFIX_PATHS) and p.count("/") >= 2)
    if not valid:
        # 拒绝时把真实约束回给模型，让它用有效路径重新调用（而不是返回错误命令让前端执行）
        return (
            f"导航路径无效: {p!r}。博客真实存在的页面: /（首页）、/about、/guestbook、/talk、"
            f"/times、/login、/category/*、/article/*、/device-console/、以及后台各面板："
            f"/dashboard（后台主页）、/dashboard/notes（后台笔记）、"
            f"/dashboard/comments（后台说说）、/dashboard/albums（后台图库）、"
            f"/dashboard/announcement（后台公告）、/dashboard/users（后台用户管理）、"
            f"/dashboard/analytics（后台数据板）、/dashboard/usercontrol（后台站点设置）。"
            f"请用有效路径重新调用 navigate_to。"
        )
    full_url = f"https://saudade.site{p}"
    return f"{chr(78)+chr(65)+chr(86)+chr(73)+chr(71)+chr(65)+chr(84)+chr(69) if confirm else chr(65)+chr(85)+chr(84)+chr(79)+chr(95)+chr(78)+chr(65)+chr(86)+chr(73)+chr(71)+chr(65)+chr(84)+chr(69)}:{full_url}"

@tool
def toggle_effect(
    effect: Annotated[str, "Effect name: sakura(樱花), rain(大雨), snow(雪花)"],
    action: Annotated[str, "开启还是关闭: on(开启), off(关闭)"] = "on",
) -> str:
    """开启或关闭博客页面的视觉效果（樱花/大雨/雪花）。
    返回 EFFECT: 前缀命令供前端执行；前端按 action 显式开关，不会因重复命令翻转状态。
    参数校验：无效 effect/action 返回提示而非命令帧——命令帧只代表真实执行的切换，
    未返回命令帧 = 动作未发生，回复不得声称已开/已关（gate 声称检查依据）。"""
    if effect not in ("sakura", "rain", "snow"):
        return f"效果无效: {effect!r}。可选: sakura(樱花), rain(大雨), snow(雪花)"
    if action not in ("on", "off"):
        return (
            f"action 无效: {action!r}。可选: on(开启), off(关闭)。"
            f"查询效果当前状态请以对话上下文中的 current_effects 字段为准，无需调用工具。"
        )
    return f"EFFECT:{effect}:{action}"


@tool
def toggle_dark_mode(
    mode: Annotated[str, "夜间模式开关: on(开启夜间模式), off(关闭夜间模式)"],
) -> str:
    """开启或关闭博客页面的夜间模式（暗色主题）。
    返回 DARKMODE: 前缀命令供前端执行；状态会持久化记忆。"""
    if mode in ("on", "off"):
        return f"DARKMODE:{mode}"
    return "模式参数无效，应为 on 或 off"

# ---------------------------------------------------------------------------
# IoT 设备（ESP32 OLED 屏幕显示等，经 device-service 下发）
# ---------------------------------------------------------------------------

# 从 settings 读取（pydantic-settings 负责 .env 加载；os.getenv 读不到 .env）
from config import settings as _settings

DEVICE_SERVICE_URL = _settings.device_service_url
JWT_SECRET = _settings.jwt_secret

# 显示指令幂等去重：同一用户短时间内相同内容的重复下发直接跳过。
# 场景：多轮重复调用（planner 重试/多轮规划） / 客户端重试 / MQTT QoS1
# at-least-once 重投——工具层保证"同内容只发一次"。（曾防后端强制路由
# _force_display 与自主调用双调，20260828 影子系统事故后强制路由已移除。）
_DISPLAY_DEDUP_SECONDS = 30.0
# ⚠️ 20260917 加锁：此前是裸 dict 的 check-then-act——两个并发请求可以同时通过
# "30s 内没发过"的检查，同一条屏显下发两次（外部审计指出；影响面只在这条幂等优化
# 本身，不是越权）。execute 跑在线程池里、工具会被并发调用，所以这个锁是必需的。
_last_display: dict[int, tuple[str, float]] = {}
_last_display_lock = threading.Lock()


def _sign_user_jwt(user_id: int) -> str:
    """用与博客相同的 JWT_SECRET 签发 HS256 JWT（sub=user_id，5 分钟有效），
    供 device-service 鉴权与设备归属校验（用户只能操作自己的设备）。"""
    def _b64(b: bytes) -> bytes:
        return base64.urlsafe_b64encode(b).rstrip(b"=")

    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": user_id,
        "exp": int(time.time()) + 300,
        "role": "user",
    }).encode())
    sig = _b64(hmac.new(JWT_SECRET.encode(), header + b"." + payload, hashlib.sha256).digest())
    return (header + b"." + payload + b"." + sig).decode()


def _device_get_user_id(config: RunnableConfig) -> int:
    """从运行时 config 取对话用户 id（server.py 注入 configurable.user_id）。"""
    return int(config.get("configurable", {}).get("user_id") or 0)


def _valid_device_id(device_id: str) -> bool:
    return bool(device_id) and all(c.isalnum() or c in "-_" for c in device_id)


@tool
def list_devices(config: RunnableConfig) -> str:
    """列出当前登录用户拥有的 IoT 设备（ESP32 等），返回设备 id、名称、在线状态。"""
    uid = _device_get_user_id(config)
    if uid <= 0:
        return unavailable("无法获取当前用户身份，设备列表不可用")
    try:
        resp = httpx.get(
            f"{DEVICE_SERVICE_URL}/api/devices",
            headers={"Authorization": "Bearer " + _sign_user_jwt(uid)},
            timeout=10,
        )
        if resp.status_code == 401:
            return unavailable("设备服务认证失败（JWT 无效或过期）")
        devices = resp.json()
        if not devices:
            return empty("当前用户还没有绑定任何 IoT 设备")
        return "\n".join(
            f"- id={d.get('id')} 名称={d.get('name')} 在线={'是' if d.get('online') else '否'}"
            for d in devices
        )
    except Exception as e:
        return unavailable(f"查询设备列表失败: {e}")


@tool
def device_oled_display(
    text: Annotated[str, "要在 ESP32 OLED 屏幕上显示的文字内容"],
    config: RunnableConfig,
    device_id: Annotated[str | None, "设备 id（可选；不填时自动选择当前用户第一个在线设备）"] = None,
) -> str:
    """在 ESP32 OLED 小屏幕上显示一段文字（经 MQTT 指令实时下发到设备）。
    指令下发后设备即收到，实际显示效果以设备端为准。"""
    # 注意：config 必须是精确的 RunnableConfig 类型（无默认值）——框架按类型注入，
    # 写成 Optional[RunnableConfig] 会破坏注入导致拿不到 user_id
    uid = _device_get_user_id(config)
    if uid <= 0:
        return "无法获取当前用户身份，指令未下发"
    if not text or len(text) > 64:
        return "显示内容为空或超过 64 字符限制"
    # 幂等去重：30s 内相同用户相同内容不重复下发（防多轮重复调用、QoS1 重投）
    now = time.time()
    with _last_display_lock:
        prev = _last_display.get(uid)
        if prev and prev[0] == text and now - prev[1] < _DISPLAY_DEDUP_SECONDS:
            return "该内容刚刚已下发过，无需重复下发（执行结果以设备回执为准）"
        # **下发前就占位**（不是发完再记）：占位与检查在同一把锁里，两个并发请求
        # 只有一个能过——这正是原来那版缺的一步。失败路径会在下面把它撤掉。
        _last_display[uid] = (text, now)
    sent = False
    try:
        # device_id 未指定时自动选择第一个在线设备（多步工具链是 IoT 工具失败的
        # 结构性原因：模型无法从 schema 知道运行时才能获取的 device_id，参数缺失时
        # 倾向文本声称而非如实失败。单步化后模型一次调用即成功）
        if not device_id:
            resp = httpx.get(
                f"{DEVICE_SERVICE_URL}/api/devices",
                headers={"Authorization": "Bearer " + _sign_user_jwt(uid)},
                timeout=10,
            )
            devices = resp.json()
            online = [d for d in devices if d.get("online")]
            chosen = (online or devices)[0] if devices else None
            if chosen is None:
                return empty("当前用户还没有绑定任何 IoT 设备")
            device_id = chosen.get("id")
            if not device_id:
                return "设备列表返回异常，无法获取设备 id"
        if not _valid_device_id(device_id):
            return "设备 id 格式非法"
        # 链路关联（图改进Ⅳ）：请求 trace_id 透传 device-service（其日志/回执带同一 id）
        headers = {"Authorization": "Bearer " + _sign_user_jwt(uid)}
        tid = get_trace_id()
        if tid and tid != "-":
            headers["X-Request-Id"] = tid
        resp = httpx.put(
            f"{DEVICE_SERVICE_URL}/api/devices/{device_id}/cmd",
            headers=headers,
            json={"type": "display", "text": text},
            timeout=10,
        )
        if resp.status_code == 404:
            return "设备不存在或不属于当前用户"
        if resp.status_code == 409:
            return "设备当前不在线，无法显示该内容（设备可能断电或 MQTT 连接断开）"
        if resp.status_code != 200:
            return f"指令下发失败（HTTP {resp.status_code}）: {resp.text[:100]}"
        sent = True          # 已真正下发 ⇒ 占位保留，30s 内的重复调用会被去重
        # 回执确认（图改进Ⅴ）：幽灵在线窗口（断电→遗嘱到达前，曾达 ~2 分钟）内下发
        # 会"假成功"——publish 入队即 200，设备实际收不到。轮询 device-service 的
        # 回执状态接口，5s 内设备回执即确认执行，否则如实告知"未确认"，不再承诺已显示。
        rid = None
        try:
            rid = resp.json().get("req_id") or None
        except Exception:
            rid = None
        if rid:
            for _ in range(5):
                time.sleep(1)
                try:
                    st = httpx.get(
                        f"{DEVICE_SERVICE_URL}/api/devices/{device_id}/cmd/{rid}",
                        headers={"Authorization": "Bearer " + _sign_user_jwt(uid)},
                        timeout=5,
                    )
                    if st.status_code == 200 and st.json().get("acked"):
                        return "OLED 显示指令已下发，设备已确认执行（回执已记录）"
                    if st.status_code == 401:
                        break  # 查询鉴权失效，不再等待
                except Exception:
                    break  # 查询接口异常，不再等待
            return "指令已入队下发，但设备未在 5 秒内回执确认——设备可能已断电或 MQTT 连接断开，请稍后到设备控制台确认"
        return "OLED 显示指令已下发"
    except Exception as e:
        return f"指令下发失败: {e}"
    finally:
        # **没真正下发就把占位撤掉**：否则一次失败（设备离线/HTTP 错/身份缺失）会把
        # 30s 内的正常重试也一并挡掉——那是加锁时最容易引入的行为回归。
        # 撤之前核对占位还是自己那一条，别误撤别人后来占的。
        if not sent:
            with _last_display_lock:
                if _last_display.get(uid) == (text, now):
                    _last_display.pop(uid, None)


# ---------------------------------------------------------------------------
# 管理助手报表工具（20260921）
# ---------------------------------------------------------------------------
# 四个只读工具，scope 全声明为 `admin.console`（agent/authz.py）⇒ **只有 admin 用得动**。
# 三道闸由粗到细：
#   ① 结构性：不进 planner 的两个点名白名单（skills._EXPLICIT_TOOLS /
#      _CALLABLE_QUERY_TOOLS）⇒ planner 无法经 PARAMS.calls/tools 点到它们，
#      只能由 ops_report / moderation_report / user_report 三个技能模板展开；
#   ② 身份：会话角色不是 admin 时 planner 上下文里**根本看不到**这三个技能
#      （skills.build_planner_context(role)）；
#   ③ 判据：execute 调用前的 authz.check()。`admin.console` **不吃 shadow 开关**
#      （authz._HARD_SCOPES）——它是本轮纯新增的能力，没有"观测既有流量"可谈，
#      shadow 期也硬拦。
# 通道分两类：
#   · 本机读数（服务器状态/服务健康）：agent 与生产服务同机，直接读 /proc、systemctl、
#     日志即可，不经后台门、不消耗发起人的身份；
#   · 后台读（审核状况/用户统计）：走「以发起人身份代调」——现签 60 秒 JWT 打本机
#     Rust，`auth_guard` 照旧按 claims.sub 查库判角色。**Rust 侧零改动**，agent 自己
#     不持有任何后台凭据。
#
# 为什么这几个工具返回"渲染好的中文报表"而不是 `_shape(data)`（绕过既有惯例）：
# 见 agent/reports.py 的模块头注——LLM 数数是幻觉高发区，报表的价值全在数字可信。

ADMIN_BASE = _settings.agent_admin_base


def _sign_local_jwt(uid: int, role: str | None) -> str:
    """以**发起人**身份签一个 60 秒的 HS256 JWT，供本机后台接口鉴权。

    payload 与 `_sign_user_jwt`（device-service 那条路径）刻意一致：
    `{sub, exp, role}` 且**不带 aud**——Rust `auth_jwt::verify_token` 用
    `Validation::default()`，多一个 aud 会被判 audience 无效直接验签失败。

    ⚠️ token 里的 `role` **只为日志可读，没有任何权威**：Rust `auth_guard` 一律按
    `claims.sub` 现查库里的角色（middleware.rs 的注释写了同一件事）。密钥是共用的
    JWT_SECRET，所以这里确实是能签出"看起来像 admin"的 token——但签了也没用，
    这正是"以发起人身份代调"成立的原因：**准入结果由库里的 role 决定，不由 token 决定**。

    **不带 `ver`（20260926）是刻意的，不要"补全"它**：Rust 那边 20260926 给登录令牌
    加了代次声明 `ver`（改密码/冻结即作废旧令牌，见 `auth_jwt::Claims`），但 `ver`
    取 `Option`：**没有这个声明**的令牌跳过代次比对、只判账号是否冻结。这里必须保持
    "没有"——这枚令牌代表的是 Rust **本次请求刚认证过**的身份，比任何代次都新鲜；
    若给它填一个猜的代次（比如 0），那么"改过密码的管理员 + 管理助手"这个组合会整体
    401（`token_version` 早已不是 0 了）。填 0 是错的、抄一个库值也无从抄起。

    有效期 60 秒（设备那条是 300）：这是一个当场用掉的请求，没有"持有一段时间"的
    场景，短一点少一分被复用的余地。
    """
    def _b64(b: bytes) -> bytes:
        return base64.urlsafe_b64encode(b).rstrip(b"=")

    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": uid,
        "exp": int(time.time()) + 60,
        "role": role or "user",
    }).encode())
    sig = _b64(hmac.new(JWT_SECRET.encode(), header + b"." + payload, hashlib.sha256).digest())
    return (header + b"." + payload + b"." + sig).decode()


def _principal_get(path: str, config: RunnableConfig,
                   *, uid_msg: str, deny_msg: str) -> dict | list | ToolResult:
    """以发起人身份 GET 一个"需要身份"的接口（`/api/protected/*`），返回其 data 字段。

    fail-closed 与 `_get` 同族：异常 / 非 200 / 业务码非 200 一律 unavailable，
    **绝不返回空**——"读不到"被当成"就是空的"是这批工具最坏的失败形态
    （报表会言之凿凿地说"没有待审留言"，而实际是一条都没读到）。
    401/403 单独给一句如实的话：那是**身份**问题不是故障，agent 要能据此说出
    "只有管理员能看"，而不是"系统出错了"。

    20260923 从 `_admin_get` 抽出本体：请求形状完全一样（同一把 JWT_SECRET 代签、
    同一条 `/api/protected` 前缀、同一套判据），差别只在**读不到时该说的话**——
    所以两句话由调用方给（`uid_msg` = 拿不到身份，`deny_msg` = 401/403）。措辞必须
    分开：收藏/通知读不到时对访客说"仅管理员可用"是错的（那是**他自己**的数据），
    反过来把后台报表说成"你未登录"更糟。
    """
    uid = _device_get_user_id(config)
    if uid <= 0:
        return unavailable(uid_msg)
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.get(f"{ADMIN_BASE}{path}", headers=headers, timeout=15)
    except Exception as exc:
        logger.error("principal API call failed: %s", exc)
        return unavailable(f"接口请求失败: {exc}")
    if resp.status_code in (401, 403):
        return unavailable(deny_msg)
    if resp.status_code != 200:
        return unavailable(f"接口返回 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception:
        return unavailable("接口返回的不是 JSON")
    if body.get("code") != 200:
        logger.warning("principal API error: %s", body.get("message"))
        return unavailable(f"接口报错: {body.get('message')}")
    return body.get("data")


def _admin_get(path: str, config: RunnableConfig) -> dict | list | ToolResult:
    """后台接口（管理员读）。scope = admin.console。"""
    return _principal_get(
        path, config,
        uid_msg="无法获取当前用户身份，后台数据不可用",
        deny_msg="当前身份无权访问后台数据（该功能仅管理员可用）")


def _own_get(path: str, config: RunnableConfig) -> dict | list | ToolResult:
    """用户**自己**的数据（收藏 / 通知 / 未读汇总）。scope = read.own（全角色）。

    uid<=0 = 访客没登录。这里刻意不返回 empty()（"你的收藏是空的"是编造——我们
    根本没读到）也不返回 ok()（读不到不是事实），而是 unavailable + 如实措辞；
    与既有的 `list_devices`（"无法获取当前用户身份，设备列表不可用"）同一取向。

    已知的粗糙处（记下来，别当成没想到）：这条走 kind=unavailable ⇒ checker 判
    BLOCK ⇒ 过程行按 `_REASON_CN["unavailable"]` 显示「服务不可用」。对"你没登录"
    来说这个词不准确（访客看的是过程行，最终回复由 narrator 按帧里的实话写）。
    改它要动原因码族（`_check_spec` + `_REASON_CN`），留给"未登录"这条 golden
    用例跑出实际观感后再定。
    """
    return _principal_get(
        path, config,
        uid_msg="未登录：读不到你自己的数据（需要先登录博客账号）",
        deny_msg="当前身份无权读取该数据")


@tool
def get_server_status() -> str:
    """查看服务器运行状态：CPU 核数与使用率、1/5/15 分钟负载、内存与 Swap 用量、
    各磁盘用量与可用空间、开机时长。数据由 agent 直接读本机 /proc 得到，不经后台接口。"""
    # 延迟导入：tools.base 在 `agent` 包的导入链**上游**（agent/__init__ → agent.agent
    # → agent.graph → tools），模块级 `from agent import …` 会构成循环导入。
    # 同先例见 rag/search.py 的 `from tools.base import _get`。
    from agent import hostinfo as H
    from agent import reports as R
    try:
        cpu = H.cpu_percent_over()
        mem = H.mem_summary(H.parse_meminfo(H.read_meminfo()))
        if cpu is None and not mem:
            # 两样都读不到 = 这根本不是一台正常机器，如实说"读不到"而不是给一张空报表
            return unavailable("读取服务器状态失败：本机 /proc 不可读")
        return ok(R.render_server_status(
            cpu_pct=cpu,
            cores=os.cpu_count() or 0,
            load=H.parse_loadavg(H.read_loadavg()),
            mem=mem,
            disks=H.disk_rows(),
            uptime_s=H.read_uptime(),
        ))
    except Exception as exc:
        logger.exception("get_server_status failed")
        return unavailable(f"读取服务器状态失败: {exc}")


@tool
def get_service_health() -> str:
    """查看服务健康：三个 systemd 服务（saudade-rust / saudade-agent / saudade-device）
    的状态、重启次数与启动时刻，心跳探针近 24 小时的 WARN/FAIL，今日对话的轮数、
    异常收尾与质检拦截，以及存活日志的体积。读的是本机的 systemctl 与 logs/ 目录。"""
    from agent import hostinfo as H
    from agent import reports as R
    try:
        services = [(unit, H.service_show(unit)) for unit in H.SERVICES]
        health = H.parse_health_log(H.read_text_tail(H.HEALTH_LOG),
                                    H.health_window(24))
        return ok(R.render_service_health(
            services=services,
            health=health,
            traces=H.trace_stats(),
            sizes=H.log_sizes(),
        ))
    except Exception as exc:
        logger.exception("get_service_health failed")
        return unavailable(f"读取服务健康失败: {exc}")


@tool
def get_moderation_status(
    config: RunnableConfig,
    status: Annotated[str | None,
                      "只列某一类明细：ai_passed=AI 直接通过的 / ai_rejected=AI 驳回的 / "
                      "pending=需要人工复批的；不填则三类各列最近几条（其余只计数）"] = None,
) -> str:
    """查看河灯留言的审核状况：总数与待审/已通过/已驳回的分布、AI 侧判定分布
    （AI 通过 / AI 驳回 / AI 存疑转人工 / 未走 AI），以及三份可读名单——
    ① AI 直接通过的 ② AI 驳回的（并说明人工是维持驳回、改判放行还是仍在等）
    ③ 需要人工复批的（并说明是 AI 存疑还是 AI 通过后才等人看）。
    追问"把被驳回的/等人复批的都列出来"时用 status 参数聚焦某一类（列得更多）。
    需要管理员身份（读的是后台留言管理视图）。"""
    from agent import reports as R
    data = _admin_get("/api/protect/board", config)
    if isinstance(data, ToolResult):
        return data
    if not data:
        return empty("河灯留言板目前还没有任何留言，没有可审核的内容")
    try:
        return ok(R.render_moderation_status(data, status=status))
    except Exception as exc:
        logger.exception("render_moderation_status failed")
        return unavailable(f"整理审核状况失败: {exc}")


@tool
def get_user_stats(config: RunnableConfig) -> str:
    """查看全站用户数据报表：用户总数与角色分布、会话/消息/执行回执的总量、
    近 7 天与近 30 天的活跃人数、以及按消息数倒序的用户明细（最多 50 行）。
    需要管理员身份。口径是"活动"不是"注册"（系统没存注册时间）。"""
    from agent import reports as R
    data = _admin_get("/api/protected/stats/users", config)
    if isinstance(data, ToolResult):
        return data
    if not data:
        return empty("后台没有返回用户统计数据")
    try:
        return ok(R.render_user_stats(data))
    except Exception as exc:
        logger.exception("render_user_stats failed")
        return unavailable(f"整理用户报表失败: {exc}")


# ---------------------------------------------------------------------------
# 管理助手写工具（20260921 第二轮：标签创建 / 文章状态 / 打标签）
# ---------------------------------------------------------------------------
# 与上一节同一条通道（以发起人身份代调），但方向是**写**：scope 全声明为
# `write.console`（agent/authz.py）⇒ 只有 admin 用得动，且**每次都要当轮命令**。
# 四道闸：① 结构性（不进 planner 的点名白名单，只能由 tag_create/article_status/
# article_tags 三个技能模板展开）② 身份（非 admin 在 planner 上下文里看不到技能）
# ③ authz.check() 硬拦（write.console 进 _HARD_SCOPES，不吃 shadow 开关）
# ④ 同意（write.console 进 CONSENT_SCOPES：用户本轮消息必须是**命令句**）。
#
# ★ 这一节最重要的一条纪律：**一切"没做成"都必须 unavailable()**。
# checker（graph._check_spec）对**非空文本**一律判 PASS，而 PASS 会被记成
# **系统确认事实**落进 receipts → execution_log → 下一轮注入 narrator 的上下文。
# 所以"创建失败，请到后台重试"这种 `ok(...)` 会被下一轮的自己念成"已创建"。
# 反过来，`empty("")` 会被判 empty_result 而 BLOCK ⇒ "零写成功"的返回**也不能是空串**。
#
# 另一个坑：Rust 的 `ApiResponse::error` 是 **HTTP 200 + code 500**（src/utils.rs），
# 所以 `_admin_post` 必须看业务码，只判状态码会把"创建失败"读成成功。

def _principal_request(method: str, path: str, payload, config: RunnableConfig,
                       *, label: str, uid_msg: str, deny_msg: str, tail: str):
    """以发起人身份请求一个"需要身份"的接口（`/api/protected/*`），返回其 data 字段。

    fail-closed 与 `_admin_get` 同族，且**更严**：任何一条不确定路径都返回
    unavailable（= 不是事实、checker BLOCK、不进跨轮执行记忆），措辞里明确说
    "本次改动未确认生效"，因为下游 narrator 要靠这句话如实告知用户。

    `label`/`uid_msg`/`deny_msg`/`tail` 由调用方给，与 `_principal_get` 抽出本体
    是同一条理由：**请求形状一样、读不到时该说的话不一样**。后台写说"仅管理员
    可用"是对的；用户改自己的收藏这么说就是错的。

    20260923 从 `_admin_request` 抽出本体（后者原样保留为薄封装，四条消息逐字节
    不变——它们是既有测试与线上回执的措辞）。
    """
    uid = _device_get_user_id(config)
    if uid <= 0:
        # **身份不明时一个请求都不发**（写操作最不该做的就是在没身份时猜）。
        # 这条同时是 golden 负向用例的安全底座：role=admin 但 uid=0 时，即便
        # planner 误规划了写，请求也走不出这个进程（线上生产库零真写）。
        return unavailable(uid_msg)
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.request(method, f"{ADMIN_BASE}{path}",
                               headers=headers, json=payload, timeout=15)
    except Exception as exc:
        logger.error("principal %s %s failed: %s", method, path, exc)
        return unavailable(f"{label}接口请求失败: {exc}{tail}")
    if resp.status_code in (401, 403):
        return unavailable(deny_msg)
    if resp.status_code != 200:
        return unavailable(f"{label}接口返回 HTTP {resp.status_code}{tail}")
    try:
        body = resp.json()
    except Exception:
        return unavailable(f"{label}接口返回的不是 JSON{tail}")
    if body.get("code") != 200:
        # ⚠️ 见本节头注：只看 HTTP 状态码会把 Rust 的 ApiResponse::error 当成功。
        logger.warning("principal %s %s error: %s", method, path, body.get("message"))
        return unavailable(f"{label}接口报错: {body.get('message')}{tail}")
    return body.get("data")


# 写操作失败路径的固定尾句：下游 narrator 靠它如实告知"别声称已改好"。
_NO_SUCCESS_TAIL = "（本次改动未确认生效，不要声称已改好）"

# 没登录时写通道的固定措辞（**只此一处**：`_own_request` 的 uid_msg 与写工具入口
# 的哨兵共用它——写工具的第一件事是"写前读"，那一步走的是读通道，读不到时说的是
# "读不到你自己的数据"，对一次写命令来说那句话不完整：用户要知道的是"没给你改"）。
_NO_LOGIN_WRITE = "未登录：本次未改动任何内容（需要先登录博客账号）"


def _own_write_guard(config: RunnableConfig) -> ToolResult | None:
    """写工具入口的统一哨兵：没身份 → 直接返回，调它之前**一个请求都不发**。

    写操作最不该做的就是在没身份时猜"写给谁"（"我以为给谁写了"比"没写成"坏得多）。
    """
    if _device_get_user_id(config) <= 0:
        return unavailable(_NO_LOGIN_WRITE)
    return None


def _pre_read_fail(result: ToolResult, what: str) -> ToolResult:
    """写**前**读失败 → 明说"本次未改动"（读侧那句原话保留在括号里，便于排查）。"""
    return unavailable(f"读不到{what}（{result}），本次未改动")


def _admin_request(method: str, path: str, payload, config: RunnableConfig):
    """以发起人身份请求一个**后台**写接口（scope = write.console）。"""
    return _principal_request(
        method, path, payload, config,
        label="后台",
        uid_msg="无法获取当前用户身份，本次改动未执行",
        deny_msg="当前身份无权改动后台数据（该功能仅管理员可用），本次未改动任何内容",
        tail=_NO_SUCCESS_TAIL)


def _own_request(method: str, path: str, payload, config: RunnableConfig):
    """以发起人身份写**用户自己**的数据（scope = write.own：收藏 / 标记已读）。

    与 `_admin_request` 是同一条通道的两种措辞，判据一样严（任何不确定路径都
    unavailable）。uid<=0 时一个字节都不发：写操作最不该做的就是在没身份时猜——
    对访客来说，"我以为给谁写了"比"没写成"坏得多。
    """
    return _principal_request(
        method, path, payload, config,
        label="",
        uid_msg=_NO_LOGIN_WRITE,
        deny_msg="当前身份无权改动该数据，本次未改动任何内容",
        tail=_NO_SUCCESS_TAIL)


def _own_post(path: str, payload, config: RunnableConfig):
    """POST 的薄封装（用户自己的数据）。"""
    return _own_request("POST", path, payload, config)


def _own_delete(path: str, config: RunnableConfig):
    """DELETE 的薄封装（用户自己的数据）。`payload` 传 None（端点从路径取参数）。"""
    return _own_request("DELETE", path, None, config)


def _admin_post(path: str, payload: dict, config: RunnableConfig):
    """POST 的薄封装（既有调用点保持原样）。"""
    return _admin_request("POST", path, payload, config)


def _admin_status_post(path: str, payload: dict, config: RunnableConfig):
    """冻结/解冻专用的 POST：把**非 200 业务码无条件读成"后台规则拒绝"**。

    为什么不能直接用 `_admin_request`：那条（= `_principal_request`）对**任何**
    `code != 200` 都返回 `unavailable("接口报错: …")` ⇒ 后端的政策拒绝（「不能冻结
    超级管理员账号」这类）被归成**服务不可用** ⇒ 过程行显示「服务不可用」、planner
    收到"稍后再试"的指引 ⇒ 它照着这句话重试，而这条请求**永远不会**成功。
    这个端点的非 200 只有三族（政策拒绝 / 客户不存在 / 角色未登记），**没有一族是
    "服务不可用"**，所以无条件按政策拒绝出口是对的，也比按消息字符串匹配稳。

    文案**逐字转述后端原话、不二次改写**：政策会变，agent 侧任何复述都会在政策
    变更那天变成假话（那几句已登记为跨语言契约，见 docs/security-boundary.md §7⑫）。
    """
    from agent import adminops as A
    uid = _device_get_user_id(config)
    if uid <= 0:
        # 身份不明时一个请求都不发（同 `_principal_request`）：写操作最不该做的
        # 就是在没身份时猜。
        return unavailable("未登录：本次未改动任何内容（需要先登录博客账号）")
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.post(f"{ADMIN_BASE}{path}", headers=headers, json=payload, timeout=15)
    except Exception as exc:
        logger.error("admin status post %s failed: %s", path, exc)
        return unavailable(f"接口请求失败: {exc}{_NO_SUCCESS_TAIL}")
    if resp.status_code in (401, 403):
        return unavailable("当前身份无权改动账号状态（该功能仅管理员可用），本次未改动任何内容")
    if resp.status_code != 200:
        return unavailable(f"接口返回 HTTP {resp.status_code}{_NO_SUCCESS_TAIL}")
    try:
        body = resp.json()
    except Exception:
        return unavailable(f"接口返回的不是 JSON{_NO_SUCCESS_TAIL}")
    if body.get("code") != 200:
        logger.warning("admin status post %s refused: %s", path, body.get("message"))
        # ⚠️ 必须包成 `ToolResult` 再往外传（**不是**裸字符串）：调用方的失败判据是
        # `isinstance(data, ToolResult)`，而 `policy_frame` 返回的是普通 str——
        # 裸传会被当成"成功返回的 data"接着往下走写后复核，最后报成
        # 「改动请求已发出…本次改动未确认生效」（一句**假话**：这条请求根本没改任何
        # 东西，它是被政策拒了）。kind 仍是 "ok"：这一族的判据是**帧的形态**
        # （`__ERROR__:` 前缀）而不是 kind，`_check_spec` 照旧 BLOCK + policy_refused。
        return ToolResult(A.policy_frame(str(body.get("message") or "后台拒绝了这次改动")))
    return body.get("data")


def _admin_todo_done_post(payload: dict, config: RunnableConfig):
    """待办"翻完成标记"专用的 POST：把**非 200 业务码无条件读成"目标类失败"**。

    为什么不能直接用 `_admin_request`：那条（= `_principal_request`）对**任何**
    `code != 200` 都返回 `unavailable("接口报错: …")` ⇒ 后端的"查无此条 / 有多条
    同名"被归成**服务不可用** ⇒ 过程行显示「服务不可用」、planner 收到"稍后再试"
    的指引 ⇒ 它照着这句话**重试同一条**，而这条请求**永远不会**成功（与
    `_admin_status_post` 头注里那条一模一样的坑）。这个端点的非 200 只有两族
    （查无此条 / 多条同名）+ 存储故障，**没有一族是"目标不存在之外的服务不可用"
    值得重试**，所以一律走 `not_found`：planner 拿到这个原因码才会去读列表、
    换一个正文，或如实告诉主人"列表里没有这一条"。

    文案**逐字转述后端原话**（同 `_admin_status_post`）：定位判据在服务端，agent
    侧任何改写都会在判据变更那天变成假话。
    """
    uid = _device_get_user_id(config)
    if uid <= 0:
        # 身份不明时一个请求都不发（同 `_principal_request`）。
        return unavailable(_NO_LOGIN_WRITE)
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.post(f"{ADMIN_BASE}/api/protected/todos/done",
                            headers=headers, json=payload, timeout=15)
    except Exception as exc:
        logger.error("admin todo done post failed: %s", exc)
        return unavailable(f"接口请求失败: {exc}{_NO_SUCCESS_TAIL}")
    if resp.status_code in (401, 403):
        return unavailable("当前身份无权改动后台待办（该功能仅管理员可用），本次未改动任何内容")
    if resp.status_code != 200:
        return unavailable(f"接口返回 HTTP {resp.status_code}{_NO_SUCCESS_TAIL}")
    try:
        body = resp.json()
    except Exception:
        return unavailable(f"接口返回的不是 JSON{_NO_SUCCESS_TAIL}")
    if body.get("code") != 200:
        # ⚠️ 必须包成 `ToolResult`（`not_found` 就是），**不是**裸字符串：调用方的
        # 失败判据是 `isinstance(data, ToolResult)`，裸传会被当成"成功返回的 data"
        # 接着往下走写后复核，最后报成「请求已发出…本次改动未确认生效」——一句假话
        # （这条请求根本没改任何东西，它被定位判据挡下了）。同 `_admin_status_post`。
        logger.warning("admin todo done post refused: %s", body.get("message"))
        return not_found(str(body.get("message") or "后台没找到这一条待办"))
    return body.get("data")


# 后端「给单个账号发通知」这批话术里属于**目标类**的两句（跨语言契约，见
# `src/routes/temp_user.rs` 的头注与 docs/security-boundary.md §7⑫）。按它分族的理由
# 见 `_admin_notice_post`——**认不出来的一律走 unavailable**，方向是硬要求。
_NOTICE_TARGET_REFUSALS = ("用户不存在", "该账号不能接收通知")


def _admin_notice_post(target_id: int, title: str, content: str, config: RunnableConfig):
    """给单个账号发通知专用的 POST：非 200 业务码**按"是不是目标类"分两族**。

    这个端点的非 200 有四种原因（`temp_user::send_user_notice` 逐条校验），落到这里
    只剩两族的区分有意义：

      · **目标类**（「用户不存在」「该账号不能接收通知」）⇒ `not_found`：planner 拿到
        这个原因码才会去问主人 / 换一个账号，**不会**把"没这个人"读成"稍后再试"；
      · **其余一律 unavailable**（含存储故障「通知发送失败，请稍后再试」、以及任何
        我们没见过的措辞）⇒ 明说"未确认"，不替后端断言一个我们并不知道的原因。

    方向刻意与 `_admin_status_post`（冻结族，非 200 **无条件**读成政策拒绝）不同：
    那一族的非 200 **没有一族是"服务不可用"**，所以无条件按政策拒绝出口是对的；这一族
    有真·存储故障，一律按目标类出口会把"库写失败"说成"没这个账号"——一句假话，而且
    收件人是**别人**，说错方向的代价比冻结族更高（冻错方向还有读回复核兜着，这条没有）。

    按消息字符串匹配这件事本身有代价（后端改字就认不出）。接受的依据是**兜底方向**：
    认不出 ⇒ unavailable（"没确认"），永远不会断言一个假的"没有这个账号"。那两句已登记
    为跨语言契约，改它们要走 `docs/security-boundary.md §7⑫` 那条同步流程。
    """
    uid = _device_get_user_id(config)
    if uid <= 0:
        # 身份不明时一个请求都不发（同 `_principal_request`）。
        return unavailable(_NO_LOGIN_WRITE)
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.post(f"{ADMIN_BASE}/api/temp-users/{target_id}/notice",
                            headers=headers, json={"title": title, "content": content},
                            timeout=15)
    except Exception as exc:
        logger.error("admin notice post failed: %s", exc)
        return unavailable(f"接口请求失败: {exc}{_NO_SUCCESS_TAIL}")
    if resp.status_code in (401, 403):
        return unavailable("当前身份无权给账号发通知（该功能仅管理员可用），本次未发送")
    if resp.status_code != 200:
        return unavailable(f"接口返回 HTTP {resp.status_code}{_NO_SUCCESS_TAIL}")
    try:
        body = resp.json()
    except Exception:
        return unavailable(f"接口返回的不是 JSON{_NO_SUCCESS_TAIL}")
    if body.get("code") != 200:
        msg = str(body.get("message") or "").strip()
        logger.warning("admin notice post refused: %s", msg)
        if any(k in msg for k in _NOTICE_TARGET_REFUSALS):
            return not_found(msg or "后台没有这个账号，本次未发送")
        return unavailable(f"{msg or '后台没有接受这次发送'}{_NO_SUCCESS_TAIL}")
    return body.get("data")


def _tag_index(config: RunnableConfig):
    """读两级标签字典 → `{id: TagInfo}`；任一读不到 → **None**。

    None 不是"没有标签"，是"这次读不到字典"——调用方必须分开处理（见
    adminops.render_tag_list 的同名区分）。返回 None 而不是抛，是因为"字典读不到"
    在只读场景（渲染清单）不该让整次查询失败，而在写场景由调用方直接拒绝。
    """
    from agent import adminops as A
    one = _admin_get("/api/tagone", config)
    two = _admin_get("/api/tagtwo", config)
    if isinstance(one, ToolResult) or isinstance(two, ToolResult):
        return None
    return A.build_tag_index(one, two)


def _read_note(article_id: int, config: RunnableConfig) -> dict | None | ToolResult:
    """读一篇文章的后台现状（**写操作的唯一读写口径**）。

    为什么不用 `/api/protected/draft/editor/:id`：那一条对「编辑修改稿」行会**解引用
    成原文章**，而 `update_note` 只发 `{status,isTop}` 时写的是**被点的那一行本身**
    ——读的行和写的行不是同一行，前值全假；更坏的是把修改稿行写成 status=public 后，
    公开列表（只滤 is_public/draft，不滤 draft_of）会多出一篇同标题文章。
    `/api/protected/notes/list` 过滤 `draft_of is null`，于是修改稿 id 在这里**读不到**
    → 调用方如实拒绝（"这不是一篇文章本体"），而不是猜。
    """
    data = _admin_get("/api/protected/notes/list", config)
    if isinstance(data, ToolResult):
        return data
    for n in (data or []):
        if isinstance(n, dict) and n.get("noteKey") == article_id:
            return n
    return None


def _note_index(config: RunnableConfig) -> dict[int, dict] | None:
    """后台文章清单 → `{noteKey: 行}`；读不到返回 None（**≠"一篇文章都没有"**，同 _tag_index）。

    与 `_read_note` 同一份数据源、同一口径（`/api/protected/notes/list`，含草稿/私密、
    不含「编辑修改稿」行）——确认弹窗要写的「现在是公开还是草稿」必须和写操作自己读
    前值的口径一致，否则弹窗里那句现状可能和真正被改的那一行不是同一行。

    None 与 `{}` 的区分是刻意的：`{}` = 后台确实一篇文章都没有（"后台清单里没有这一篇"
    是**事实**），None = 这次读不到（只能说"没核对上"，不许说成"站内没有"）。
    """
    data = _admin_get("/api/protected/notes/list", config)
    if isinstance(data, ToolResult):
        return None
    out: dict[int, dict] = {}
    for row in data or []:
        if not isinstance(row, dict):
            continue
        try:
            nid = int(row.get("noteKey"))
        except (TypeError, ValueError):
            continue
        row["noteKey"] = nid          # 就地归一成 int：下游 notes.get(id) 直接可用
        out[nid] = row
    return out


def _as_article_id(value) -> int | None:
    """实参 → 正整数 id；不合法 → None（调用方拒绝，不猜、不默认）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    s = str(value).strip()
    return int(s) if s.isdigit() and int(s) > 0 else None


@tool
def list_admin_notes(config: RunnableConfig) -> str:
    """查看后台文章清单：**包含未公开的草稿与私密文章**（公开接口一律看不到它们），
    每行给出 id、状态（公开/私密/草稿）、是否置顶、标签。要改某篇文章的状态或标签，
    先用它拿到**确切的 id**。需要管理员身份。"""
    from agent import adminops as A
    data = _admin_get("/api/protected/notes/list", config)
    if isinstance(data, ToolResult):
        return data
    notes = data if isinstance(data, list) else []
    if not notes:
        return empty("后台文章列表是空的（一篇文章都没有）")
    return ok(A.render_admin_notes(notes, _tag_index(config)), meta={"count": len(notes)})


@tool
def create_tag(
    title: Annotated[str, "新标签的名字（如「Python」「分布式」）"],
    config: RunnableConfig,
    parent_tag: Annotated[str | None,
                          "父标签的**名字**（建二级标签时给，如「编程」；建一级标签不填）；"
                          "**不是 id**"] = None,
    color: Annotated[str | None,
                     "用户点了名的颜色（中文色名如「粉色」，或站内色板色值如 #eb2f96）；"
                     "用户没说就不填——不填按标签名哈希取色，同名永远同色"] = None,
) -> str:
    """新建一个文章标签：不填 parent_tag 建**一级**标签，填了则在该一级标签下建**二级**标签。
    同名标签已存在时**不重复创建**，直接复用并告知它的 id。
    **本工具只往标签字典里加一项，不会把它挂到任何文章上**（挂标签是另一件事）。
    需要管理员身份。"""
    from agent import adminops as A
    name = str(title or "").strip()
    if not name:
        return unavailable("标签名为空，未创建")
    if len(name) > 40:
        return unavailable(f"标签名过长（{len(name)} 字，上限 40），未创建")

    index = _tag_index(config)
    if index is None:
        return unavailable("读不到现有的标签字典，无法确认是否重名，本次未创建")

    # 父标签：**按名字**解析成 id（20260921 第四轮）。为什么不收 id：用户嘴里说的
    # 就是名字，跨轮执行记忆里也只有名字没有 id（`list_tags` 的摘要不带编号），
    # 而"父标签 id 从哪来"曾是这条链上唯一解不开的环——planner 只可能猜一个数字。
    # 名字对不上就**拒绝**（零写），不新建、不做模糊匹配。
    pid = None
    pname = str(parent_tag or "").strip()
    if pname:
        parent, cands = A.find_tag(index, pname, level=1)
        if parent is None:
            if cands:
                return unavailable(
                    f"站内有两个同名的一级标签「{pname}」，无法确定是哪一个，本次未创建")
            return unavailable(f"站内没有叫「{pname}」的一级标签，本次未创建"
                               f"（要建的话先把它建出来，或者换个爸爸）")
        pid = parent.id

    # 先查后建（幂等）：同名同层 → 直接复用，不写库。
    hit, cands = A.find_tag(index, name, pid)
    # 复用只认**同一层**的同名标签：建一级时命中的若是二级同名标签，那不是"已存在"
    # （管理员要的是一个一级标签），而是"你可能想要那个子标签"——走下面的拒绝路径。
    # 给 pid 时 find_tag 已按 (level==2 且 father_id==pid) 过滤，故命中即同层。
    if hit is not None and (pid is not None or hit.level == 1):
        return ok(A.render_tag_reuse(hit), meta={
            "op": "tag_reuse", "tag_id": hit.id, "tag_name": hit.name, "level": hit.level})

    if pid is not None:
        parent = index.get(pid)
        # 参数对调守卫（20260921）：父标签的**名字与新标签名相同**时拒绝。
        # 生产实证：planner 把「在编程标签下新建 Rust」填成 title=编程、父也填编程
        # （父名当成了新标签名），若放行就会在「编程」下建出一个也叫「编程」的二级
        # 标签——库里多一条永远不该有的同名父子，而回复还会说"建好了"。这一判据不需要
        # 理解用户意图，只认"父子同名"这个自身矛盾的结构，误伤面 0（正常的二级标签
        # 不会与父同名：同名父子在做任何按名字找标签的操作时都是歧义）。
        # 名字通道下这条更容易撞上（两边都是名字，写串了就直接同名），所以留着。
        if parent is not None and parent.name == name:
            return unavailable(
                f"要新建的标签名「{name}」与父标签「{parent.name}」同名，这多半是把父标签名"
                f"当成了新标签名（「在{name}下面新建 X」里的 X 才是新标签的名字），本次未创建")
    elif cands or hit is not None:
        cands = cands or [hit]
        # 有同名标签、但都在二级（同一个或不同父下）——这不是"已存在"，是"你可能是
        # 想要那个"。直接建一个同名一级标签会让以后所有按名字找标签的操作都变歧义。
        return unavailable(
            f"站内已有同名**二级**标签「{name}」（{'、'.join(c.label + ' id=' + str(c.id) for c in cands)}）；"
            f"如果你要的是它，直接用它；如果确实要新建一个同名一级标签，请说明后再来。本次未创建")

    # 颜色（20260921）：点名了就用它，没点名按名字哈希（同名同色，前端手建同源）。
    # **点名的色认不出 → 拒绝**，不回落哈希——那会让用户拿到一个他没要的颜色，
    # 且屏幕上看着"确实建成了"。这是写操作 fail-closed 纪律的一部分。
    color_spec = str(color or "").strip()
    picked = A.match_tag_color(color_spec) if color_spec else None
    if color_spec and picked is None:
        return unavailable(f"颜色「{color_spec}」不在站内色板里（可选：{A.TAG_COLOR_SPEC}），未创建")
    color = picked or A.color_for_name(name)
    if pid is not None:
        path, payload = "/api/protected/tagtwo", {"title": name, "color": color, "fatherTag": pid}
    else:
        path, payload = "/api/protected/tagone", {"title": name, "color": color}
    data = _admin_post(path, payload, config)
    if isinstance(data, ToolResult):
        return data
    try:
        new_id = int(str(data).strip())
    except Exception:
        new_id = 0
    if new_id <= 0:
        return unavailable(f"后台没有返回新标签 id（返回 {data!r}），无法确认创建结果")

    # 建后复核：**读回来证明它真的在**，而不是相信返回值。理由：id 由库分配、
    # 创建与读取之间还可能撞上并发/约束问题；只有"字典里真有一行 id=新id 且名字一致"
    # 才算数（tag 列表接口 `unwrap_or(vec![])` 会把 DB 故障伪装成空表，读回是唯一的证）。
    after = _tag_index(config)
    if after is None:
        return unavailable(f"标签可能已创建（后台返回 id={new_id}），但读不回标签字典、无法确认——"
                           f"请到后台标签页核对后再决定是否重试")
    got = after.get(new_id)
    if got is None or got.name != name:
        return unavailable(f"创建后复核失败：标签字典里找不到 id={new_id} 且名字为「{name}」的行，"
                           f"本次改动未确认生效")
    return ok(A.render_tag_created(got), meta={
        "op": "tag_create", "tag_id": got.id, "tag_name": got.name, "level": got.level})


def _near_miss_names(want: str, cands, limit: int = 3):
    """名字的**近似候选**（近失）→ `[(id, 展示名), …]`，按"更像"排序。

    判据 = 去掉所有空白后**互相包含**（`want ⊂ 候选名` 或 `候选名 ⊂ want`），
    长短任一侧至少 3 字（2 字的名字包含一切，只会把清单变成噪声）。

    它只用来**把一次假否定换成一次可点名的追问**，绝不参与"要不要动手"：
    写操作仍然只认完全相等（见各调用方 docstring）。
    动机（20260925 现场）：主人点名的公告被节选截短成"管理员助手公告发布测试"，
    台账按标题**完全相等**查不到 ⇒ 工具回一句"站内没有标题是「…」的公告"——
    而站内明明有一条名字只差前三个字的公告。**"查不到"被说成了"没有"**，
    主人拿到的是一句假话加一次零写。近失候选把这句话变成可核对的事实：
    「没有完全同名的；最接近的是 id=14「泠月喵管理员助手公告发布测试」」。
    排序：先"候选名包含 want"（截断形态，最常见），再按长度差、再按 id。
    """
    w = re.sub(r"\s+", "", str(want or ""))
    if len(w) < 3:
        return []
    out = []
    for cid, name in cands:
        n = re.sub(r"\s+", "", str(name or ""))
        if len(n) < 3:
            continue
        if w in n or n in w:
            out.append((0 if w in n else 1, abs(len(n) - len(w)), cid, name))
    out.sort(key=lambda x: (x[0], x[1], x[2]))
    return [(cid, name) for _, _, cid, name in out[:limit]]


def _find_named_tag(name, config, level=None, role="标签", index=None):
    """按名字在标签字典里找一个标签 → `(TagInfo, None)` 或 `(None, 拒绝文本)`。

    这是**所有按名字改标签的工具共用的解析入口**（改 / 删 / 移动），也是"名字通道"
    落地的地方：planner 写名字，这里确定性换成 id 与层级。四种结局都响亮：
      · 唯一命中 → 交给调用方；
      · 命中多个（不同父下的同名二级）→ 请调用方追问，**不替用户选一个**
        （选错就是改错/删错数据）；
      · 一个都没有 → 如实说站内没这个标签，**并附上名字最接近的候选**请主人点名
        （`_near_miss_names`）；**绝不新建、绝不按模糊匹配动手**；
      · 字典读不到 → 单独一种说法（"读不到" ≠ "没有"）。
    `role` 只影响措辞（找父标签时说「一级标签」）；**给了 level 就自动说清是
    "一级里没有"还是"二级里没有"**——否则 planner 会以为这个名字站内根本不存在，
    去新建一个，而不是回来把 level 去掉。
    `index` 是**可选的外部快照**：一个工具要连着查两个名字（目标 + 父）时，
    用同一份快照查才有意义（两次读之间标签可能被挪走），也省一次网络往返。
    """
    from agent import adminops as A
    if index is None:
        index = _tag_index(config)
    if index is None:
        return None, "读不到现有的标签字典，无法把名字对应到标签，本次未改动"
    want = str(name or "").strip()
    if not want:
        return None, "标签名为空，本次未改动"
    lv = None
    if str(level or "").strip():
        lv = A.normalize_level(level)
        if lv is None:
            return None, f"层级「{level}」认不出来（只支持一级 / 二级），本次未改动"
        if role == "标签":
            role = "一级标签" if lv == "one" else "二级标签"
    hit, cands = A.find_tag(index, want, level=(1 if lv == "one" else 2) if lv else None)
    if hit is None:
        # 同名都在同一层（两个二级挂在不同父下）时，"说明是一级还是二级"帮不上忙
        # ——所以再给一条**能落下的**指认方式：用展示名（`父 / 子`）。这正是下面
        # 候选名单写出来的形态，用户照着念、planner 照着填都能对上。
        want_lv = (1 if lv == "one" else 2) if lv else None
        by_label = [t for t in index.values()
                    if t.label == want and (want_lv is None or t.level == want_lv)]
        if len(by_label) == 1:
            return by_label[0], None
    if hit is not None:
        return hit, None
    if cands:
        return None, (f"站内有 {len(cands)} 个叫「{want}」的{role}（"
                      + "、".join(c.label + f"（id={c.id}）" for c in cands)
                      + "）：无法确定要动的是哪一个，本次未改动——"
                        "请用「父 / 子」这样的全名指认它（或说明是一级还是二级）")
    near = _near_miss_names(want, [(t.id, t.label) for t in index.values()])
    if near:
        # 近失（20260925）：没有**同名**的，但名字接近——把候选摆出来请主人点名，
        # 而不是丢一句"站内没有"（那句话在主人的视角里是假的：他记得站内有）。
        return None, (f"站内没有叫「{want}」的{role}（完全同名的一个都没有）；"
                      + "名字最接近的是 "
                      + "、".join(f"{nm}（id={cid}）" for cid, nm in near)
                      + f"：本次未改动——若就是其中一个，请照它的**完整名字**再说一遍，"
                        f"我按那个名字动手")
    return None, f"站内没有叫「{want}」的{role}，本次未改动"


@tool
def update_tag(
    name: Annotated[str, "要改的那个标签的**名字**（站内已有的标签，如「Asyncio」）"],
    config: RunnableConfig,
    new_title: Annotated[str | None, "改成什么名字；不改名就不填"] = None,
    color: Annotated[str | None,
                     "改成什么颜色（中文色名如「粉色」，或站内色板色值）；不改颜色就不填"] = None,
    parent_tag: Annotated[str | None,
                          "把这个标签挪到哪个**一级标签**下面（写它的名字，如「编程」）；"
                          "不动位置就不填"] = None,
    to_level: Annotated[str | None,
                        "要把这个标签改成一级还是二级（one/two）；不改层级就不填。"
                        "改成二级（two）时必须同时给出 parent_tag"] = None,
    level: Annotated[str | None,
                     "这个标签现在是几级（one/two）；站内同名标签不止一个时用它指认"] = None,
) -> str:
    """修改一个已有标签：改名 / 改颜色 / 换父级 / 一级↔二级互转（可同时改几样）。
    **只动你点名的那几样**，没点名的保持不动。需要管理员身份。

    换父级与换层级走一条原子接口：**换父级时标签 id 与文章引用都不变**（可逆）；
    只有跨表（一级↔二级）且旧 id 在新表里已被占用时才会换 id，那种情况下所有引用
    过它的文章会被同步改写（返回里会说清楚）。"""
    from agent import adminops as A
    # 目标与父标签用**同一份**字典快照解析：两次读之间标签可能被挪走/改名，
    # 拿两个时刻的数据拼一个请求是自找的错；顺带少一次网络往返。
    index = _tag_index(config)
    hit, err = _find_named_tag(name, config, level, index=index)
    if err:
        return unavailable(err)

    new_title = str(new_title or "").strip()
    if new_title and len(new_title) > 40:
        return unavailable(f"新标签名过长（{len(new_title)} 字，上限 40），本次未改动")
    color_spec = str(color or "").strip()
    picked = A.match_tag_color(color_spec) if color_spec else None
    if color_spec and picked is None:
        return unavailable(f"颜色「{color_spec}」不在站内色板里（可选：{A.TAG_COLOR_SPEC}），"
                           f"本次未改动")

    cur = A.level_of(hit)
    tl = None
    if str(to_level or "").strip():
        tl = A.normalize_level(to_level)
        if tl is None:
            return unavailable(f"层级「{to_level}」认不出来（只支持一级 / 二级），本次未改动")
    pname = str(parent_tag or "").strip()
    father = hit.father_id
    target = cur
    if pname:
        if tl == "one":
            return unavailable(f"既说了挪到「{pname}」下面、又说了改成一级标签——"
                               f"一级标签没有父标签，这两个说法互相矛盾，本次未改动")
        parent, perr = _find_named_tag(pname, config, "one", role="一级标签", index=index)
        if perr:
            return unavailable(perr)
        if parent.id == hit.id:
            # 自环：后端会硬拒（CASCADE 会把刚插入的行一起删掉，标签彻底消失），
            # 这里先拦一道，报错更直白。
            return unavailable("不能把标签挂到它自己下面，本次未改动")
        father, target = parent.id, "two"
    elif tl:
        target = tl
        if tl == "two" and cur == "one":
            return unavailable("要改成二级标签就必须给出它挂在哪个一级标签下"
                               "（parent_tag 填那个一级标签的名字），本次未改动")
        if tl == "one":
            father = None
    moves = (target != cur) or (target == "two" and father != hit.father_id)
    if not new_title and not picked and not moves:
        return unavailable("没有指出要改什么（名字 / 颜色 / 父标签 / 层级），本次未改动")

    if moves:
        payload = {"level": cur, "id": hit.id}
        if target != cur:
            payload["toLevel"] = target
        if target == "two" and father is not None:
            payload["fatherTag"] = father
        if new_title:
            payload["title"] = new_title
        if picked:
            payload["color"] = picked
        data = _admin_request("POST", "/api/protected/tag/move", payload, config)
        if isinstance(data, ToolResult):
            return data
        res = data if isinstance(data, dict) else {}
        new_id = res.get("toId")
        if not isinstance(new_id, int) or new_id <= 0:
            return unavailable(f"后台没有返回标签移动后的 id（返回 {data!r}），"
                               f"本次改动未确认生效——请到后台标签页核对后再决定是否重试")
        after = _tag_index(config)
        if after is None:
            return unavailable(f"标签可能已经改好（后台返回 id={new_id}），但读不回标签字典、"
                               f"无法确认——请到后台标签页核对后再决定是否重试")
        got = after.get(new_id)
        if got is None:
            return unavailable(f"改动请求已发出，但读回标签字典里找不到 id={new_id} 的行，"
                               f"本次改动未确认生效")
        mismatch = []
        if target == "two" and got.father_id != father:
            mismatch.append("父标签")
        if got.level != (2 if target == "two" else 1):
            mismatch.append("层级")
        if new_title and got.name != new_title:
            mismatch.append("名字")
        before_label = hit.label
        if mismatch:
            return unavailable(f"改动请求已发出，但读回标签「{got.label}」的"
                               f"{'、'.join(mismatch)}与预期不一致，本次改动未确认生效")
        return ok(A.render_tag_moved(before_label, got.label, A.move_impact(res)),
                  meta={"op": "tag_update", "tag_id": got.id, "tag_name": got.name,
                        "level": got.level, "before": before_label, "after": got.label})

    # 同层改名 / 改色：PUT /tagone|tagtwo/:id。**两个字段都是必填**，所以必须把
    # 当前颜色原样回传（不猜、也不许传空——传空等于把这个标签的颜色抹掉）。
    if not picked and not hit.color:
        return unavailable("读不到这个标签现在的颜色，而改名接口要求同时提交颜色；"
                           "为避免把它的颜色抹掉，本次未改动")
    payload = {"title": new_title or hit.name, "color": picked or hit.color}
    path = ("/api/protected/tagone/" if cur == "one" else "/api/protected/tagtwo/")
    data = _admin_request("PUT", f"{path}{hit.id}", payload, config)
    if isinstance(data, ToolResult):
        return data
    after = _tag_index(config)
    if after is None:
        return unavailable("改动请求已发出，但读不回标签字典、无法确认，请到后台标签页核对")
    got = after.get(hit.id)
    if got is None:
        return unavailable(f"改动请求已发出，但读回标签字典里找不到 id={hit.id} 的行，"
                           f"本次改动未确认生效")
    want_color = picked or hit.color
    if got.name != payload["title"] or got.color != want_color:
        return unavailable(f"改动请求已发出，但读回标签「{got.label}」的值与预期不一致"
                           f"（名字或颜色没落库），本次改动未确认生效")
    pairs = []
    if new_title:
        pairs.append((hit.name, got.name))
    if picked:
        pairs.append((A.describe_color(hit.color) if hit.color else "（未知）",
                      A.describe_color(got.color) if got.color else "（未知）"))
    before_s, after_s = A.render_change(pairs) if pairs else (hit.label, got.label)
    return ok(A.render_tag_updated(got.label, before_s, after_s),
              meta={"op": "tag_update", "tag_id": got.id, "tag_name": got.name,
                    "level": got.level, "before": before_s, "after": after_s})


@tool
def delete_tag(
    name: Annotated[str, "要删掉的那个标签的**名字**（站内已有的标签）"],
    config: RunnableConfig,
    level: Annotated[str | None,
                     "这个标签是几级（one/two）；站内同名标签不止一个时用它指认"] = None,
) -> str:
    """删除一个文章标签（一级或二级都可以）。需要管理员身份。

    ⚠️ **删除不可撤销**：删一级标签会连带删掉它下面的所有二级标签，并把这些标签
    从**所有文章**上摘掉（文章本身不会被删）。要动的标签名对不上就什么都不做。"""
    from agent import adminops as A
    # 子标签名单取自**解析目标的那一份快照**（同一次读）：删一级时会连坐子标签，
    # 名单要是从第二次读里取，两次读之间新建的子标签就会在回执里凭空消失
    # ——而它已经被 CASCADE 删掉了。
    index = _tag_index(config)
    hit, err = _find_named_tag(name, config, level, index=index)
    if err:
        return unavailable(err)
    hits_before = {hit.id}
    kids = A.children_of(index or {}, hit.id) if hit.level == 1 else []
    hits_before |= {k.id for k in kids}

    payload = {"level": A.level_of(hit), "ids": [hit.id]}
    data = _admin_request("DELETE", "/api/protected/tag", payload, config)
    if isinstance(data, ToolResult):
        return data

    after = _tag_index(config)
    if after is None:
        return unavailable(f"删除请求已发出，但读不回标签字典、无法确认它是否真的删掉了"
                           f"——请到后台标签页核对")
    left = [i for i in sorted(hits_before) if i in after]
    if left:
        return unavailable(f"删除请求已发出，但读回标签字典里 id={left[0]} 还在"
                           f"（共 {len(left)} 个没删掉），本次改动未确认生效")
    if hit.level == 1 and kids:
        extra = f"它下面的 {len(kids)} 个二级标签（{'、'.join(k.name for k in kids)}）已一并删除"
    elif hit.note_count is not None:
        extra = f"它原本挂在 {hit.note_count} 篇文章上，这些引用已一并摘掉（文章本身没删）"
    else:
        extra = ""
    return ok(A.render_tag_deleted(hit.label, extra),
              meta={"op": "tag_delete", "tag_id": hit.id, "tag_name": hit.name,
                    "level": hit.level, "change": extra or "已删除"})


def _category_index(config: RunnableConfig):
    """分类字典 → `{id: CategoryInfo}`；读不到返回 None（≠"没有分类"）。"""
    from agent import adminops as A
    data = _admin_get("/api/category", config)
    if isinstance(data, ToolResult):
        return None
    return A.build_category_index(data)


def _find_named_category(name, config, index=None):
    """按名字找一个分类 → `(CategoryInfo, None)` 或 `(None, 拒绝文本)`（同上）。

    `index` 与 `_find_named_tag` 同义：可选的**外部快照**。调用方一次要查多个名字
    （或只是"先看看能不能做"，见 graph._write_target_refusal）时传进来，省一次往返、
    也让两次判断看到同一份字典。完全没有同名分类时附名字最接近的候选（同 `_near_miss_names`）。"""
    from agent import adminops as A
    if index is None:
        index = _category_index(config)
    if index is None:
        return None, "读不到现有的分类列表，无法把名字对应到分类，本次未改动"
    want = str(name or "").strip()
    if not want:
        return None, "分类名为空，本次未改动"
    hit, cands = A.find_category(index, want)
    if hit is not None:
        return hit, None
    if cands:
        return None, (f"站内有 {len(cands)} 个叫「{want}」的分类（"
                      + "、".join(f"id={c.id}" for c in cands)
                      + "）：无法确定要动的是哪一个，本次未改动")
    near = _near_miss_names(want, [(c.id, c.name) for c in index.values()])
    if near:
        return None, (f"站内没有叫「{want}」的分类（完全同名的一个都没有）；"
                      + "名字最接近的是 "
                      + "、".join(f"{nm}（id={cid}）" for cid, nm in near)
                      + f"：本次未改动——若就是其中一个，请照它的**完整名字**再说一遍，"
                        f"我按那个名字动手")
    return None, f"站内没有叫「{want}」的分类，本次未改动"


@tool
def create_category(
    title: Annotated[str, "新分类的名字"],
    config: RunnableConfig,
    path_name: Annotated[str | None,
                         "分类的路径名（分类页 URL 里那一段，如 pythonfy）；用户没说就不填"] = None,
    introduce: Annotated[str | None, "分类简介；用户没说就不填"] = None,
    icon: Annotated[str | None, "分类图标；用户没说就不填"] = None,
    color: Annotated[str | None,
                     "用户点了名的颜色（中文色名，或任意 6 位色值如 #eb2f96）；没说就不填"] = None,
) -> str:
    """新建一个文章分类。分类是**平铺**的、没有层级。
    站内已有同名分类时**拒绝**（既不重复建、也不当成"已存在"复用——分类没有唯一
    约束，重名会让以后每一次按名字找分类都变成歧义）。需要管理员身份。"""
    from agent import adminops as A
    name = str(title or "").strip()
    if not name:
        return unavailable("分类名为空，未创建")
    if len(name) > 40:
        return unavailable(f"分类名过长（{len(name)} 字，上限 40），未创建")

    index = _category_index(config)
    if index is None:
        return unavailable("读不到现有的分类列表，无法确认是否重名，本次未创建")
    hit, cands = A.find_category(index, name)
    if hit is not None or cands:
        ids = "、".join(f"id={c.id}" for c in (cands or [hit]))
        return unavailable(f"站内已经有叫「{name}」的分类（{ids}），本次未创建")

    payload: dict = {"categoryTitle": name}
    color_spec = str(color or "").strip()
    if color_spec:
        picked = A.match_any_color(color_spec)
        if picked is None:
            return unavailable(f"颜色「{color_spec}」认不出来（可用中文色名，"
                               f"或 6 位色值如 #eb2f96），本次未创建")
        payload["color"] = picked
    for key, val in (("pathName", path_name), ("introduce", introduce), ("icon", icon)):
        s = str(val or "").strip()
        if s:
            payload[key] = s

    data = _admin_post("/api/protected/category", payload, config)
    if isinstance(data, ToolResult):
        return data

    # 建后复核：这个端点**不回 id**（返回死字符串 "Category created"），所以要靠
    # "分类列表里多出来一行、且那行的名字就是我建的那个"来证明它真的在。多出两行
    # （并发下别人也建了一个）就说不清哪行是我的——如实报不确定，不当成成功。
    after = _category_index(config)
    if after is None:
        return unavailable("分类可能已经建好，但读不回分类列表、无法确认"
                           "——请到后台分类页核对后再决定是否重试")
    new_ids = [i for i in after if i not in index]
    got = after.get(new_ids[0]) if len(new_ids) == 1 else None
    if got is None or got.name != name:
        return unavailable(f"新建请求已发出，但读回分类列表里找不到名字为「{name}」的新行，"
                           f"本次改动未确认生效")
    return ok(A.render_category_created(got),
              meta={"op": "category_create", "category_name": got.name})


@tool
def update_category(
    name: Annotated[str, "要改的那个分类的**名字**（站内已有的分类）"],
    config: RunnableConfig,
    new_title: Annotated[str | None, "改成什么名字；不改名就不填"] = None,
    path_name: Annotated[str | None, "改成什么路径名；不改就不填"] = None,
    introduce: Annotated[str | None, "改成什么简介；不改就不填"] = None,
    icon: Annotated[str | None, "换成什么图标；不改就不填"] = None,
    color: Annotated[str | None, "换成什么颜色；不改就不填"] = None,
) -> str:
    """修改一个已有分类（改名 / 路径名 / 简介 / 图标 / 颜色）。**只动你点名的字段**，
    没点名的保持不动。需要管理员身份。

    注：这些字段都**只能改不能清空**（清空请求会被后端当成"不改"忽略），
    所以本工具不接受"把简介清空"这类要求。"""
    from agent import adminops as A
    hit, err = _find_named_category(name, config)
    if err:
        return unavailable(err)

    payload: dict = {}
    new_title = str(new_title or "").strip()
    if new_title:
        if len(new_title) > 40:
            return unavailable(f"新分类名过长（{len(new_title)} 字，上限 40），本次未改动")
        payload["categoryTitle"] = new_title
    color_spec = str(color or "").strip()
    if color_spec:
        picked = A.match_any_color(color_spec)
        if picked is None:
            return unavailable(f"颜色「{color_spec}」认不出来（可用中文色名，"
                               f"或 6 位色值如 #eb2f96），本次未改动")
        payload["color"] = picked
    for key, val in (("pathName", path_name), ("introduce", introduce), ("icon", icon)):
        s = str(val or "").strip()
        if s:
            payload[key] = s
    if not payload:
        return unavailable("没有指出要改什么（名字 / 路径名 / 简介 / 图标 / 颜色），本次未改动")

    data = _admin_post(f"/api/protected/category/{hit.id}", payload, config)
    if isinstance(data, ToolResult):
        return data

    after = _category_index(config)
    if after is None:
        return unavailable("改动请求已发出，但读不回分类列表、无法确认，请到后台分类页核对")
    got = after.get(hit.id)
    if got is None:
        return unavailable(f"改动请求已发出，但读回分类列表里已经没有 id={hit.id} 这一行，"
                           f"本次改动未确认生效")
    # 逐字段复核"我要的它变成了"——**不是**相信那个 "Updated" 字符串
    # （update_category 对不存在的 id 也返回 Not found 之外的正常路径，
    #  且 0 行更新不会报错）。
    fields = {"categoryTitle": "name", "pathName": "path_name",
              "introduce": "introduce", "icon": "icon", "color": "color"}
    pairs = []
    for key, attr in fields.items():
        if key not in payload:
            continue
        want = str(payload[key])
        if str(getattr(got, attr)) != want:
            return unavailable(f"改动请求已发出，但读回分类「{got.name}」的这一项仍是旧值，"
                               f"本次改动未确认生效")
        pairs.append((str(getattr(hit, attr)) or "（空）", want))
    before_s, after_s = A.render_change(pairs)
    return ok(A.render_category_updated(got.name, before_s, after_s),
              meta={"op": "category_update", "category_name": got.name,
                    "change": after_s})


@tool
def delete_category(
    name: Annotated[str, "要删掉的那个分类的**名字**（站内已有的分类）"],
    config: RunnableConfig,
) -> str:
    """删除一个文章分类。需要管理员身份。

    ⚠️ **文章不会被删**：分类删掉后，原本属于它的文章会变成"没有分类"（后端外键是
    ON DELETE SET NULL）。分类名对不上就什么都不做。"""
    from agent import adminops as A
    hit, err = _find_named_category(name, config)
    if err:
        return unavailable(err)

    data = _admin_request("DELETE", "/api/protected/category", [hit.id], config)
    if isinstance(data, ToolResult):
        return data

    after = _category_index(config)
    if after is None:
        return unavailable("删除请求已发出，但读不回分类列表、无法确认它是否真的删掉了"
                           "——请到后台分类页核对")
    if hit.id in after:
        return unavailable(f"删除请求已发出，但读回分类列表里 id={hit.id}（{hit.name}）还在，"
                           f"本次改动未确认生效")
    change = (f"{hit.note_count} 篇文章变成没有分类"
              if hit.note_count is not None else "")
    return ok(A.render_category_deleted(hit.name, hit.note_count),
              meta={"op": "category_delete", "category_name": hit.name,
                    "change": change})


# ---------------------------------------------------------------------------
# 管理助手写工具：站内公告（20260922 第五轮：代发 / 改 / 删）
# ---------------------------------------------------------------------------
# 与标签/分类同一套纪律（名字通道 + fail-closed + 读回复核），三处**公告独有**的
# 事实决定了实现的形状：
#   ① 公告**没有唯一约束、没有"已发布/草稿"字段**（src/entity/announcement.rs 只有
#      id/title/content/时间戳），所以目标只能按**标题**认，且重名一律拒绝——按名字
#      挑一条出来改/删，选错就是改了别人的公告；
#   ② 新建端点**不回 id**（返回字符串 "Created"）⇒ 复核只能靠"读回清单、按标题+
#      正文认出新增的那条"，与 create_category 同族（那条也是按名字回头认）；
#   ③ 改端点要求 title 与 content **都发**（Rust 侧 `UpsertAnnouncement` 两个字段
#      都必填）⇒ 只改正文时必须把现有标题原样带上，不能发空标题把公告改成没名字。
# 删除是 `ON DELETE` 真删（公告没有外键），所以**读不回就等于没删掉**，不猜。
MAX_ANNOUNCE_TITLE = 80
MAX_ANNOUNCE_BODY = 2000


def _announcement_index(config: RunnableConfig) -> dict[int, dict] | None:
    """读公告清单 → `{id: 公告行}`；读不到返回 None（≠"没有公告"，同 _tag_index）。

    读的是**公开**端点 `/api/public/announcements`：公告本来人人都看得见，后台没
    第二份清单（`src/routes/announcements.rs` 只有这一个读接口）。仍然带发起人
    身份去打（`_admin_get` 统一签名），写工具的身份判据不受影响。
    """
    data = _admin_get("/api/public/announcements", config)
    if isinstance(data, ToolResult):
        return None
    out: dict[int, dict] = {}
    for row in data or []:
        if not isinstance(row, dict):
            continue
        try:
            aid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        row["id"] = aid                    # id 就地归一成 int：下游 `hit["id"]` 直接可用
        out[aid] = row
    return out


def _find_named_announcement(title, config, index=None):
    """按**标题**在公告清单里找一条 → `(行, None)` 或 `(None, 拒绝文本)`。

    与 `_find_named_tag` 同取向（也是同一个"名字通道"的公告版）：唯一命中才动手；
    命中多条（同名公告）**不替用户选一条**——把候选连同 id/时间列出来让他指认；
    一条完全同名的都没有就如实说没有，**并附上名字最接近的候选**（`_near_miss_names`，
    20260925：被截短的标题在这里会撞成一句"站内没有"的假话）；字典读不到则单独一种
    说法（"读不到" ≠ "没有"，把一次网络故障说成"站内没这条公告"是最坏的错法）。
    **模糊匹配只用来提问、不用来动手**：写操作仍然只认标题完全相等。
    `index` 是可选快照（一次操作要读同一份清单两回时用）。
    """
    if index is None:
        index = _announcement_index(config)
    if index is None:
        return None, "读不到现有的公告列表，无法把标题对应到公告，本次未改动"
    want = str(title or "").strip()
    if not want:
        return None, "公告标题为空，本次未改动"
    hits = [r for r in index.values() if str(r.get("title") or "").strip() == want]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        cands = "、".join(f"id={r.get('id')}（{str(r.get('createdAt') or '')[:16]}）" for r in hits)
        return None, (f"站内有 {len(hits)} 条标题都叫「{want}」的公告（{cands}）："
                      f"无法确定要动的是哪一条，本次未改动——请先说明是哪一条"
                      f"（或让我按别的说法列出全部公告）")
    near = _near_miss_names(want, [(r.get("id"), str(r.get("title") or ""))
                                   for r in index.values()])
    if near:
        # 近失（20260925 现场，见 _near_miss_names 长注）：主人点名的标题被节选
        # 截短 ⇒ 完全相等查不到。此时说"站内没有这条公告"是**假话**——站内有，
        # 只差三个字。把候选连同 id 摆出来，让"没有"变成一次可核对的追问。
        cands2 = "、".join(f"id={cid}「{nm}」" for cid, nm in near)
        return None, (f"站内没有标题**完全等于**「{want}」的公告；名字最接近的是 {cands2}。"
                      f"本次未改动——若就是其中一条，请照它的**完整标题**再说一遍"
                      f"（或直接给 id），我按那个标题动手")
    return None, f"站内没有标题是「{want}」的公告，本次未改动"


@tool
def create_announcement(
    title: Annotated[str, "公告标题（一句话，会显示在公告里）"],
    content: Annotated[str, "公告正文（用户说出来的内容，可以多句；不要自己加戏或改写）"],
    config: RunnableConfig,
) -> str:
    """发一条站内公告（全站访客都会在首页看到）。
    **标题与正文都必须是用户本轮说过的内容**：只写用户给了的，别替用户润色、补充
    或编造细节（公告是对全体访客说的话，改一个字都算改了主人的意思）。
    需要管理员身份，且要经主人确认才会真正发出。"""
    from agent import adminops as A
    t = str(title or "").strip()
    c = str(content or "").strip()
    if not t:
        return unavailable("公告标题为空，未发布")
    if not c:
        return unavailable("公告正文为空，未发布")
    if len(t) > MAX_ANNOUNCE_TITLE:
        return unavailable(f"公告标题过长（{len(t)} 字，上限 {MAX_ANNOUNCE_TITLE}），未发布")
    if len(c) > MAX_ANNOUNCE_BODY:
        return unavailable(f"公告正文过长（{len(c)} 字，上限 {MAX_ANNOUNCE_BODY}），未发布")

    before = _announcement_index(config)
    if before is None:
        return unavailable("读不到现有的公告列表，无法确认发布结果，本次未发布")
    data = _admin_request("POST", "/api/protected/announcements",
                          {"title": t, "content": c}, config)
    if isinstance(data, ToolResult):
        return data

    # 建后复核：端点不回 id，只能读回清单按"新旧 id 差集 + 标题对上"认人。
    # 认不出（读不回 / 差集为空 / 新行标题对不上）一律 unavailable——宁可让主人
    # 去后台看一眼，也不能把"可能没发出去"说成"已发布"。
    after = _announcement_index(config)
    if after is None:
        return unavailable("发布请求已发出，但读不回公告列表、无法确认是否真的发出去了"
                           "——请到后台公告页核对")
    fresh = [r for i, r in after.items() if i not in before
             and str(r.get("title") or "").strip() == t
             and str(r.get("content") or "").strip() == c]
    if len(fresh) != 1:
        return unavailable(f"发布请求已发出，但读回公告列表里没有找到标题为「{t}」的新公告"
                           f"（找到 {len(fresh)} 条），本次改动未确认生效——请到后台核对")
    row = fresh[0]
    return ok(A.render_announcement_created(row),
              meta={"op": "announcement_create", "announcement_id": row.get("id"),
                    "announcement_title": t})


@tool
def update_announcement(
    title: Annotated[str, "要改的那条公告的**标题**（用现在标题来指认它，不是新标题）"],
    config: RunnableConfig,
    new_title: Annotated[str | None, "改成什么标题；用户没说要改标题就不填"] = None,
    content: Annotated[str | None, "正文改成什么；用户没说要改正文就不填"] = None,
) -> str:
    """修改一条已经发出去的公告（改标题、改正文，或两者一起改）。
    **只改用户点名的那几项**，没点名的保持不变（改端点要求标题与正文都发，这里会
    把没点名的那个原样带上，不会把标题清空）。标题对不上就什么都不做。
    需要管理员身份，且要经主人确认才会真正改。"""
    from agent import adminops as A
    hit, err = _find_named_announcement(title, config)
    if err:
        return unavailable(err)

    nt = str(new_title or "").strip()
    nc = str(content or "").strip()
    if not nt and not nc:
        return unavailable("没有指出要改什么（标题还是正文），本次未改动")
    cur_t = str(hit.get("title") or "").strip()
    cur_c = str(hit.get("content") or "")
    final_t = nt or cur_t                  # 未点名的字段原样带上（见函数头注 ③）
    final_c = nc or cur_c.strip()
    if len(final_t) > MAX_ANNOUNCE_TITLE:
        return unavailable(f"新标题过长（{len(final_t)} 字，上限 {MAX_ANNOUNCE_TITLE}），未改动")
    if len(final_c) > MAX_ANNOUNCE_BODY:
        return unavailable(f"正文过长（{len(final_c)} 字，上限 {MAX_ANNOUNCE_BODY}），未改动")
    if final_t == cur_t and final_c == cur_c.strip():
        return ok(A.render_announcement_noop(hit),
                  meta={"op": "announcement_update", "announcement_id": hit.get("id"),
                        "announcement_title": final_t, "change": "与现在一致，无需改动"})

    aid = hit.get("id")
    data = _admin_request("PUT", f"/api/protected/announcements/{aid}",
                          {"title": final_t, "content": final_c}, config)
    if isinstance(data, ToolResult):
        return data

    after = _announcement_index(config)
    if after is None:
        return unavailable("修改请求已发出，但读不回公告列表、无法确认是否真的改上了"
                           "——请到后台公告页核对")
    got = after.get(int(aid))
    if got is None:
        return unavailable(f"修改请求已发出，但读回公告列表里找不到 id={aid} 这条公告，"
                           f"本次改动未确认生效")
    if (str(got.get("title") or "").strip() != final_t
            or str(got.get("content") or "").strip() != final_c):
        return unavailable(f"修改请求已发出，但读回 id={aid} 的公告与预期不一致，本次改动未确认生效")
    return ok(A.render_announcement_updated(hit, got),
              meta={"op": "announcement_update", "announcement_id": aid,
                    "announcement_title": final_t,
                    "change": _announce_change(cur_t, final_t, cur_c, final_c)})


def _announce_change(cur_t: str, final_t: str, cur_c: str, final_c: str) -> str:
    """变更摘要（进回执行的那句人话，跨轮执行记忆读它）。**不写正文内容**——
    公告正文可以很长，回执行只留列宽（300 字符），把正文塞进去会把摘要挤掉。

    改名的措辞是「改名（原「旧名」）」而不是「标题改为「新名」」：回执行的动作行
    已经用**新**名字做主语（`修改公告「新名」：…`），change 里再写一遍新名就成了
    「修改公告「维护改期」：标题改为「维护改期」」——同一句里同一个名字出现两次，
    跨轮读到也读不出改之前叫什么。
    """
    bits = []
    if final_t != cur_t:
        bits.append(f"改名（原「{cur_t}」）")
    if final_c != cur_c.strip():
        bits.append("正文已更新")
    return "、".join(bits)


@tool
def delete_announcement(
    title: Annotated[str, "要删掉的那条公告的**标题**（用现在标题指认它）"],
    config: RunnableConfig,
) -> str:
    """删除一条站内公告（删除后访客在首页看不到了，**删掉取不回来**）。
    标题对不上、或站内有好几条同名标题时什么都不做，并如实说明原因。
    需要管理员身份，且要经主人确认才会真正删。"""
    from agent import adminops as A
    hit, err = _find_named_announcement(title, config)
    if err:
        return unavailable(err)

    aid = int(hit.get("id"))
    data = _admin_request("DELETE", "/api/protected/announcements", [aid], config)
    if isinstance(data, ToolResult):
        return data

    after = _announcement_index(config)
    if after is None:
        return unavailable("删除请求已发出，但读不回公告列表、无法确认是否真的删掉了"
                           "——请到后台公告页核对")
    if aid in after:
        return unavailable(f"删除请求已发出，但读回公告列表里 id={aid}（{hit.get('title')}）还在，"
                           f"本次改动未确认生效")
    return ok(A.render_announcement_deleted(hit),
              meta={"op": "announcement_delete",
                    "announcement_id": aid,
                    "announcement_title": str(hit.get("title") or "").strip(),
                    "change": "已删除"})


# ---------------------------------------------------------------------------
# 管理助手写工具：河灯留言的人工复核（20260922 第六轮：审核 / 删除）
# ---------------------------------------------------------------------------
# 与上一节的公告三件同一套纪律（名字通道 + fail-closed + 读回复核），三处**留言
# 独有**的事实决定了实现的形状：
#   ① 留言**没有标题、没有名字**（`talk` 表只有 content/cat/author/时间）：用户嘴里
#      说的就是**那句话本身** ⇒ 目标通道 = **正文片段**（唯一子串匹配）。这是名字
#      通道的留言版：目标仍是"用户说过的东西"，解析仍是确定性的，解不出仍然零写。
#      命中的那条**由工具认、不由模型认**——模型只负责把用户描述的那句原话抄进来。
#   ② 审核端点的请求体是 `{approved: i8}`，且 **0 = 驳回**（Rust 侧写 approved=2
#      "未通过"，与"待审 0"区分）——方向传反的后果是"该驳回的给放行了"。所以
#      **数字由工具内部产生**，上层只说 pass/reject，绝不让模型碰 1/0。
#   ③ 删除端点对不存在的 id **静默无操作**（`delete_by_id` 不报错，照样返回
#      "Deleted"）⇒ 同公告取向：**读不回就等于没删掉**，不猜。
#
# 与"名字通道"的唯一差别在**歧义的概率**：标签名撞车很罕见，而正文片段
# （"好"/"谢谢"）一撞就是好几条。所以这里的取向更严——**命中多条一律不替主人挑**
# （把候选连同作者/时间/原文列出来让他指认），绝不按"最新的那条"猜。
BOARD_APPROVED_CN = {0: "待审", 1: "已通过", 2: "未通过"}


def _board_index(config: RunnableConfig) -> dict[int, dict] | None:
    """读后台留言清单 → `{talkKey: 行}`；读不到返回 None（≠"没有留言"，同 _tag_index）。

    读的是 `GET /api/protect/board`（后台管理视图，含 approved 与 ai_result 两列，
    也是 `get_moderation_status` 的同一份数据源）——**写操作的读写必须同一口径**，
    否则"读的是台账、写的是另一行"这类错会一直藏到线上。
    """
    data = _admin_get("/api/protect/board", config)
    if isinstance(data, ToolResult):
        return None
    out: dict[int, dict] = {}
    for row in data or []:
        if not isinstance(row, dict):
            continue
        try:
            tid = int(row.get("talkKey"))
        except (TypeError, ValueError):
            continue
        row["talkKey"] = tid               # 就地归一成 int：下游 hit["talkKey"] 直接可用
        out[tid] = row
    return out


def _board_label(row: dict) -> str:
    """一条留言的指称（问句/回执行共用）：`#id 作者（时间）`。

    **不带正文**：正文由调用方按需 clip 后拼上（问句要预览、回执行只留列宽），
    而"作者"是访客可控文本（留言时可填），故经 `sanitize_untrusted` 拆命令前缀。
    """
    from agent.reports import sanitize_untrusted
    from agent.adminops import clip as _clip
    who = sanitize_untrusted(row.get("author") or row.get("nickname") or "", 16)
    if not who:
        who = f"用户#{row.get('userId')}"
    at = str(row.get("createTime") or "")[:16]
    return f"#{row.get('talkKey')} {_clip(who, 16)}（{at}）"


def _board_excerpt(row: dict) -> str:
    """留言正文的一小段（问句预览用；访客可控 ⇒ 同样消毒）。"""
    from agent.reports import sanitize_untrusted
    from agent.adminops import clip as _clip
    return _clip(sanitize_untrusted(row.get("content") or "", 40), 40)


def _find_board_comment(quote, config: RunnableConfig, index=None):
    """按**正文片段**在留言清单里找一条 → `(行, None)` 或 `(None, 拒绝文本)`。

    与 `_find_named_tag` 同取向（名字通道的留言版）：唯一命中才动手；命中多条
    **不替用户选**（列出候选让他指认——「好」这种片段一撞就是好几条）；一条都不
    匹配就如实说没有匹配的留言，**绝不模糊匹配、绝不猜"最新的那条"**；清单读不到
    则单独一种说法（"读不到" ≠ "没有"）。

    匹配口径：先按**原文子串**（最严）；不中再按**去掉所有空白后**的子串——模型
    转写用户原话时常把换行/空格抹平（`reports._detail_line` 给 planner 看的明细行
    也是压成一行的），这一步只为救回这种转写差，不引入任何模糊匹配。
    `index` 是可选快照（一次操作要读同一份清单两回时用）。
    """
    if index is None:
        index = _board_index(config)
    if index is None:
        return None, "读不到后台的留言列表，无法把这段话对应到某条留言，本次未改动"
    want = str(quote or "").strip()
    if not want:
        return None, "没有给出能指认那一条留言的原话片段，本次未改动"
    hits = [r for r in index.values() if want in str(r.get("content") or "")]
    if not hits:
        squashed = re.sub(r"\s+", "", want)
        if squashed:
            hits = [r for r in index.values()
                    if squashed in re.sub(r"\s+", "", str(r.get("content") or ""))]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        cands = "；".join(f"{_board_label(r)}「{_board_excerpt(r)}」" for r in hits[:5])
        more = f"（还有 {len(hits) - 5} 条未列出）" if len(hits) > 5 else ""
        return None, (f"站内有 {len(hits)} 条留言都含「{want}」：{cands}{more}。"
                      f"无法确定是哪一条，本次未改动——请给一段更完整的原话"
                      f"（或说明作者/大致时间），我再动手")
    return None, (f"站内没有含「{want}」的河灯留言，本次未改动"
                  f"（可能记错了字，或那条已经被删了）")


def _board_state_cn(row: dict) -> str:
    return BOARD_APPROVED_CN.get(row.get("approved"), "状态未知")


@tool
def audit_board_comment(
    quote: Annotated[str, "用来指认是哪一条留言的**原话片段**（从那条留言正文里原样抄一段，"
                          "用户说的就是这句；别改写、别概括）"],
    verdict: Annotated[str, "复核结论：pass=通过（放行展示）/ reject=驳回（隐藏）"],
    config: RunnableConfig,
) -> str:
    """人工复核一条河灯留言：**通过**（放行给所有人看）或**驳回**（隐藏起来）。
    留言按正文片段指认：片段对不上、或站内有好几条都含这段时什么都不做，并如实
    说明原因与候选。这一动作**可以改判**（驳回的能再放行），不是删除。
    需要管理员身份，且要经主人确认才会真正生效。"""
    from agent import adminops as A
    v = A.normalize_verdict(verdict)
    if v is None:
        return unavailable(f"认不出复核结论「{verdict}」（只能是 通过/pass 或 驳回/reject），未改动")
    hit, err = _find_board_comment(quote, config)
    if err:
        return unavailable(err)

    tid = int(hit.get("talkKey"))
    want = A.BOARD_VERDICT_APPROVED[v]           # 1=通过 / 2=未通过（见节头注 ②）
    if hit.get("approved") == want:
        # 现状即目标：**也走 ok**（这不是失败，"现在就是这样"是事实本身）。
        return ok(A.render_board_audit_noop(hit, v),
                  meta={"op": "board_audit", "board_id": tid,
                        "board_author": str(hit.get("author") or ""),
                        "change": f"与现在一致（{_board_state_cn(hit)}），无需改动"})

    # 请求体按 Rust 侧口径发：1=通过 / 0=驳回（端点内部把 0 写成 approved=2）。
    data = _admin_request("PUT", f"/api/protect/board/{tid}/audit",
                          {"approved": A.BOARD_VERDICT_BODY[v]}, config)
    if isinstance(data, ToolResult):
        return data

    after = _board_index(config)
    if after is None:
        return unavailable("复核请求已发出，但读不回留言列表、无法确认是否真的改上了"
                           "——请到后台留言管理页核对")
    got = after.get(tid)
    if got is None:
        return unavailable(f"复核请求已发出，但读回留言列表里找不到 #{tid} 这条留言，"
                           f"本次改动未确认生效")
    if got.get("approved") != want:
        return unavailable(f"复核请求已发出，但读回 #{tid} 的状态是"
                           f"「{_board_state_cn(got)}」、与预期的「{A.BOARD_VERDICT_CN[v]}」"
                           f"不一致，本次改动未确认生效")
    return ok(A.render_board_audited(hit, v),
              meta={"op": "board_audit", "board_id": tid,
                    "board_author": str(hit.get("author") or ""),
                    "change": f"{_board_state_cn(hit)} → {A.BOARD_VERDICT_CN[v]}"})


@tool
def delete_board_comment(
    quote: Annotated[str, "用来指认是哪一条留言的**原话片段**（从那条留言正文里原样抄一段）"],
    config: RunnableConfig,
) -> str:
    """删除一条河灯留言（**删掉取不回来**，也没有回收站）。
    留言按正文片段指认：片段对不上、或站内有好几条都含这段时什么都不做，并如实
    说明原因与候选。**只是要隐藏一条留言时改用审核（驳回），不要删。**
    需要管理员身份，且要经主人确认才会真正删。"""
    from agent import adminops as A
    hit, err = _find_board_comment(quote, config)
    if err:
        return unavailable(err)

    tid = int(hit.get("talkKey"))
    data = _admin_request("DELETE", f"/api/protect/board/{tid}", None, config)
    if isinstance(data, ToolResult):
        return data

    after = _board_index(config)
    if after is None:
        return unavailable("删除请求已发出，但读不回留言列表、无法确认是否真的删掉了"
                           "——请到后台留言管理页核对")
    if tid in after:
        return unavailable(f"删除请求已发出，但读回留言列表里 {_board_label(hit)} 还在，"
                           f"本次改动未确认生效")
    return ok(A.render_board_deleted(hit),
              meta={"op": "board_delete", "board_id": tid,
                    "board_author": str(hit.get("author") or ""),
                    "change": "已删除"})


@tool
def set_article_status(
    article_id: Annotated[int, "文章 id：用户本轮点名了（如「文章 12」）就**直接用点名的那个**，"
                               "不必先读；没说 id 只说特征（「那篇讲 OTA 的」）时必须先用 "
                               "list_admin_notes 读回确切 id，不许凭记忆猜"],
    config: RunnableConfig,
    status: Annotated[str | None, "改成什么状态：public=公开 / private=私密 / draft=草稿"] = None,
    is_top: Annotated[int | None, "置顶开关：1=置顶 / 0=取消置顶"] = None,
) -> str:
    """修改一篇文章的状态（发布/隐藏/转草稿）或置顶开关。**只改你点名的字段**，
    没点名的保持不动（不会顺手改标题、正文或可见性）。需要管理员身份。"""
    from agent import adminops as A
    aid = _as_article_id(article_id)
    if aid is None:
        return unavailable(f"文章 id「{article_id}」不合法，未改动")
    has_status = status is not None and str(status).strip() != ""
    has_top = is_top is not None and str(is_top).strip() != ""
    if not has_status and not has_top:
        return unavailable("没有指出要改什么（状态 / 置顶），未改动")
    want_status = A.normalize_status(status) if has_status else None
    if has_status and want_status is None:
        return unavailable(f"认不出状态「{status}」（只支持 public 公开 / private 私密 / draft 草稿），未改动")
    want_top = A.normalize_top(is_top) if has_top else None
    if has_top and want_top is None:
        return unavailable(f"认不出置顶值「{is_top}」（只支持 1 置顶 / 0 取消置顶），未改动")

    before = _read_note(aid, config)
    if isinstance(before, ToolResult):
        return before
    if before is None:
        return unavailable(f"后台文章列表里没有 id={aid} 这一篇"
                           f"（它可能是某篇文章的「编辑修改稿」影子行，不能这样直接改），本次未改动")
    title = str(before.get("noteTitle") or "").strip()

    payload: dict = {}
    if want_status is not None:
        payload["status"] = want_status
    if want_top is not None:
        payload["isTop"] = want_top
    # ★ 刻意**不发** title/content（会触发 from_editor 分支：重定向 + 级联删修改稿）、
    #   不发 isPublic（由 status 联动）、不发 updateTime（Rust 自己写 updated_at）。

    # 已经就是目标值 → **不发这个请求**。`update_note` 无条件刷新 updated_at，
    # 一次空改动会把文章顶到列表最前（纯副作用、无收益）。
    same = ((want_status is None or A.normalize_status(before.get("status")) == want_status)
            and (want_top is None or A.normalize_top(before.get("isTop")) == want_top))
    if same:
        now_cn = "、".join(filter(None, [
            A.status_cn(before.get("status")) if want_status is not None else "",
            A.top_cn(before.get("isTop")) if want_top is not None else ""]))
        return ok(f"文章 {aid}《{title}》本来就是{now_cn}，无需改动（没有发出写请求）。",
                  meta={"op": "set_status", "article_id": aid, "before": now_cn, "after": now_cn,
                        "noop": True})

    data = _admin_post(f"/api/protected/notes/{aid}", payload, config)
    if isinstance(data, ToolResult):
        return data

    after = _read_note(aid, config)
    if isinstance(after, ToolResult):
        return after
    if after is None:
        return unavailable(f"改动请求已发出，但读回时文章 {aid} 已不在后台列表里，本次改动未确认生效")

    pairs = []
    if want_status is not None:
        pairs.append((A.status_cn(before.get("status")), A.status_cn(after.get("status"))))
    if want_top is not None:
        pairs.append((A.top_cn(before.get("isTop")), A.top_cn(after.get("isTop"))))
    if all(b == a for b, a in pairs):
        # 请求发出去了、读回来却原封不动 —— 这不是"改好了"，是"没确认生效"。
        return unavailable(f"改动请求已发出，但读回文章 {aid} 仍是原值"
                           f"（{(' / '.join(b for b, _ in pairs))}），本次改动未确认生效")
    before_s, after_s = A.render_change(pairs)
    return ok(A.render_status_ok(aid, title, before_s, after_s),
              meta={"op": "set_status", "article_id": aid, "before": before_s, "after": after_s})


@tool
def set_article_tags(
    article_id: Annotated[int, "文章 id：用户本轮点名了（如「文章 12」）就**直接用点名的那个**，"
                               "不必先读；没说 id 只说特征（「那篇讲 OTA 的」）时必须先用 "
                               "list_admin_notes 读回确切 id，不许凭记忆猜"],
    config: RunnableConfig,
    add: Annotated[list[str] | None, "要**加上**的标签名（如 [\"Python\"]；必须是站内已存在的标签）"] = None,
    remove: Annotated[list[str] | None, "要**去掉**的标签名"] = None,
    replace: Annotated[list[str] | None, "整体**替换**成这些标签名；**只有明确要求「清空标签」时才传 []**"] = None,
) -> str:
    """给一篇文章加标签 / 去掉标签。**只动你点名的标签，没点名的原样保留**（绝不顺手清空）。
    标签按**名字**精确匹配站内已有的标签——找不到就如实说，**不会自动新建**
    （要新建标签用 create_tag）。传 replace 才是整体替换。需要管理员身份。"""
    from agent import adminops as A
    aid = _as_article_id(article_id)
    if aid is None:
        return unavailable(f"文章 id「{article_id}」不合法，未改动")

    def _names(v) -> list:
        if v is None:
            return []
        if isinstance(v, list):
            return [x for x in v if str(x).strip() != ""]
        s = str(v).strip()
        return [s] if s else []

    add_l, rm_l, rep_l = _names(add), _names(remove), _names(replace)
    if replace is not None and (add_l or rm_l):
        return unavailable("replace 不能与 add/remove 同时使用（一个说\"整体替换\"、一个说\"增减\"），未改动")
    if not add_l and not rm_l and replace is None:
        return unavailable("没有指出要加/去/替换哪些标签，未改动")

    index = _tag_index(config)
    if index is None:
        return unavailable("读不到现有的标签字典，无法把标签名对应到 id，本次未改动")

    def _resolve(items) -> tuple[list[int], list[str]]:
        """名字/数字混合列表 → (id 列表, 认不出来的原样列表)。"""
        ids, bad = [], []
        for it in items:
            tid = _as_article_id(it)
            if tid is not None:
                ids.append(tid)
                continue
            hit, cands = A.find_tag(index, str(it))
            if hit is not None:
                ids.append(hit.id)
            else:
                bad.append(str(it))
        return ids, bad

    before_note = _read_note(aid, config)
    if isinstance(before_note, ToolResult):
        return before_note
    if before_note is None:
        return unavailable(f"后台文章列表里没有 id={aid} 这一篇"
                           f"（它可能是某篇文章的「编辑修改稿」影子行，不能这样直接改），本次未改动")
    title = str(before_note.get("noteTitle") or "").strip()
    cur = A.parse_tag_ids(before_note.get("noteTags"))

    if replace is not None:
        add_ids, bad = _resolve(rep_l)
        if bad:
            # 返回文本里**不许出现工具名**（20260921）：narrator 被要求"按工具返回作答"，
            # 照抄到回复里就会变成"我调用了 X"（而 X 若不在本轮执行集里，5c 具名声称闸
            # 直接判编造 → 整条回复被换成兜底道歉）。同族事故见 adminops.render_tag_created。
            return unavailable(f"站内没有这些标签：{'、'.join(bad)}——"
                               f"先确认名字（或先把标签建出来），本次未改动")
        new = add_ids
    else:
        add_ids, bad_add = _resolve(add_l)
        rm_ids, bad_rm = _resolve(rm_l)
        if bad_add:
            return unavailable(f"站内没有这些标签：{'、'.join(bad_add)}——"
                               f"标签按名字精确匹配，不会自动新建，本次未改动")
        if bad_rm:
            return unavailable(f"站内没有这些标签：{'、'.join(bad_rm)}——无法确定要去掉的是哪一个，本次未改动")
        new = [i for i in cur if i not in rm_ids]
        for i in add_ids:
            if i not in new:
                new.append(i)

    if new == cur:
        return ok(f"文章 {aid}《{title}》的标签本来就是"
                  f"{A.render_tag_list(cur, index)}，无需改动（没有发出写请求）。",
                  meta={"op": "set_tags", "article_id": aid,
                        "before": A.render_tag_list(cur, index),
                        "after": A.render_tag_list(cur, index), "noop": True})

    # `noteTags` 是"传了就写"，`""` = 清空 ⇒ 只有 replace（含 replace=[]）才可能产出空串；
    # add/remove 路径下 new 至少含一个元素或被上面的 new==cur 拦下。
    data = _admin_post(f"/api/protected/notes/{aid}", {"noteTags": A.join_tag_ids(new)}, config)
    if isinstance(data, ToolResult):
        return data

    after_note = _read_note(aid, config)
    if isinstance(after_note, ToolResult):
        return after_note
    if after_note is None:
        return unavailable(f"改动请求已发出，但读回时文章 {aid} 已不在后台列表里，本次改动未确认生效")
    got = A.parse_tag_ids(after_note.get("noteTags"))
    if got != new:
        return unavailable(f"改动请求已发出，但读回文章 {aid} 的标签是"
                           f"{A.render_tag_list(got, index)}、与预期的"
                           f"{A.render_tag_list(new, index)}不一致，本次改动未确认生效")
    before_s = A.render_tag_list(cur, index)
    after_s = A.render_tag_list(got, index)
    return ok(A.render_tags_ok(aid, title, before_s, after_s),
              meta={"op": "set_tags", "article_id": aid, "before": before_s, "after": after_s})


# ---------------------------------------------------------------------------
# 用户自己的数据工具（20260923：收藏 / 未读汇总 / 站内通知）
# ---------------------------------------------------------------------------
# 这一节是"用户侧感知能力"的**读**那一半（写那一半是同一批里的 add_favorite /
# remove_favorite / read_notifications，scope 为 `write.own`）：
# 访客问"我收藏了哪些文章""有没有未读的公告"，planner 点名这三个工具，execute
# 以**本轮发起人身份**读他自己的数据后如实作答。
#
# 三条纪律：
#   ① scope 全声明为 `read.own`（agent/authz.py）——三档角色都有，匿名没有。
#      **不是** admin.console：收藏/通知是"我的"，与管理员身份无关，判据是
#      "以谁的 uid 去读"，由工具层落地。
#   ② 通道是 `_own_get`（与 `_admin_get` 同一个 `_principal_get` 本体，只有措辞
#      不同）：fail-closed 同族，"读不到"绝不返回空——否则 narrator 会对着一次
#      读失败说"你还没有收藏任何文章"。
#   ③ 返回值走 `_shape(data)`（结构化），**不做中文报表**——它们是"用户要的值"
#      （标题、日期、已读态），narrator 手上还有完整 ToolMessage 可转述；报表那套
#      渲染（agent/reports.py）是给"数字必须可信、模型数不了"的管理报表用的。
#      planner 那一侧由 `_compact_list_frame` 压成一行一条（见 agent/context.py）。
#
# 端点（Rust 侧，src/routes/profile.rs）：`/favorites`、`/notifications`、
# `/notifications/summary` 都只认 auth_uid，**自己读自己**——没有"读别人的"接口。

@tool
def list_my_favorites(config: RunnableConfig) -> str:
    """列出**当前登录用户自己**收藏的文章（返回 noteId / title / status / createdAt）。
    访客问"我收藏了哪些文章""我的收藏夹里有什么"时用。未登录时如实告知读不到。
    注意：这条读的是**用户自己的**收藏夹，不是全站文章列表（那是 list_notes）。"""
    data = _own_get("/api/protected/favorites", config)
    return _shape(data)


# 未读汇总连带带回的条目（20260924）：红点问的是"几条"，紧接着的一句必定是
# "是什么"——只有计数时那一句要么再调一次 list_notifications（多一轮）、要么
# 拿计数去编内容。上限 10 条：条目是给"红点里是什么"看的，不是列表副本
# （要全量走 list_notifications）；超限时在 `unread_items_note` 里如实说明。
# 20260924 二改：`content` **进**条目。首版刻意不搬它（当时的口径是"条目只给指代用"），
# 实测错了——留言审核的**驳回理由就写在 content 里**，不搬等于逼着多问一轮：
# trace 20260924T025619 只拿到 title，访客紧接着问"那两条留言的审核意见是什么"，
# 正是这一句逼出后面两轮（其中一轮还把通知里的留言 id 当文章 id 去查 get_article_detail）。
# 截断到 120 字：条目是摘要，要原文走 list_notifications。
_UNREAD_ITEMS_MAX = 10
_UNREAD_ITEM_KEYS = ("id", "type", "title", "content", "link", "createdAt")
_UNREAD_CONTENT_MAX = 120
# 条目那半读不到时的措辞（计数是真的、条目没有）：**不许**退化成"没有未读条目"
_UNREAD_ITEMS_DOWN = "未读条目的内容这次没读到（只有计数是可信的）"


def _unread_item_slim(row: dict) -> dict:
    """一条未读通知 → 给 planner/narrator 看的条目（`content` 截断，别把整条通知搬进帧）。"""
    out = {k: row[k] for k in _UNREAD_ITEM_KEYS if k in row}
    text = out.get("content")
    if isinstance(text, str) and len(text) > _UNREAD_CONTENT_MAX:
        out["content"] = text[:_UNREAD_CONTENT_MAX] + "…"
    return out


@tool
def get_unread_summary(config: RunnableConfig) -> str:
    """查看**当前登录用户自己**的未读汇总：计数（notifications / messages / total）
    **连带未读的那几条通知**（`unread_items`：id / type / title / content（正文，截断
    120 字）/ link / createdAt，最多 10 条，超出时另有 `unread_items_note` 说明）。
    红点数 = 站内通知未读 + 私信未读；**公告在发布时按用户展开成通知行**，所以
    公告的未读也计在 notifications 里，不需要另一套计数。
    访客问"我有未读吗""红点上有几条""有多少没看的消息""红点里是什么/是什么通知"时用
    ——问"是什么"**不必**再调 list_notifications（未读条目连同正文就在这次返回里）。
    **通知正文里往往就是答案本体**（如留言审核的驳回理由写在 content 里）：访客追问
    "那是什么内容/理由是什么"时先看这里，别绕去查别的接口，更不许说"读不到内容"。
    未登录时如实告知读不到。"""
    data = _own_get("/api/protected/notifications/summary", config)
    if isinstance(data, ToolResult):
        # 计数都没读到：原样透出（fail-closed），**不做任何"连带"**——
        # 计数读不到时再补一次列表调用，只会让"未登录"变成一次多余的请求
        return data
    if not isinstance(data, dict):
        return _shape(data)
    notices = _own_get("/api/protected/notifications", config)
    rows: list = []
    if isinstance(notices, dict):
        if isinstance(notices.get("items"), list):
            rows = notices["items"]
    elif isinstance(notices, list):
        rows = notices
    if isinstance(notices, ToolResult) or not rows:
        # 两种情况分开：条目读失败（notices 是 unavailable）与真的零条目。
        # 读失败绝不能写成"你没有未读的通知"——本模块头注第 2 条纪律。
        note = (_UNREAD_ITEMS_DOWN if isinstance(notices, ToolResult)
                else "")
        out = dict(data, unread_items=[])
        if note:
            out["unread_items_note"] = note
        return _shape(out)
    unread = [r for r in rows
              if isinstance(r, dict) and not r.get("isRead")]
    slim = [_unread_item_slim(r) for r in unread[:_UNREAD_ITEMS_MAX]]
    out = dict(data, unread_items=slim)
    # 抬头那个计数（notifications）才是权威数量：拿它跟带回来的条数比，**两种缺口
    # 用同一句话如实说**——① 超过本条上限；② 未读里较早的落在列表接口的最近 100 条
    # 窗口之外（那时"未读里还有几条"照样成立）。不许默默少给。
    total_unread = data.get("notifications")
    if (isinstance(total_unread, int) and not isinstance(total_unread, bool)
            and total_unread > len(slim)):
        out["unread_items_note"] = (f"只带回 {len(slim)} 条"
                                    f"（未读通知共 {total_unread} 条）")
    return _shape(out)


@tool
def list_notifications(config: RunnableConfig) -> str:
    """列出**当前登录用户自己**的站内通知（返回 unread 与 items：id / type / title /
    content / link / isRead / createdAt，按时间倒序，最多 100 条）。
    type=announcement 的是站内公告（发布时按用户展开），其余是系统通知（如留言审核
    结果）。访客问"有什么未读的公告/通知""最新的一条通知说了什么"时用。
    未登录时如实告知读不到；**这条不含私信**（私信未读只在 get_unread_summary 的
    messages 里计数，本站没有"读自己的私信列表"的 agent 工具）。"""
    data = _own_get("/api/protected/notifications", config)
    return _shape(data)


# ── 写那一半（20260923 批 7）：收藏 / 取消收藏 / 标记已读 ────────────────────
# scope = `write.own`（三档角色都有、匿名没有）。三条纪律，第三条是写操作独有的：
#   ① 同意闸判"本轮有没有一条**明确命令**"，判据按**工具名**分开（agent/authz.py
#      的 `_own_command`：一个 scope 挂多个工具，只看"有没有写动作"会让填错工具
#      的那一轮照样放行）。判不出来走确认弹窗，既不硬拒也不静默写。
#   ② 通道是 `_own_request`（与后台写共用 `_principal_request` 本体）：uid ≤ 0 时
#      **一个字节都不发**——写操作最不该做的就是在没身份时猜"写给谁"。
#   ③ **写前先读、写后再读**：写前读不到就不写（连"是不是已经收藏了"都判不出来时
#      写下去等于蒙）；写后读不回目标状态就报"未确认生效"，**绝不拿接口的成功文案
#      当事实**——Rust 的 `ApiResponse` 成功文案是给人看的，不是给 agent 当判据的。
#
# 幂等（写前读的副产品）：已经收藏 / 本来就没收藏 / 这些通知本来就是已读
# → **不发请求**、如实说"本来就是这样"。收藏端点自己也是幂等的，但一次空写会刷新
# 那一行的时间戳（纯副作用、无收益），与 set_article_status 的同值 noop 同一条理由。


def _favorites_snapshot(config: RunnableConfig,
                        what: str) -> tuple[list | None, ToolResult | None]:
    """本人收藏列表 → `(行列表, 失败原因)`，两者**恰有一个**为 None。

    **弹窗的「状态已达成」判据与 add/remove_favorite 的写前读走这一个函数**（照
    `_tag_index` / `_note_index` / `_todo_rows` 那条先例）：两处各写一套形态判断时，
    "弹窗说本来就在收藏夹里、工具却照写一遍"这种不一致没有任何东西拦得住——而它正是
    20260926 那一批要治的病。

    `what` = 失败话里的那半句（收藏与取消收藏问的不是同一件事："是不是已经收藏过"
    /"是不是本来就没收藏"），由调用点带进来，措辞不在这里分叉。
    """
    got = _own_get("/api/protected/favorites", config)
    if isinstance(got, ToolResult):
        return None, _pre_read_fail(got, what)
    if not isinstance(got, list):
        return None, unavailable(f"读不到{what}，本次未改动")
    return got, None


def _fav_row(data, note_id: int) -> dict | None:
    """收藏列表 → 目标那一行（不是列表/没这一行 → None，不猜）。"""
    if not isinstance(data, list):
        return None
    for r in data:
        if isinstance(r, dict) and _as_article_id(r.get("noteId")) == note_id:
            return r
    return None


def _fav_count(data) -> str:
    """收藏条数的括注；读不出条数就**什么都不写**（不写 0）。"""
    n = len(data) if isinstance(data, list) else None
    return f"（你的收藏夹现在有 {n} 篇）" if n is not None else ""


def _fav_title(row: dict) -> str:
    t = str(row.get("title") or "").strip()
    return f"《{t}》" if t else ""


def _note_items(data) -> dict[int, dict] | None:
    """通知列表（`{unread, items}`）→ `{id: 行}`；形态不对 → None（不猜）。"""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    out: dict[int, dict] = {}
    for r in data["items"]:
        if not isinstance(r, dict):
            continue
        nid = _as_article_id(r.get("id"))
        if nid is not None:
            out[nid] = r
    return out


def _notifications_snapshot(config: RunnableConfig,
                            what: str) -> tuple[dict[int, dict] | None, ToolResult | None]:
    """通知列表 → `({id: 行}, 失败原因)`，两者**恰有一个**为 None。

    与 `_favorites_snapshot` 同一条纪律（弹窗判据与 read_notifications 的写前读
    必须是**同一份**读、同一份形态判断）。`what` 同上：由调用点给出失败话的那半句。
    """
    got = _own_get("/api/protected/notifications", config)
    if isinstance(got, ToolResult):
        return None, _pre_read_fail(got, what)
    rows = _note_items(got)
    if rows is None:
        return None, unavailable(f"读不到{what}，本次未改动")
    return rows, None


def _as_ids(value) -> list[int]:
    """实参 → 正整数 id 列表（去重、保序；认不出的项丢掉——不猜）。"""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[int] = []
    for it in items:
        i = _as_article_id(it)
        if i is not None and i not in out:
            out.append(i)
    return out


def _as_bool(value) -> bool | None:
    """实参 → 布尔；认不出 → None（调用方按"没说"处理，**绝不默认 True**）。"""
    if isinstance(value, bool):
        return value
    s = str(value if value is not None else "").strip().lower()
    if s in ("true", "1", "yes", "y", "on", "all", "全部", "全都", "所有"):
        return True
    if s in ("false", "0", "no", "n", "off", ""):
        return False
    return None


@tool
def add_favorite(
    article_id: Annotated[int, "文章 id：用户本轮点名了（如「收藏文章 12」）就直接用点名的那个；"
                               "只说特征（「收藏那篇讲架构的」）或指代（「收藏这篇」）时"
                               "必须先用检索/列表工具或 get_article_detail 拿到确切 id，"
                               "不许凭记忆猜"],
    config: RunnableConfig,
) -> str:
    """把一篇文章收藏进**当前登录用户自己**的收藏夹（幂等：已收藏过就如实说本来就有，
    不会重复收藏）。收藏只有他自己看得见，**不改变文章的公开状态**。
    未登录时如实告知，一个请求都不发。"""
    aid = _as_article_id(article_id)
    if aid is None:
        return unavailable(f"文章 id「{article_id}」不合法，未改动")
    guard = _own_write_guard(config)
    if guard is not None:
        return guard

    before, fail = _favorites_snapshot(config, "你的收藏列表（拿不准是不是已经收藏过）")
    if fail is not None:
        return fail
    hit = _fav_row(before, aid)
    if hit is not None:
        return ok(f"文章 {aid}{_fav_title(hit)}本来就在你的收藏夹里，无需改动（没有发出写请求）。",
                  meta={"op": "favorite_add", "article_id": aid,
                        "change": "本来已收藏", "noop": True})

    data = _own_post("/api/protected/favorites", {"noteId": aid}, config)
    if isinstance(data, ToolResult):
        return data

    after = _own_get("/api/protected/favorites", config)
    if isinstance(after, ToolResult):
        # ⚠️ 写**已经发出去了**，此后读不回来不能再把原样的话交出去：读侧的
        # unavailable 措辞只说"读不到"（那是写前读该说的话），而下游 narrator 需要
        # 知道"写请求已发出、但没确认生效"。两种情形混用会让它把一次读失败讲成
        # 收藏失败，或者更糟——照接口回的话说"已收藏"。
        return unavailable(f"收藏请求已发出，但读不回你的收藏列表（{after}），"
                           f"本次改动未确认生效（不要声称已收藏）")
    got = _fav_row(after, aid)
    if got is None:
        return unavailable(f"收藏请求已发出，但读回收藏列表里没有文章 {aid}，"
                           f"本次改动未确认生效（不要声称已收藏）")
    return ok(f"已收藏文章 {aid}{_fav_title(got)}{_fav_count(after)}。",
              meta={"op": "favorite_add", "article_id": aid, "change": "已收藏"})


@tool
def remove_favorite(
    article_id: Annotated[int, "文章 id：同 add_favorite（点名了就直接用，"
                               "只说特征/指代时先用检索或收藏列表拿到确切 id）"],
    config: RunnableConfig,
) -> str:
    """把一篇文章从**当前登录用户自己**的收藏夹里去掉（本来就没收藏过就如实说，
    不当成出错——同一个按钮点两次不该报错）。只动他自己的收藏夹，不改文章本身。
    未登录时如实告知，一个请求都不发。"""
    aid = _as_article_id(article_id)
    if aid is None:
        return unavailable(f"文章 id「{article_id}」不合法，未改动")
    guard = _own_write_guard(config)
    if guard is not None:
        return guard

    before, fail = _favorites_snapshot(config, "你的收藏列表（拿不准是不是本来就没收藏）")
    if fail is not None:
        return fail
    hit = _fav_row(before, aid)
    if hit is None:
        return ok(f"文章 {aid} 本来就不在你的收藏夹里，无需改动（没有发出写请求）。",
                  meta={"op": "favorite_remove", "article_id": aid,
                        "change": "本来就没收藏", "noop": True})

    data = _own_delete(f"/api/protected/favorites/{aid}", config)
    if isinstance(data, ToolResult):
        return data

    after = _own_get("/api/protected/favorites", config)
    if isinstance(after, ToolResult):
        return unavailable(f"取消收藏请求已发出，但读不回你的收藏列表（{after}），"
                           f"本次改动未确认生效（不要声称已取消）")
    if _fav_row(after, aid) is not None:
        return unavailable(f"取消收藏请求已发出，但读回收藏列表里文章 {aid} 还在，"
                           f"本次改动未确认生效（不要声称已取消）")
    return ok(f"已取消收藏文章 {aid}{_fav_title(hit)}{_fav_count(after)}。",
              meta={"op": "favorite_remove", "article_id": aid, "change": "已取消收藏"})


@tool
def read_notifications(
    config: RunnableConfig,
    ids: Annotated[list[int] | None, "要标记已读的通知 id 列表（用户点名了具体哪几条时给；"
                                     "id 只能来自 list_notifications 的返回或执行记忆摘要里 "
                                     "`id《标题》` 形态的编号，**不许把「共 N 条」里的 N 当 id**，"
                                     "也不许自己编——编号必须是真的才能在列表里对上"] = None,
    all: Annotated[bool | None, "true = 把**全部**未读通知标记已读（用户说了「全部/都/所有」"
                                "才给；只是「把通知标记为已读」这种没限定范围的**不要自己填 True**）"] = None,
) -> str:
    """把**当前登录用户自己**的站内通知标记为已读（按 id 或全部）。**已读不可撤销**：
    标记之后那几条就不再是未读（头顶红点问的是"未读总数 > 0"，而那个总数是**通知与
    站内信之和**：只标一部分时它不变，把通知全标完也还要信那边也没有未读才会消失），
    所以只有用户明确说了要标记才用。
    既要不了 id 也没说"全部"时**什么都不动**，如实问清是哪几条。
    未登录时如实告知，一个请求都不发。

    ⚠️ 形参名 `all` 是对外（planner / 技能参数）的 JSON 键，**本函数体内不得再调用
    内置 `all()`** —— 它已被同名形参遮蔽（20260923 实测：`all(...)` 直接
    `TypeError: 'NoneType' object is not callable`）。要判"全都没读"用显式循环。
    """
    want_all = _as_bool(all) is True
    want_ids = _as_ids(ids)
    guard = _own_write_guard(config)
    if guard is not None:
        return guard
    if not want_all and not want_ids:
        return unavailable("没有指出要标记哪些通知（是全部未读、还是某几条的 id），未改动")

    rows, fail = _notifications_snapshot(config, "通知列表（无法确认哪几条是未读）")
    if fail is not None:
        return fail

    if want_all:
        targets = [i for i, r in rows.items() if not r.get("isRead")]
    else:
        targets = want_ids
        # ⚠️ 这里刻意**不是**"按 id 原样交给服务端、由它在全量里改"（那是本工具
        # 20260923 批 7 最早的写法）：列表只回最近 100 条，交出去确实能覆盖到更早的
        # 那些，但代价是**编出来的 id 也会被照样发出去**——服务端按它匹配 0 行，
        # 写后复核报"未确认生效"，过程行还说成「服务不可用」。trace 实证
        # （`20260923T130033_9`）：planner 把摘要里的条数「3」当 id 填了进来（真实那条
        # 是 23），用户看到的就是"标记已读未成功（服务不可用）"——一次注定 0 行的写
        # 换来一句误导。现在改成写**之前**先确认这些 id 至少是**我读得到的**：
        # 一个都不在就零写 + not_found 如实说清。上限 100 条这个边界写进话里
        # （**不谎称"站内没有这条"**——它可能只是比最近 100 条更早）。
        unknown = [i for i in targets if i not in rows]
        if len(unknown) == len(targets):
            return not_found(
                f"你点名的通知 {'、'.join(str(i) for i in unknown)} 不在我读到的通知列表"
                f"（最近 {len(rows)} 条，最多 100 条）里，无法确认是哪几条——"
                f"本次一个字节都没改（没有发出写请求）。"
                f"要标记哪一条请先读一遍通知列表拿它的 id（列表里每条都带着 id）。")
        if unknown:
            return not_found(
                f"你点名的通知里 {'、'.join(str(i) for i in unknown)} 不在我读到的通知列表"
                f"（最近 {len(rows)} 条，最多 100 条）里，无法确认是哪几条——"
                f"本次一个字节都没改（没有发出写请求）。"
                f"请把 id 核对一遍（或说「全部标记已读」由系统按未读的那几条来标）。")
        # 已经全是已读 → 不发请求（写前读的副产品，见本节头注的幂等段）。
        # 显式循环而非 all(...)：见本函数 docstring 的形参遮蔽警告。
        all_read = True
        for i in targets:
            if rows.get(i, {}).get("isRead") is not True:
                all_read = False
                break
        if all_read:
            return ok(f"通知 {('、'.join(str(i) for i in targets))} 本来就是已读，"
                      f"无需改动（没有发出写请求）。",
                      meta={"op": "notice_read", "change": "本来就读过", "noop": True})
    if want_all and not targets:
        return ok("你的通知本来就没有未读的，无需改动（没有发出写请求）。",
                  meta={"op": "notice_read", "change": "本来就没未读", "noop": True})

    before_sum = _own_get("/api/protected/notifications/summary", config)
    if isinstance(before_sum, ToolResult):
        return _pre_read_fail(before_sum, "未读通知的条数（无法确认改动是否生效）")
    n_before = (before_sum or {}).get("notifications")
    if not isinstance(n_before, int) or isinstance(n_before, bool):
        return unavailable("读不回未读通知的条数，无法确认改动是否生效，本次未改动")

    payload = {"ids": [], "all": True} if want_all else {"ids": sorted(targets), "all": False}
    data = _own_post("/api/protected/notifications/read", payload, config)
    if isinstance(data, ToolResult):
        return data

    # 复核只认**服务端重新数出来的**未读数（写入响应里那个数是更新后当场查的，
    # 但"接口说成功了"仍然不是判据——读侧的判据必须是一次独立读数）。
    after_sum = _own_get("/api/protected/notifications/summary", config)
    if isinstance(after_sum, ToolResult):
        return unavailable(f"标记已读请求已发出，但读不回未读条数（{after_sum}），"
                           f"本次改动未确认生效（不要声称已标记）")
    n_after = (after_sum or {}).get("notifications")
    if not isinstance(n_after, int) or isinstance(n_after, bool):
        return unavailable("标记已读请求已发出，但读不回未读条数，本次改动未确认生效")
    if n_after >= n_before:
        return unavailable(f"标记已读请求已发出，但读回未读通知仍是 {n_after} 条"
                           f"（可能这几条本来就是已读）——本次改动未确认生效，不要声称已标记")
    marked = n_before - n_after
    m_after = (after_sum or {}).get("messages")
    tail = f" / 私信 {m_after}" if isinstance(m_after, int) and not isinstance(m_after, bool) else ""
    return ok(f"已把 {marked} 条通知标记为已读（现在未读：通知 {n_after} 条{tail}）。",
              meta={"op": "notice_read", "change": f"标记已读 {marked} 条"})


# ---------------------------------------------------------------------------
# 站内信（私信）（20260923 批 8）：读信箱 / 标记已读（发信见本节的最后一小节）
# ---------------------------------------------------------------------------
# **术语必须分开**（用户点名要求）：站内信（私信）≠ 河灯留言。
#   · 河灯留言 = 访客在**公开页面**写下的内容（灯影集/说说），谁都看得见
#     ——那是 `list_guestbook` / `list_talks`。
#   · 站内信 = 一封**一对一**的信，只有收发双方看得见——就是这一节。
# 两者混用会让 narrator 说出"我看了你留言板上收到的私信"这种不存在的东西，
# 所以每个工具的 docstring 与技能描述里都把这句话写死一遍。
#
# 端点（Rust src/routes/profile.rs）：`GET /api/protected/messages`（收件箱 +
# 发件箱，各最近 100 条）、`POST /api/protected/messages/read`（只动收件箱里我的
# 信）、`POST /api/protected/messages`（发一条，收件人**按账号或 UID** 精确匹配）。
# 收发双方都只认 auth_uid——**没有"读别人的信箱"的接口**。
#
# 读那一半（scope = read.own）与收藏/通知同族：`_own_get` fail-closed，读不到
# 绝不返回空（"你信箱里没有信"是结论，"没读到"不是）。


def _mailbox_inbox(data) -> dict[int, dict] | None:
    """信箱返回（`{inbox, outbox, unread}`）→ `{id: 收件箱那一行}`；形态不对 → None。

    只收**收件箱**：这个函数的调用点（写前读、写后复核、弹窗判据）都只关心
    "我收到的信"，发件箱里的信被别人读没读是别人的事（`isRead` 只对收件人有意义）。
    """
    if not isinstance(data, dict) or not isinstance(data.get("inbox"), list):
        return None
    out: dict[int, dict] = {}
    for r in data["inbox"]:
        if not isinstance(r, dict):
            continue
        mid = _as_article_id(r.get("id"))
        if mid is not None:
            out[mid] = r
    return out


def _mailbox_snapshot(config: RunnableConfig,
                      what: str) -> tuple[dict | None, ToolResult | None]:
    """本人信箱 → `(信箱原始返回, 失败原因)`，两者**恰有一个**为 None。

    这里回的是**原始返回**而不是直接给收件箱那半：同一个 payload 上还有第二个
    事实（`unread` 未读封数）是 read_messages 的写前基线，只给 `inbox` 会逼它再读一次
    （或者更糟——另算一遍未读数，那就成了同一件事的第二份判据）。调用方各自用
    `_mailbox_inbox` / `_mailbox_unread` 取自己那半。

    与 `_favorites_snapshot` 同一条纪律：弹窗的「已达成」判据与工具的写前读**同一份**。
    """
    got = _own_get("/api/protected/messages", config)
    if isinstance(got, ToolResult):
        return None, _pre_read_fail(got, what)
    if _mailbox_inbox(got) is None:
        return None, unavailable(f"读不到{what}，本次未改动")
    return got, None


def _mailbox_unread(data) -> int | None:
    """信箱返回里的未读封数（Rust `MailboxDto.unread`）；读不出 → None（不写 0）。"""
    n = (data or {}).get("unread") if isinstance(data, dict) else None
    return n if isinstance(n, int) and not isinstance(n, bool) else None


@tool
def list_my_messages(config: RunnableConfig) -> str:
    """列出**当前登录用户自己**的站内信（私信）：收件箱与发件箱各最近 100 条，
    外加未读封数（返回 inbox / outbox / unread，每封带 id / fromUserId / toUserId /
    peerName / title / content / isRead / createdAt）。
    访客问"我的信箱里有什么""谁给我写过信""我发出去的信"时用。
    ⚠️ 站内信与**河灯留言**是两回事：留言在公开页面上、谁都看得见（那是
    list_guestbook）；站内信是一对一写的信，只有收发双方看得见。
    未登录时如实告知读不到。"""
    data = _own_get("/api/protected/messages", config)
    return _shape(data)


@tool
def read_messages(
    config: RunnableConfig,
    ids: Annotated[list[int] | None, "要标记已读的那几封**收到的信**的 id（用户点名了具体"
                                     "哪几封时给；id 只能来自 list_my_messages 的返回或执行"
                                     "记忆摘要里 `id《标题》` 形态的编号，**不许把「共 N 封」"
                                     "里的 N 当 id**，也不许自己编"] = None,
    all: Annotated[bool | None, "true = 把**全部**未读的收信标记已读（用户说了「全部/都/"
                                "所有」才给；只是「把信读了」这种没限定范围的**不要自己填 True**）"] = None,
) -> str:
    """把**当前登录用户自己收到的**站内信标记为已读（按 id 或全部）。**已读不可撤销**：
    标记之后那几封就不再是未读（头顶红点问的是"未读总数 > 0"，而那个总数是**通知与
    站内信之和**：只标一部分时它不变，把信全标完也还要通知那边也没有未读才会消失），
    所以只有用户明确说了要标记才用。
    既能要不到 id 也没说"全部"时**什么都不动**，如实问清是哪几封。
    只动**收到的**信——发出去的信别人读没读改不了。
    未登录时如实告知，一个请求都不发。

    ⚠️ 与 read_notifications 同一个坑：形参名 `all` 遮蔽内置 `all()`，本函数体内
    不得再调用内置 `all(...)`（要判"全都没读"用显式循环）。
    """
    want_all = _as_bool(all) is True
    want_ids = _as_ids(ids)
    guard = _own_write_guard(config)
    if guard is not None:
        return guard
    if not want_all and not want_ids:
        return unavailable("没有指出要标记哪几封信（是全部未读、还是某几封的 id），未改动")

    before, fail = _mailbox_snapshot(config, "你的信箱（无法确认哪几封是未读）")
    if fail is not None:
        return fail
    rows = _mailbox_inbox(before)

    if want_all:
        targets = [i for i, r in rows.items() if r.get("isRead") is not True]
    else:
        targets = want_ids
        # 与 read_notifications 同一条纪律（20260923 三轮的那次误导）：**写之前**先确认
        # 这些 id 至少是"我读得到的"——一个都不在就零写 + not_found 如实说清，绝不让
        # 一个编出来的 id 变成一次注定 0 行的写 + 一句「服务不可用」的误报。
        unknown = [i for i in targets if i not in rows]
        if len(unknown) == len(targets):
            return not_found(
                f"你点名的信 {'、'.join(str(i) for i in unknown)} 不在我读到的收件箱"
                f"（最近 {len(rows)} 封，最多 100 封）里，无法确认是哪几封——"
                f"本次一个字节都没改（没有发出写请求）。"
                f"要标记哪一封请先读一遍信箱拿它的 id（每封都带着 id）。")
        if unknown:
            return not_found(
                f"你点名的信里 {'、'.join(str(i) for i in unknown)} 不在我读到的收件箱"
                f"（最近 {len(rows)} 封，最多 100 封）里，无法确认是哪几封——"
                f"本次一个字节都没改（没有发出写请求）。"
                f"请把 id 核对一遍（或说「全部标记已读」由系统按未读的那几封来标）。")
        all_read = True
        for i in targets:
            if rows.get(i, {}).get("isRead") is not True:
                all_read = False
                break
        if all_read:
            return ok(f"信 {('、'.join(str(i) for i in targets))} 本来就是已读，"
                      f"无需改动（没有发出写请求）。",
                      meta={"op": "message_read", "change": "本来就读过", "noop": True})
    if want_all and not targets:
        return ok("你的收件箱本来就没有未读的信，无需改动（没有发出写请求）。",
                  meta={"op": "message_read", "change": "本来就没未读", "noop": True})

    n_before = _mailbox_unread(before)
    if n_before is None:
        return unavailable("读不回未读的封数，无法确认改动是否生效，本次未改动")

    payload = {"ids": [], "all": True} if want_all else {"ids": sorted(targets), "all": False}
    data = _own_post("/api/protected/messages/read", payload, config)
    if isinstance(data, ToolResult):
        return data

    # 写后复核 = **一次独立读数**（接口回的那份不算判据）：未读封数必须真降，
    # 且点名的每一封都确实是已读了——只看"降了"会在"标错了几封、又漏了几封"
    # 的巧合下判成功（净变化相同）。
    after = _own_get("/api/protected/messages", config)
    if isinstance(after, ToolResult):
        return unavailable(f"标记已读请求已发出，但读不回你的信箱（{after}），"
                           f"本次改动未确认生效（不要声称已标记）")
    n_after = _mailbox_unread(after)
    if n_after is None:
        return unavailable("标记已读请求已发出，但读不回未读封数，本次改动未确认生效")
    if n_after >= n_before:
        return unavailable(f"标记已读请求已发出，但读回未读的信仍是 {n_after} 封"
                           f"（可能这几封本来就是已读）——本次改动未确认生效，不要声称已标记")
    arows = _mailbox_inbox(after) or {}
    still = [i for i in targets if arows.get(i, {}).get("isRead") is not True]
    if still:
        return unavailable(f"标记已读请求已发出，但读回信 {'、'.join(str(i) for i in still)} "
                           f"还不是已读——本次改动未确认生效，不要声称已标记")
    return ok(f"已把 {n_before - n_after} 封信标记为已读（收件箱现在未读 {n_after} 封）。",
              meta={"op": "message_read", "change": f"标记已读 {n_before - n_after} 封"})


# ---------------------------------------------------------------------------
# 后台首页待办 / 日程（20260926）：读整份 / 追加一条 / 翻完成标记
# ---------------------------------------------------------------------------
# 端点在守卫域内（Rust `src/routes/todos.rs`，`auth_guard` 之后）⇒ 这一族照例是
# `admin.console` / `write.console`（见 agent/authz.py 的登记与那里的取舍说明：
# 接口虽然按 uid 存"你自己的那份列表"，但普通登录用户前端根本打不开后台首页，
# 取 read.own/write.own 会让授权层对普通用户说"允许"而 Rust 随后 403）。
#
# **agent 的写通道有两条**（POST /api/protected/todos/item 追加一条、
# POST /api/protected/todos/done 翻某一条的完成标记），而它读的却是整份 GET：
# 为什么不用 PUT 整份覆盖，见该文件头注——主人自己的那份列表在他手里，agent 先读
# 再写会把主人刚做的改动抹掉，而"发一份自己拼的"在整份覆盖的语义下等于清空他的
# 待办。两条写通道都只动**一行**，这正是它们能绕开那个取舍的原因。
#
# 两条通道的**定位判据同为"正文逐字相等"**（这张列表线上从不回行 id，正文是唯一
# 能认出是哪一行的东西）：服务端 `pick_todo` 判一遍，agent 侧读回整份再判一遍
# （**纵深，不互替**——agent 那遍是为了在发出请求之前就能如实说"没有这一条/
# 分不清是哪一条"，且写后复核也只有这条路能认回那一行）。
#
# 契约同源（改一侧必须同步另一侧）：条数上限与正文上限都写在 Rust 那侧的
# `MAX_TODOS` / `MAX_TEXT_CHARS`；这里各留一份**只为不发注定被拒的请求**，真正的
# 判据在服务端（本地这份写小了只是少发一次，写大了服务端照样拒）。
_TODO_TEXT_LIMIT = 200     # = src/routes/todos.rs MAX_TEXT_CHARS（按字符数）
_TODO_MAX_ROWS = 200       # = src/routes/todos.rs MAX_TODOS


def _todo_rows(data) -> list[dict] | None:
    """接口返回 → 待办行列表；形态不对 → None（≠ 空列表，同 `_tag_index` 的区分）。"""
    if not isinstance(data, list):
        return None
    return [r for r in data if isinstance(r, dict)]


def _todo_row_key(row: dict) -> tuple[str, str]:
    """一行的**判等键** = (正文, 排期日)。用于"写后复核"里认哪一条是新加的。

    为什么不用 id：这张列表的读接口**不回 id**（前端那份列表的"身份"是它自己那份
    数组，服务端只存"此刻的样子"，见 Rust 头注）——所以复核只能按内容认。用内容
    判等的前提也写清楚了：正文与排期日都由我们自己发出去、服务端原样落库，两边
    逐字相等才是"读到了我加的那一条"。
    """
    return (str(row.get("text") or "").strip(),
            str(row.get("date") or "").strip())


@tool
def list_dashboard_todos(config: RunnableConfig) -> str:
    """查看**后台首页**的待办 / 日程列表（主人自己在后台首页那张卡里记的事）：
    每行给出正文、排期日（写「未排期」的是没定日子那条）与是否已完成。
    主人问"我有哪些待办 / 我日程上有什么 / 那个 xx 是不是还没做"时用它。
    这是**他自己那份私人列表**，站内公开页面上看不到；需要管理员身份。"""
    from agent import adminops as A
    data = _admin_get("/api/protected/todos", config)
    if isinstance(data, ToolResult):
        return data
    rows = _todo_rows(data)
    if rows is None:
        return unavailable("后台待办接口返回的不是列表，没法读出你的待办")
    if not rows:
        # 读到了、就是空的 —— 这是**事实**（`empty`，checker 照常 PASS 进回执），
        # 不能写成 unavailable：那会把"你还没记过待办"说成"系统挂了"。
        return empty("后台首页的待办列表现在是空的（一条都没记）。")
    return ok(A.render_todo_list(rows), meta={"op": "dashboard_todo_list",
                                              "count": len(rows)})


@tool
def create_dashboard_todo(
    text: Annotated[str, "这条待办/日程的正文：**照抄主人说的那件事**，不许润色、补细节或改写法"
                         "（他给几个字就写几个字）"],
    config: RunnableConfig,
    date: Annotated[str | None, "排期日：主人说了哪一天就填那一天（「明天」「后天」直接照抄他的"
                                "说法也行，系统会翻成日期）；他没说日子就不填，**不要自己挑一个**"] = None,
) -> str:
    """在**后台首页的待办列表**里加一条（排期可选）。这是主人自己的私人清单——
    不会对外可见，加完他自己随时能改能删；他问"记一下…/帮我记着…/安排一下…"时用它。
    只**追加**一条，不动列表里原有的任何一条。需要管理员身份，且要经主人确认。"""
    from agent import adminops as A
    body = str(text or "").strip()
    if not body:
        return unavailable("这条待办没写内容（正文是空的），本次未改动")
    if len(body) > _TODO_TEXT_LIMIT:
        return unavailable(f"这条待办太长了（{len(body)} 字，最多 {_TODO_TEXT_LIMIT} 字），"
                           f"本次未改动——请让主人把这件事说短一点")
    raw_date = "" if date is None else str(date).strip()
    due = A.normalize_due_date(raw_date) if raw_date else None
    if raw_date and due is None:
        # 认不出来就不挑一个顶上（见 adminops.normalize_due_date 头注）：错一天的
        # 日程会静静地躺在后台日历的错误格子里，主人不翻到那天根本不会发现。
        return unavailable(f"认不出排期日「{raw_date}」（只认 年-月-日 / 年/月/日 / X月X日 / "
                           f"今天·明天·后天），本次未改动——请向主人问清是哪一天")

    # 写前先读（同族纪律）：① 拿"加之前有几条"当复核基线 ② 满员时不发注定被拒的请求
    before = _admin_get("/api/protected/todos", config)
    if isinstance(before, ToolResult):
        return _pre_read_fail(before, "你后台首页的待办列表")
    rows_before = _todo_rows(before)
    if rows_before is None:
        return unavailable("读回的后台待办不是列表，无法确认这次追加，本次未改动")
    if len(rows_before) >= _TODO_MAX_ROWS:
        return unavailable(f"待办已经满了（{len(rows_before)} 条，上限 {_TODO_MAX_ROWS} 条），"
                           f"本次未改动——请先到后台首页清掉几条")
    key = (body, due or "")
    n_before = sum(1 for r in rows_before if _todo_row_key(r) == key)

    payload: dict = {"text": body}
    if due:
        payload["date"] = due       # 没排期就**不带这个键**（不写 null，省得两种"没填"混在一起）
    data = _admin_request("POST", "/api/protected/todos/item", payload, config)
    if isinstance(data, ToolResult):
        return data

    # 写后复核 = **一次独立读数**（接口回的那一条不算判据）：这一条必须真在列表里，
    # 且**同键的条数比写前多**——只判"列表里有这么一条"会在"主人本来就记过同样一条"
    # 的情况下把一次失败的追加判成功（净变化才是判据，同 read_messages 那条注释）。
    after = _admin_get("/api/protected/todos", config)
    if isinstance(after, ToolResult):
        return unavailable(f"追加请求已发出，但读不回你后台首页的待办列表（{after}），"
                           f"本次改动未确认生效")
    rows_after = _todo_rows(after)
    if rows_after is None:
        return unavailable("追加请求已发出，但读回的后台待办不是列表，本次改动未确认生效")
    n_after = sum(1 for r in rows_after if _todo_row_key(r) == key)
    if n_after <= n_before:
        return unavailable(f"追加请求已发出，但读回列表里没多出这一条"
                           f"（同内容的仍是 {n_after} 条，写前 {n_before} 条）"
                           f"——本次改动未确认生效，不要声称已记下")
    return ok(A.render_todo_added(body, due),
              meta={"op": "dashboard_todo_add", "text": body, "date": due or "",
                    "count": len(rows_after)})


def _todo_text_hits(rows, text: str) -> list[dict]:
    """待办行快照里按**正文逐字相等**找命中行（写侧判据，渲染侧另有一份见 adminops）。

    与 Rust `pick_todo` 同判据（`row.text == text`，两边都先 trim）：这张列表**线上
    从不回行 id**，正文是唯一能认出是哪一行的东西。逐字相等意味着"买 菜"与"买菜"
    是两条不同的行——这不是严苛，是**唯一**能保证"我们勾的就是主人指的那一条"的
    判据：改成模糊匹配之后，"把「买菜」勾了"会在同名的两条里挑一条（挑错=主人以为
    办完的事其实没办，而列表上看不出区别）。
    """
    want = str(text or "").strip()
    if not want:
        return []
    return [r for r in (rows or [])
            if isinstance(r, dict) and _todo_row_key(r)[0] == want]


@tool
def complete_dashboard_todo(
    text: Annotated[str, "要勾成完成的那条待办的**正文原样**——必须一字不差地照抄它此刻"
                         "在列表里的写法；主人只给了模糊说法（「那个买菜的」）时先读列表"
                         "（list_dashboard_todos）再照抄，**不要自己改写或猜**"],
    config: RunnableConfig,
) -> str:
    """把**后台首页待办列表**里的某一条勾成完成（那行前面的勾）。它写的是主人自己
    那份私人清单，站内公开页面上看不到；他问"那个 xx 办完了 / 帮我把它勾掉"时用它。
    `text` 必须是那一行**现在的正文原样**：这张列表没有行号，正文是唯一能认出是
    哪一条的东西——对不上、或有多条同名，就一条都不改、如实告诉他。
    **只翻完成标记**（正文与排期一个字都不动），**也不做**"取消完成"。需要管理员
    身份，且要经主人确认。"""
    from agent import adminops as A
    body = str(text or "").strip()
    if not body:
        return unavailable("这条待办没写内容（正文是空的），本次未改动")
    if len(body) > _TODO_TEXT_LIMIT:
        return unavailable(f"这条待办太长了（{len(body)} 字，最多 {_TODO_TEXT_LIMIT} 字），"
                           f"本次未改动——请让主人把这件事说短一点")

    # 写前先读（同族纪律）：① 在**发出请求之前**就认出是哪一条——查无此条/有多条时
    # 一个字节都不发（服务端也会拒，但那样主人拿到的是一句"服务端说没有"，而不是
    # 我们读到的"你列表里现在有哪几条"）② 顺手记下它此刻的完成状态当回执基线。
    before = _admin_get("/api/protected/todos", config)
    if isinstance(before, ToolResult):
        return _pre_read_fail(before, "你后台首页的待办列表")
    rows_before = _todo_rows(before)
    if rows_before is None:
        return unavailable("读回的后台待办不是列表，没法确认你要勾的是哪一条，本次未改动")
    hits = _todo_text_hits(rows_before, body)
    if not hits:
        if not rows_before:
            return not_found("你后台首页的待办列表现在是空的（一条都没记），没有可勾的")
        return not_found(f"你后台首页的待办里没有「{body}」这一条（列表里现在有 "
                         f"{len(rows_before)} 条）——请照那一行现在的正文说，"
                         f"或先读一遍列表再指")
    if len(hits) > 1:
        # **歧义即零写**（同 Rust `pick_todo`）：绝不替主人挑一条——挑错的那次
        # 在列表上看起来和挑对一模一样。
        where = "、".join(A.render_todo_when(r) for r in hits)
        return not_found(f"有 {len(hits)} 条待办都叫「{body}」，分不清是哪一条"
                         f"（{where}）——先到后台首页把其中一条改个说法")
    before_done = bool(hits[0].get("done"))

    data = _admin_todo_done_post({"text": body, "done": True}, config)
    if isinstance(data, ToolResult):
        return data

    # 写后复核 = **一次独立读数**（接口回的那一条不算判据，同 create_dashboard_todo）：
    # 按同一 key 找回那一行，它必须真的是完成态。这一条**不能**用"列表里有没有这么
    # 一条"代替——那一行在写之前就在（这是"翻标记"不是"新增"），只有 done 翻转才是
    # 净变化。
    after = _admin_get("/api/protected/todos", config)
    if isinstance(after, ToolResult):
        return unavailable(f"勾完成的请求已发出，但读不回你后台首页的待办列表（{after}），"
                           f"本次改动未确认生效")
    rows_after = _todo_rows(after)
    if rows_after is None:
        return unavailable("勾完成的请求已发出，但读回的后台待办不是列表，本次改动未确认生效")
    now_hits = _todo_text_hits(rows_after, body)
    if len(now_hits) != 1:
        return unavailable(f"勾完成的请求已发出，但读回列表里叫「{body}」的现在有 "
                           f"{len(now_hits)} 条（写前 {len(hits)} 条）——"
                           f"本次改动未确认生效，不要声称已勾完成")
    after_done = bool(now_hits[0].get("done"))
    if not after_done:
        return unavailable(f"勾完成的请求已发出，但读回列表里这一条仍是**未完成**"
                           f"——本次改动未确认生效，不要声称已勾完成")
    # 幂等**不短路**（同冻结/解冻族）：写前已经是完成态也照发请求（服务端那个分支
    # 是真 no-op），结论由上面这次复核给——回执按 `changed` 如实区分"刚勾的"与
    # "本来就是"，绝不把一次什么都没做的请求叙述成一个动作。
    return ok(A.render_todo_done(body, changed=not before_done),
              meta={"op": "dashboard_todo_done",
                    "before": "已完成" if before_done else "未完成",
                    "after": "已完成"})


# ---------------------------------------------------------------------------
# 管理助手写工具：账号管理（冻结 / 解冻 / 发通知）（20260926）
# ---------------------------------------------------------------------------
# 三件共用"目标 = 后台账号列表里的**账号名**"这条唯一通道（`_user_directory` /
# `_find_named_user`），差别在**动的是什么**：冻结族动的是对方的登录能力、发通知
# 动的是"发给对方的一段话"。下面四条是冻结族独有的事实，发通知那件只在 ①② 上同族
# （它也走名录、也只按名字），③④ 是它自己的（见 `_send_user_notice` 头注）。
#
# 与标签/分类/公告/留言同一套纪律（名字通道 + fail-closed + 读回复核），四件**这一族
# 独有**的事实决定了实现的形状：
#   ① 账号名录 `GET /api/temp-users` 是全站唯一**不回 `ApiResponse` 信封**的
#      `/api/protected` 接口（裸数组）⇒ 不能复用 `_admin_get`，见 `_user_directory`；
#   ② **只按名字**指认，没有编号通道（`user_id` 参数）：后台名录不列超管行这道防线
#      （Rust `authz::is_listable_role`），只在"定位必须经过名录"时才成立——开一扇
#      编号门等于把"冻一个看不见的超管"从第二扇门重新打开。名字重名就直接拒绝
#      （选错就是冻了另一个活人）；
#   ③ **两个工具而不是一个带布尔参数的**：方向必须写进**工具名**。单工具
#      `set_account_status(frozen=…)` 的方向由 planner 填，而 `_confirm_grant_plan`
#      的"工具 ⊆ 技能 plan"判据**看不见**这次翻转（工具名没变）⇒ 卡上写「冻结」、
#      实际执行解冻，且无人可见（先例：add_favorite / remove_favorite 也是这样一对）；
#   ④ 拒绝的形态是**后端的政策**，不是"目标不存在"⇒ 走 `policy_frame`
#      （`__ERROR__` + 原因码族），见 `_admin_status_post` 与 agent/adminops.py。

def _user_directory(config: RunnableConfig) -> dict[int, dict] | ToolResult:
    """读后台**账号名录** → `{uid: 行}`；读不到 → `ToolResult`（失败，不是 None）。

    ⚠️ **不要把它"统一"回 `_admin_get`**（本节最容易改错的一处）：
    `GET /api/temp-users` 是全站唯一**不回 `ApiResponse` 信封**的 `/api/protected`
    接口（Rust 侧直接 `Json<Vec<TempUserInfo>>`，src/routes/temp_user.rs），而
    `_principal_get` 里取业务码的那一行（`body.get("code")`）在**裸 list** 上会抛
    `AttributeError`——异常冒到 execute 的兜底，表现成过程行「执行出错」，看起来像
    服务挂了。也**不要反过来**去改后端把它包成信封：前端 `Users/index.tsx` 按
    `Array.isArray(res?.data)` 读、`scripts/probe_token_revoke.py` 直接迭代裸数组，
    包信封会同时打断这两处。所以这里自带一次请求，其余形状照抄 `_principal_get`
    （同一把 60 秒代签 JWT、同一条 `/api/protected` 前缀、同一套 fail-closed），
    **唯一**的差别就是最后认的是"顶层是不是 list"。

    失败返回 `ToolResult` 而不是 `None`：本族的调用方（按名字解析）在**任何**读不到
    的情况下都必须零写，原样往外传一个带人话的 ToolResult 比 `None` 更好用
    （`_tag_index` 那套 `None` 是给"读不到也要出报表"的只读场景用的）。
    """
    uid = _device_get_user_id(config)
    if uid <= 0:
        return unavailable("无法获取当前用户身份，后台账号列表不可用")
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.get(f"{ADMIN_BASE}/api/temp-users", headers=headers, timeout=15)
    except Exception as exc:
        logger.error("user directory call failed: %s", exc)
        return unavailable(f"读后台账号列表的请求失败: {exc}")
    if resp.status_code in (401, 403):
        return unavailable("当前身份无权访问后台账号列表（该功能仅管理员可用）")
    if resp.status_code != 200:
        return unavailable(f"后台账号列表返回 HTTP {resp.status_code}")
    try:
        rows = resp.json()
    except Exception:
        return unavailable("后台账号列表返回的不是 JSON")
    if not isinstance(rows, list):
        # 信封形状 = 后端换了契约。**不当成台账**：宁可如实说读不懂，也不让一次契约
        # 变更变成"名录里一个账号都没有"（下一步就是零写，而"没有这个账号"是假话）。
        return unavailable("后台账号列表返回的形状不对（不是数组），读不出账号名录")
    out: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            rid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        row["id"] = rid            # id 就地归一成 int：写后复核直接按 id 找回那一行
        out[rid] = row
    return out


def _account_frozen(row) -> bool | None:
    """名录里那一行"是不是冻结状态" → True/False；读不出这个字段 → **None**。

    判据 = `status != 0`，与 Rust `authz::is_frozen` 同一条（取值域见 `authz.rs`）。
    `None` **不是 False**：把"读不到"读成"正常"会让写后复核把一次失败的冻结判成功
    ——同 `_tag_index` 的 `None ≠ 空表` 那条纪律。
    """
    try:
        return int(row.get("status")) != 0
    except (TypeError, ValueError, AttributeError):
        return None


def _find_named_user(name, config, index=None):
    """按**账号名**在后台账号名录里找一个账号 → `(行, None)` 或 `(None, 拒绝文本)`。

    四种结局与 `_find_named_tag` 同取向（唯一命中才动手；同名多个如实说分不清、
    **不替主人挑一个**；查无此名如实说没有并附名字最接近的候选；名录读不到单独
    一种说法——"读不到" ≠ "没有"），**但有一处刻意的方向差异**：

      名录读不出来时这里**一个字节都不发**（调用方直接零写收场），而
      `graph._write_target_refusal` 在同一个情形下是"读不到就不拦"（放行给工具）。
      两边不是不一致，是**定位方式不同**：那边台账只用来印证一个已知的名字，读不到
      只损失一次预检；这边按名字定位是**唯一**的定位方式，读不到就没有任何可执行的
      落点——放行等于让 planner 拿一个没核过的名字去冻结一个活人。
      （不写这句，下一个人会把它当不一致"修"齐。）

    `index` 是可选的外部快照（一次操作要读同一份名录两回时用，同 `_find_named_tag`）。
    """
    if index is None:
        index = _user_directory(config)
    if isinstance(index, ToolResult):
        return None, str(index)
    want = str(name or "").strip()
    if not want:
        return None, "账号名为空，本次未改动"
    hits = [r for r in index.values()
            if str(r.get("username") or "").strip() == want]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        cands = "、".join(f"id={r.get('id')}" for r in hits)
        return None, (f"后台有 {len(hits)} 个账号都叫「{want}」（{cands}）："
                      f"无法确定要动的是哪一个，本次未改动——请让主人说清是哪一个"
                      f"（id 是后台账号列表里那一行的编号）")
    near = _near_miss_names(want, [(r.get("id"), str(r.get("username") or ""))
                                   for r in index.values()])
    if near:
        # 近失（同 `_near_miss_names` 长注）：没有完全同名的，但名字接近——把候选
        # 摆出来请主人点名，而不是丢一句"没有这个账号"（他记得后台里有）。
        cands2 = "、".join(f"id={cid}「{nm}」" for cid, nm in near)
        return None, (f"后台账号列表里没有叫「{want}」的账号（完全同名的一个都没有）；"
                      f"名字最接近的是 {cands2}。本次未改动——若就是其中一个，"
                      f"请照它的**完整账号名**再说一遍，我按那个名字动手")
    return None, f"后台账号列表里没有叫「{want}」的账号，本次未改动"


# 冻结/解冻每个方向的**结局句**——工具自己发音时用（回执）。卡面问句用的是
# adminops 的 `_ACCOUNT_CONSEQ`：两份措辞必须以同一件事为真（"会话不回来"这半边
# 两个方向都要说清），所以改一处必须看一眼另一处。
_ACCOUNT_DONE = {
    True: "他当前所有登录会话**已经全部失效**，在他被解冻之前连登录都进不来",
    False: "他现在**能重新登录**了（冻结期间被踢下线的会话不会自动恢复，需要他自己重新登录）",
}


def _set_account_frozen(name, frozen: bool, config: RunnableConfig) -> ToolResult:
    """冻结/解冻的公共实现（两个 @tool 只是方向不同的薄壳，见本节头注③）。

    照 `delete_tag` 的五段式：① 读名录 → ② 解析出唯一一行 → ③ 写 → ④ 写后重读
    **同一份名录**复核 → ⑤ 出口只有 `ok` / `not_found` / `policy_frame` /
    `unavailable`，绝不 `return ""`。
    """
    from agent import adminops as A
    want = str(name or "").strip()
    if not want:
        return unavailable("没给出要动的账号名，本次未改动——请让主人说清是哪个账号")

    # ① 写前读：既拿复核基线，也让"名字不存在"在**发请求之前**就响亮地报出来
    before_index = _user_directory(config)
    if isinstance(before_index, ToolResult):
        # 名录读不到 → 明说"本次未改动"（读侧那句原话留在括号里，便于排查）。
        return _pre_read_fail(before_index, "后台账号名录")
    # ② 解析：唯一命中才继续。**index 传进去**，于是这条路上的所有拒绝都是
    #    "目标不明"（读不到那一支已经在上面被挡掉了），出口统一按 not_found 走
    #    ——planner 的应对是换个名字或如实问主人，不是"稍后再试"。
    row, err = _find_named_user(want, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or want)
    was_frozen = _account_frozen(row)

    # ③ 写。**幂等不短路**：目标已经是目标状态时照样发请求（后端那一支是真 no-op），
    #    结论由下面的复核给——短路成 ok 会让回执把一次"没发生的事"读成一个动作
    #    （"刚冻结的"与"本来就是冻结的"对主人是两句不同的话）。
    data = _admin_status_post(f"/api/temp-users/{target_id}/status",
                              {"frozen": frozen}, config)
    if isinstance(data, ToolResult):
        # 政策拒绝（自己 / 超管 / 同级管理员）就走这一支：`_admin_status_post` 已经
        # 把后端的原话包成了 `policy_refused` 帧（checker BLOCK ⇒ 零回执 ⇒ 不进
        # 跨轮执行记忆），这里原样往外传，不改写一个字。
        return data

    # ④ 写后复核：重读**同一份名录**按 id 找回那一行。三种情形一律 unavailable
    #    （"…本次改动未确认生效"），**不许说成功**：读不回 / 那一行不见了
    #    （并发删号）/ 状态仍是旧值。
    after_index = _user_directory(config)
    if isinstance(after_index, ToolResult):
        return unavailable(f"改动请求已发出，但读不回后台账号名录（{after_index}），"
                           f"本次改动未确认生效")
    got = after_index.get(target_id)
    if not isinstance(got, dict):
        return unavailable(f"改动请求已发出，但读回的名录里找不到 id={target_id} 那一行"
                           f"（账号可能已被删除），本次改动未确认生效")
    now_frozen = _account_frozen(got)
    if now_frozen is None:
        return unavailable(f"改动请求已发出，但读回的账号「{username}」没有状态字段，"
                           f"无法确认，本次改动未确认生效")
    if now_frozen != frozen:
        verb = "冻结" if frozen else "解冻"
        state = "冻结" if now_frozen else "正常"
        return unavailable(f"{verb}请求已发出，但读回账号「{username}」的状态仍是"
                           f"「{state}」，本次改动未确认生效")

    changed = (was_frozen != now_frozen)
    return ok(
        A.render_account_status(username, target_id, frozen, changed=changed,
                                before_frozen=was_frozen),
        meta={"op": "account_freeze" if frozen else "account_unfreeze",
              "account_id": target_id, "account_name": username,
              "before": "冻结" if was_frozen else ("正常" if was_frozen is False else ""),
              "after": "冻结" if now_frozen else "正常",
              "change": A.account_change_phrase(frozen, changed)})


@tool
def freeze_account(
    name: Annotated[str, "要冻结的那个后台账号的**账号名**（后台账号列表里看得见的那一行）"],
    config: RunnableConfig,
) -> str:
    """冻结一个后台账号：他立刻被踢下线，**在他被解冻之前连登录都进不来**。
    已登录的会话不会恢复（解冻后他需要重新登录）。需要管理员身份，且每次都要经主人确认。

    **要动的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。管理员之间不能互相冻结，超级管理员
    谁的账号都冻不了——撞上这两条时，如实把系统给的原话转告主人，不要换个说法重试。"""
    return _set_account_frozen(name, True, config)


@tool
def unfreeze_account(
    name: Annotated[str, "要解冻的那个后台账号的**账号名**（后台账号列表里看得见的那一行）"],
    config: RunnableConfig,
) -> str:
    """解冻一个被冻结的后台账号：他重新可以登录了。
    **冻结期间被踢下线的会话不会自动恢复**——解冻不等于"恢复原状"，他需要自己重新
    登录一次。需要管理员身份，且每次都要经主人确认。

    **要动的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。超级管理员的账号谁都解冻不了——
    撞上时如实转告系统给的原话，不要换个说法重试。"""
    return _set_account_frozen(name, False, config)


# 通知的标题/正文上限与**服务端同一处口径**（`src/routes/notice.rs` 的 `TITLE_MAX` /
# `CONTENT_MAX`，前端 `Users/index.tsx` 的 maxLength 也是这两个数）。三处必须一致：
# 前端拦一道、展开层拦一道（零工具 + 说清原因）、工具这一道是硬闸；服务端那一道是
# 最终判据，超长会被拒（**不是**静默截断——见 notice.rs 头注）。
_NOTICE_TITLE_LIMIT = 128
_NOTICE_CONTENT_LIMIT = 1000
# 标题留空时用的默认标题。**是系统写的字**（服务端 `notice::DEFAULT_TITLE` 一处）。
# 这里重复一份只为"工具自己也知道最终会是什么标题"（回执里念得出那个字），
# 真正落库的字由服务端决定。
_NOTICE_DEFAULT_TITLE = "站内通知"


def _send_user_notice(name, content, title, config: RunnableConfig) -> ToolResult:
    """给一个账号发一条站内通知（**只有**这一条写通道）。

    五段式的前两段照 `_set_account_frozen`（① 读名录 → ② 按名字解析出唯一一行），
    后三段**不同族**，三条都是"这一件事没有别的载体"的直接后果：

      · **没有写后复核那条腿**。站内**不存在**"读别人的通知"的通道
        （`/api/protected/notifications` 一族是 own-data only，`profile.rs` 只按自己的
        uid 查），所以公告族那种"建完读回清单、按新旧 id 差集认人"在这里**结构上做不到**。
        复核判据 = **端点回执本身**：`temp_user::send_user_notice` 只在
        `notice::push_notice_checked` 插入成功时才回 code 200（那条 Result 直接决定
        成败分支）——**这是后端契约**，后来人若把"插入失败也回 200"带进来，这一层的
        "成功"就同时失真了。别以为这里漏写了一条腿。
      · **不重读名录**。重读能证明的只是"账号还在"，证明不了通知发出去了——那会是一段
        看着像复核、实际什么都没核的空转（同族里唯一没有读回复核的一件，理由写在这里）。
      · 正文**可以不是主人的原话**（用户拍板：允许把主人的意思整理成一句得体的通知），
        所以这里的措辞纪律与公告族相反：公告要求"只写用户给了的"，这里允许整理，但
        **不许添加主人没说过的事实、承诺或威胁**（「你已被警告三次」这类编造），
        而**唯一的人眼复核点是确认卡**（`adminops.render_notice_action` 把正文**全文**
        印出来）——那条纪律是硬要求，不是措辞偏好。
    """
    from agent import adminops as A
    want = str(name or "").strip()
    if not want:
        return unavailable("没给出要发给哪个账号，本次未发送——请让主人说清是哪个账号")
    body_text = str(content or "").strip()
    if not body_text:
        # 空正文**不发**：一条什么都不说的通知对收件人只是打扰，而且它是一段以主人名义
        # 发出去、删不掉的字（见 notice.rs 那条"发出后没有撤回的通道"）。
        return unavailable("通知正文为空，本次未发送")
    if len(body_text) > _NOTICE_CONTENT_LIMIT:
        return unavailable(f"通知正文太长（{len(body_text)} 字，上限 {_NOTICE_CONTENT_LIMIT} 字），"
                           f"本次未发送")
    head = str(title or "").strip() or _NOTICE_DEFAULT_TITLE
    if len(head) > _NOTICE_TITLE_LIMIT:
        # 超长在**校验**这一层拒（不静默截断）：截断会让主人核对的是没被截的那一句、
        # 库里存的是另一句（同 notice.rs 头注那条取舍）。
        return unavailable(f"通知标题太长（{len(head)} 字，上限 {_NOTICE_TITLE_LIMIT} 字），"
                           f"本次未发送")

    # ① 写前读：既拿"这个名字在不在名录里"的判据，也让"查无此名"在**发请求之前**
    #    就响亮地报出来（同 `_set_account_frozen`）。
    before_index = _user_directory(config)
    if isinstance(before_index, ToolResult):
        return _pre_read_fail(before_index, "后台账号名录")
    # ② 解析：唯一命中才继续（重名 ⇒ not_found，零写——选错就是给**另一个活人**发了一段话）。
    row, err = _find_named_user(want, config, index=before_index)
    if err:
        return not_found(err)
    target_id = int(row.get("id"))
    username = str(row.get("username") or want)

    # ③ 写。失败按原因码分区（见 `_admin_notice_post` 头注），**原样往外传**：
    #    目标类走 not_found（planner 去问主人），其余走 unavailable（不许说成功）。
    data = _admin_notice_post(target_id, head, body_text, config)
    if isinstance(data, ToolResult):
        return data
    # ④ 回执即复核判据（这一族没有第二条腿，见头注）：端点回了 code 200 ⇒
    #    服务端那次 `push_notice_checked` 插入成功。
    return ok(A.render_notice_status(username, target_id, head, body_text),
              meta={"op": "notice_send",
                    "account_id": target_id, "account_name": username})


@tool
def send_user_notice(
    name: Annotated[str, "收件人的**账号名**（后台账号列表里看得见的那一行）"],
    content: Annotated[str, "通知正文（可以是一句整理过的话；不要编造主人没说过的事实、承诺或威胁）"],
    config: RunnableConfig,
    title: Annotated[str | None, "（可选）通知标题：一句话；主人没说标题就不填，**不要自己挑一个**"] = None,
) -> str:
    """给一个后台账号发一条站内通知（会出现在**对方**个人中心的通知面板里，**发出后没有撤回的通道**）。

    **收件人的账号名必须能在后台账号列表里看到**；列表里没有这个名字就当它不存在
    （不要用账号编号，也不要自己拼一个名字）。正文可以按主人的意思整理成一句得体的
    通知，但**不许**添加主人没说过的事实、承诺或威胁。需要管理员身份，且每次都要经
    主人确认（确认卡上会印出**正文全文**，主人核对的就是将要发出的那一句）。"""
    return _send_user_notice(name, content, title, config)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_TOOL_REGISTRY = [
    list_notes,
    search_notes,
    get_article_detail,
    rag_search,
    get_top_notes,
    list_categories,
    list_tags,
    get_announcements,
    list_guestbook,
    list_talks,
    get_blog_info,
    get_social_links,
    get_site_map,
    get_chat_history,
    search_knowledge_base,
    get_current_time,
    get_weather,
    navigate_to,
    toggle_effect,
    toggle_dark_mode,
    list_devices,
    device_oled_display,
    # 管理助手报表（20260921）：scope = admin.console，见本节头注
    get_server_status,
    get_service_health,
    get_moderation_status,
    get_user_stats,
    # 管理助手后台写（20260921 第二轮）：list_admin_notes=admin.console，
    # 写工具=write.console，见"管理助手写工具"节头注
    list_admin_notes,
    create_tag,
    set_article_status,
    set_article_tags,
    # 标签改/删 + 分类增删改（20260921 第四轮）：同样是 write.console，
    # 目标是**名字**（planner 写名字，工具确定性解析成 id，见 _find_named_tag）
    update_tag,
    delete_tag,
    create_category,
    update_category,
    delete_category,
    # 站内公告代发/改/删（20260922 第五轮）：同样是 write.console，目标=公告标题
    create_announcement,
    update_announcement,
    delete_announcement,
    # 河灯留言的人工复核（20260922 第六轮）：同样是 write.console，目标=正文片段
    # （留言没有名字/标题，见 _find_board_comment）
    audit_board_comment,
    delete_board_comment,
    # 用户自己的数据（20260923）：scope = read.own（三档角色都有），见本节头注
    list_my_favorites,
    get_unread_summary,
    list_notifications,
    # 用户自己的数据·写那一半（20260923 批 7）：scope = write.own，写前先读、写后再读，
    # 见"写那一半"节头注
    add_favorite,
    remove_favorite,
    read_notifications,
    # 站内信（私信）（20260923 批 8）：读信箱=read.own，标记已读=write.own，
    # 见"站内信"节头注（术语：站内信 ≠ 河灯留言）
    list_my_messages,
    read_messages,
    # 后台首页待办 / 日程（20260926）：读=admin.console、写两件=write.console，
    # 见"后台首页待办 / 日程"节头注。两条写通道都只动**一行**（追加一条 / 翻一条
    # 的完成标记）——agent 手里没有那份列表，整份覆盖会抹掉主人的改动。
    list_dashboard_todos,
    create_dashboard_todo,
    complete_dashboard_todo,
    # 冻结 / 解冻账号（20260926）：write.console，目标=后台账号列表里的**账号名**，
    # 见"管理助手写工具：冻结 / 解冻账号"节头注。两个工具而不是一个带方向的参数：
    # 方向写进工具名，确认卡与回执才不可能与真正执行的方向相反。
    freeze_account,
    unfreeze_account,
    # 给单个账号发站内通知（20260926）：write.console，目标=同一个账号名录里的名字，
    # 动的却是"发给对方的一段话"（发出后没有撤回的通道）⇒ 同样进「一律弹窗」族，
    # 卡面必须印出**正文全文**由主人核对。见 `_send_user_notice` 头注。
    send_user_notice,
]

def get_all_tools():
    """Return the list of all registered tools."""
    return _TOOL_REGISTRY
