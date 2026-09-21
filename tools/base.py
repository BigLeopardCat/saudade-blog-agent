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

    def __new__(cls, text: str, kind: str = "ok") -> "ToolResult":
        obj = str.__new__(cls, text)
        obj.kind = kind
        return obj


def ok(text: str) -> ToolResult:
    return ToolResult(text, "ok")


def empty(text: str) -> ToolResult:
    return ToolResult(text, "empty")


def unavailable(text: str) -> ToolResult:
    return ToolResult(text, "unavailable")


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
# 早期图省事。test_hardening.py 里有一条盯着校验开关的断言。
_client = httpx.Client(timeout=15)

def _get(path: str) -> dict | list | ToolResult:
    """Helper: call API and return data field.

    ⚠️ 失败返回 `UPSTREAM_DOWN`（kind=unavailable）而**不是** `[]`——见上面 ToolResult
    的注释：把故障吞成空列表，会让"服务挂了"伪装成"查到了、就是空的"进入执行回执。
    绝大多数调用方 `return _shape(data)`，不改也能拿到人话（只是这时带 unavailable 标记）；
    要迭代结果的（如 get_article_detail）必须自己先判 `isinstance(data, list)`。
    """
    try:
        resp = _client.get(f"{API_BASE}{path}")
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

@tool
def list_notes(
    page: Annotated[int, "Page number, default 1"] = 1,
    page_size: Annotated[int, "Items per page, default 10"] = 10,
) -> str:
    """获取文章列表，按页返回。返回文章标题、描述、分类、标签等信息。"""
    data = _get(f"/notes?page={page}&page_size={page_size}")
    return _shape(data)

@tool
def search_notes(keyword: Annotated[str, "搜索关键词"]) -> str:
    """搜索文章标题和内容，返回匹配的文章列表。"""
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
        return empty("[]") if not data else _shape(data)
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
    """
    if doc_type == "note":
        data = _get(f"/notes/{article_id}")
        if not section or not isinstance(data, dict):
            return _shape(data)          # 故障（unavailable）原样透出，不伪装成空
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
    return empty("未找到该文档")


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
    """获取置顶文章列表。"""
    data = _get("/topnotes")
    return _shape(data)

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
    """获取全部一级标签列表。"""
    data = _get("/tagone")
    return _shape(data)

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

@tool
def get_weather(
    location: Annotated[str, "City name"] = "Beijing",
) -> str:
    """Query weather for a city using wttr.in."""

    try:
        resp = _client.get(f"https://wttr.in/{location}?format=%C+%t+%w+%h",
timeout=10)
        if resp.status_code == 200:
            return f"{location}天气: {resp.text.strip()}"
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
_NAV_EXACT_PATHS = {"/", "/about", "/guestbook", "/talk", "/times", "/login", "/dashboard", "/device-console/"}
_NAV_PREFIX_PATHS = ("/category/", "/article/")


@tool
def navigate_to(
    path: Annotated[str, "Page path to navigate to, e.g. / /times /category/tech /article/3 /talk /guestbook /about"],
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
            f"/times、/login、/dashboard、/category/*、/article/*、/device-console/。"
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


def _admin_get(path: str, config: RunnableConfig) -> dict | list | ToolResult:
    """以发起人身份 GET 一个后台接口（`/api/protected/*`），返回其 data 字段。

    fail-closed 与 `_get` 同族：异常 / 非 200 / 业务码非 200 一律 unavailable，
    **绝不返回空**——"读不到"被当成"就是空的"是这批工具最坏的失败形态
    （报表会言之凿凿地说"没有待审留言"，而实际是一条都没读到）。
    401/403 单独给一句如实的话：那是**身份**问题不是故障，agent 要能据此说出
    "只有管理员能看"，而不是"系统出错了"。
    """
    uid = _device_get_user_id(config)
    if uid <= 0:
        return unavailable("无法获取当前用户身份，后台数据不可用")
    principal = (config.get("configurable", {}) or {}).get("principal")
    headers = {"Authorization": "Bearer " + _sign_local_jwt(uid, getattr(principal, "role", None))}
    try:
        resp = _client.get(f"{ADMIN_BASE}{path}", headers=headers, timeout=15)
    except Exception as exc:
        logger.error("admin API call failed: %s", exc)
        return unavailable(f"后台接口请求失败: {exc}")
    if resp.status_code in (401, 403):
        return unavailable("当前身份无权访问后台数据（该功能仅管理员可用）")
    if resp.status_code != 200:
        return unavailable(f"后台接口返回 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception:
        return unavailable("后台接口返回的不是 JSON")
    if body.get("code") != 200:
        logger.warning("admin API error: %s", body.get("message"))
        return unavailable(f"后台接口报错: {body.get('message')}")
    return body.get("data")


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
def get_moderation_status(config: RunnableConfig) -> str:
    """查看河灯留言的审核状况：总数与待审/已通过/已驳回的分布、AI 侧判定分布、
    需要人工介入的交叉统计（AI 拦下但仍待审、AI 放过但被人驳回）、以及最近待审明细。
    需要管理员身份（读的是后台留言管理视图）。"""
    from agent import reports as R
    data = _admin_get("/api/protect/board", config)
    if isinstance(data, ToolResult):
        return data
    if not data:
        return empty("河灯留言板目前还没有任何留言，没有可审核的内容")
    try:
        return ok(R.render_moderation_status(data))
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
]

def get_all_tools():
    """Return the list of all registered tools."""
    return _TOOL_REGISTRY
