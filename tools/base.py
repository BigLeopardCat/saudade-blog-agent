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

# httpx 客户端复用
_client = httpx.Client(timeout=15, verify=False)

def _get(path: str) -> dict | list:
    """Helper: call API and return data field."""
    try:
        resp = _client.get(f"{API_BASE}{path}")
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") == 200:
            return body["data"]
        logger.warning("API error: %s", body.get("message"))
        return []
    except Exception as exc:
        logger.error("API call failed: %s", exc)
        return []

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
    return str(data)

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
        return str(body.get("data", []))
    except Exception as exc:
        logger.error("Search failed: %s", exc)
        return str(exc)

@tool
def get_article_detail(
    article_id: Annotated[int, "文档的唯一 ID（note 为 noteKey，talk/board 为 talkKey，announcement 为 id）"],
    doc_type: Annotated[str, "文档类型：note（文章，默认）/ talk（说说）/ board（留言）/ announcement（公告）"] = "note",
) -> str:
    """获取指定文档的完整内容（路线 B 契约的解读段：检索只定位、解读读全文）。
    note 走 /notes/:id；talk/board/announcement 无单条详情端点，从列表接口按 key
    过滤（列表已带全文，量小，全量扫描可接受）。"""
    if doc_type == "note":
        return str(_get(f"/notes/{article_id}"))
    endpoint, key_field = {
        "talk": ("/talk", "talkKey"),
        "board": ("/board", "talkKey"),
        "announcement": ("/announcements", "id"),
    }[doc_type]
    for it in _get(endpoint):
        if str(it.get(key_field)) == str(article_id):
            return str(it)
    return "未找到该文档"


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
        hits = search(query, top_k=top_k)
        if not hits:
            return "检索无结果"
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
        return str(exc)

@tool
def get_top_notes() -> str:
    """获取置顶文章列表。"""
    data = _get("/topnotes")
    return str(data)

# ---------------------------------------------------------------------------
# 分类 / 标签 工具
# ---------------------------------------------------------------------------

@tool
def list_categories() -> str:
    """获取全部分类列表，包含分类名称、颜色、图标、文章数量。"""
    data = _get("/category")
    return str(data)

@tool
def list_tags() -> str:
    """获取全部一级标签列表。"""
    data = _get("/tagone")
    return str(data)

# ---------------------------------------------------------------------------
# 公告 工具
# ---------------------------------------------------------------------------

@tool
def get_announcements() -> str:
    """获取博客公告列表。"""
    data = _get("/announcements")
    return str(data)

# ---------------------------------------------------------------------------
# 留言板 工具
# ---------------------------------------------------------------------------

@tool
def list_guestbook() -> str:
    """获取留言板（河灯留言）列表。

    注意：留言板与说说（碎语）是两个独立的数据源——查询"博客里有没有人聊过 X"
    这类问题时，需同时调用 list_talks 检查说说内容，两个都查全后才能回答。
    """
    data = _get("/board")
    return str(data)

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
    return str(data)

# ---------------------------------------------------------------------------
# 站点信息 工具
# ---------------------------------------------------------------------------

@tool
def get_blog_info() -> str:
    """获取博客基本信息：作者、头像、签名、ICP备案号等。"""
    data = _get("/user")
    return str(data)

@tool
def get_social_links() -> str:
    """获取社交链接（QQ、GitHub、BILIBILI等）。"""
    data = _get("/social")
    return str(data)

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
- 留言板 (/guestbook) — 河灯留言（访客放灯许愿/留言）
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
            return f"知识库查询失败: HTTP {resp.status_code}"
        items = resp.json()
        if not items:
            return "知识库中暂无内容"
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
        return f"知识库中未找到与「{query}」相关的内容"
    except Exception as e:
        return f"知识库查询失败: {e}"

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
        return f"Cannot get weather"
    except Exception as e:
        return f"Weather query failed: {e}"

# ---------------------------------------------------------------------------
# 导航工具
# ---------------------------------------------------------------------------

# 导航目标白名单（与 agent/prompts.py 的 navigate_to 约束保持一致）：
# prompt 约束是第一道闸，工具层校验是第二道闸——模型不可信，工具必须自证。
# 教训：模型曾把"友链板块"猜成 /links 直接发出去（真实页是 /guestbook）。
_NAV_EXACT_PATHS = {"/", "/about", "/guestbook", "/talk", "/times", "/login", "/dashboard", "/device-console/"}
_NAV_PREFIX_PATHS = ("/category/", "/article/")


@tool
def navigate_to(
    path: Annotated[str, "Page path to navigate to, e.g. / /times /category/tech /article/3 /talk /guestbook /about"],
    confirm: Annotated[bool, "Whether user confirmation is needed. false=direct nav, true=ask user"] = True,
) -> str:
    """导航到博客页面。页面跳转只能通过调用本工具生效：调用后返回 NAVIGATE:/AUTO_NAVIGATE: 前缀命令，由系统执行跳转。
    严禁在回复正文中自行输出命令前缀文本——那不是工具调用，不会产生任何跳转，属于违规输出，质检会打回重做。"""
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
    未返回命令帧 = 动作未发生，回复不得声称已开/已关（reflector 声称闸/轨迹核对依据）。"""
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
# 场景：REVISE 循环多轮重复调用 / 客户端重试 / MQTT QoS1 at-least-once 重投——
# 工具层保证"同内容只发一次"。（曾防后端强制路由 _force_display 与自主调用双调，
# 20260828 影子系统事故后强制路由已移除。）
_DISPLAY_DEDUP_SECONDS = 30.0
_last_display: dict[int, tuple[str, float]] = {}


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
        return "无法获取当前用户身份，设备列表不可用"
    try:
        resp = httpx.get(
            f"{DEVICE_SERVICE_URL}/api/devices",
            headers={"Authorization": "Bearer " + _sign_user_jwt(uid)},
            timeout=10,
        )
        if resp.status_code == 401:
            return "设备服务认证失败（JWT 无效或过期）"
        devices = resp.json()
        if not devices:
            return "当前用户还没有绑定任何 IoT 设备"
        return "\n".join(
            f"- id={d.get('id')} 名称={d.get('name')} 在线={'是' if d.get('online') else '否'}"
            for d in devices
        )
    except Exception as e:
        return f"查询设备列表失败: {e}"


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
    # 幂等去重：30s 内相同用户相同内容不重复下发（防 REVISE 多轮重复调用、QoS1 重投）
    now = time.time()
    prev = _last_display.get(uid)
    if prev and prev[0] == text and now - prev[1] < _DISPLAY_DEDUP_SECONDS:
        return "该内容刚刚已下发过，无需重复下发（执行结果以设备回执为准）"
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
                return "当前用户还没有绑定任何 IoT 设备"
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
        _last_display[uid] = (text, now)  # 记录本次下发，供去重
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
]

def get_all_tools():
    """Return the list of all registered tools."""
    return _TOOL_REGISTRY
