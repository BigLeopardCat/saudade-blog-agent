"""FastAPI server wrapping the LangChain agent for production deployment.

Run with:
    cd /home/ubuntu/memory_blog_rust/saudade-blog-agent
    .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8010 --workers 2
"""

import asyncio
import base64
import contextvars
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Literal
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

from agent import adminops as A  # 过程行中文取值（写工具的预告/完成帧共用）
from agent import confirm  # 待办令牌：签发在 graph 弹窗侧，验签在这里（见 /chat/stream）
from agent import create_agent
from agent.graph import AgentCancelled, graph_input
from agent.principal import Principal
from agent.summarizer import summarize
from agent.skills import NAV_MAP  # 过程行路径反查中文别名用（展示层，非执行依据）
# 写工具参数的归一（20260923 批 7）：与 instantiate_plan 展开时**同一组纯函数**，
# 保证"预告帧"与"计划文本"对同一个参数值的理解一致（两处各写一份必然漂移）。
from agent.skills import _norm_id_list, _norm_true
from rag import search as rag_search, wordgraph
from utils import setup_logging
from utils.logging import get_trace_id, set_trace_id
from utils.trace import finish_trace, record, start_trace

logger = logging.getLogger(__name__)

# Shared thread pool for blocking agent calls
# 8 → 16：LLM 挂起期间任务占用线程直至超时释放（120s），短时间多次对话会占满 8 线程
# 导致后续对话排队卡死；扩容 16 显著降低并发窗口内的排队概率
_executor = ThreadPoolExecutor(max_workers=16)


def _submit_with_context(loop, func, *args):
    """把阻塞调用提交到线程池，并显式传播调用方 context。

    run_in_executor 不拷贝 contextvars（只有 asyncio.to_thread 自动做）——直接提交
    的话，worker 线程里 _trace_id.get() 读回默认值 "-"，agent 图节点日志（planner/
    model/tools/reflector）的 tid 全部丢失。提交前用 copy_context() 快照当前 context
    （含 middleware 设置的 trace_id），线程内经 ctx.run 恢复后再执行目标函数。
    """
    ctx = contextvars.copy_context()
    return loop.run_in_executor(_executor, ctx.run, functools.partial(func, *args))

# 流式输出兜底超时（秒）：与前端 120s 空闲超时对齐。
# LLM/线程池异常挂起时主动终止流，避免对话无限等待（见 event_stream 的 wait_for）
STREAM_IDLE_TIMEOUT = 120.0
# 流式总时长硬上限（秒）：agent 工具调用循环/超长生成时每轮都有输出帧，
# 空闲超时不会触发（帧流动会重置），需用总时长兜底保证流必会终止
STREAM_TOTAL_TIMEOUT = 300.0

# agent 图递归上界（防模型幻觉重试循环烧满 STREAM_TOTAL_TIMEOUT）：
# langchain 1.3 create_agent 默认硬编码 recursion_limit=9999（等效无界），
# 工具幻觉循环（同一意图反复"表演"调用而不真正调用）会打满总时长上限才断开，
# 前端表现为 5 分钟"卡死"。压到 30（每轮循环约 2 图步 = 约 15 次模型-工具往返，
# 正常流程 5 次以内），超限走既有 __ERROR__ 异常路径，卡死窗口缩到 60-90s。
RECURSION_LIMIT = int(os.environ.get("AGENT_RECURSION_LIMIT", "30"))

# 空回复恢复语：agent 流正常收尾但无任何输出（qwen 偶发空内容）时补发的人设内
# 兜底文本——否则前端静默无感知（Rust 空回复不存历史、UI 无任何反馈，即"卡死"）
_RECOVERY_SENTENCE = "喵呜……主人抱歉，泠月喵刚才脑袋卡壳了，没有生成出回复，请主人再问一遍喵～ 🐾"

# ── 输入限额与并发闸（20260916 加固）──
# 输入全部来自 Rust 转发（只绑回环，见 docs/security-boundary.md），所以限额防的不是
# 陌生人，而是"前端出 bug / 被塞畸形请求 / 本机进程乱调"把 agent 拖垮：
#   · starlette **默认不限制 body 大小**，直接读进内存——畸形大包先吃满 3.7GB 机器；
#   · 超长字段会灌进 prompt，白烧 token，还可能撑爆 LLM 侧上下文；
#   · LLM 调用是最贵的资源（单次最长 180s），无闸时并发涌进来只会一起排队到超时。
MAX_BODY_BYTES = int(os.environ.get("AGENT_MAX_BODY_BYTES", str(12 * 1024 * 1024)))
MAX_MESSAGE_CHARS = 4000
MAX_HISTORY_ITEMS = 60
MAX_IMAGES = 6
MAX_IMAGE_CHARS = 1_600_000     # ≈1.17MB 二进制（前端压缩后单图 ≤1MB，留余量）
MAX_TEXT_FIELD_CHARS = 8000     # summary / executions
MAX_SHORT_FIELD_CHARS = 500     # current_url / page_title
# 确认令牌：base64url(json) + 签名，签名体里带**已实例化的全部参数**（标签名/标签
# 数组都在里面）——500 字在"建二级标签 + 长名字"时会被顶到。给足额度（令牌本身
# 只是 HMAC 材料，不进 prompt、不落库），形状校验交给 confirm.verify（fail-closed）。
MAX_CONFIRM_TOKEN_CHARS = 4000
MAX_CONCURRENT_STREAMS = int(os.environ.get("AGENT_MAX_CONCURRENT", "8"))
STREAM_QUEUE_WAIT = 3.0         # 秒；排队超过这个时间就如实 503，不让请求无声堆着

# 并发闸（每 worker 一个；uvicorn --workers 2 ⇒ 全局 2×MAX_CONCURRENT_STREAMS）。
# Python 3.10+ 起 asyncio.Semaphore() 不在构造时绑事件循环，模块级创建是安全的。
_stream_slots = asyncio.Semaphore(MAX_CONCURRENT_STREAMS)

# /review 的**独立**小闸（20260925 审计）：它是"外部文本 → 一次 LLM 裁决"，此前不占任何
# 闸 ⇒ 任何能连到 8010 的进程都能免费烧模型额度。刻意不共用对话那 8 个槽位：留言审核是
# 同步短任务（上限 25s），与访客对话抢槽位会让留言高峰把对话打成 503。
# 用 threading 版（`/review` 是 sync def，FastAPI 放线程池里跑，不占事件循环）。
REVIEW_CONCURRENCY = int(os.environ.get("AGENT_MAX_REVIEW", "4"))
REVIEW_QUEUE_WAIT = 3.0
_review_slots = threading.BoundedSemaphore(REVIEW_CONCURRENCY)


async def _try_acquire_slot() -> bool:
    """拿并发槽位；排队超过 STREAM_QUEUE_WAIT 秒返回 False（调用方回 503，别无声排队）。"""
    try:
        await asyncio.wait_for(_stream_slots.acquire(), timeout=STREAM_QUEUE_WAIT)
        return True
    except asyncio.TimeoutError:
        logger.warning("并发闸已满（%d 槽），排队 %.1fs 未拿到 → 503",
                       MAX_CONCURRENT_STREAMS, STREAM_QUEUE_WAIT)
        return False


def _release_slot() -> None:
    try:
        _stream_slots.release()
    except ValueError:      # 多还一次（理论上不会）：记日志别把收尾炸掉
        logger.warning("并发槽位重复释放（计数错乱）")

class HistoryItem(BaseModel):
    """一条历史消息。**结构化而不是 dict**（20260917 外部审计指出）：此前是
    `list[dict]`，`_build_messages` 直接 `h["role"]`/`h["content"]` 取键——畸形项
    会 KeyError → 500（`/chat/stream` 那条路还只是在释放并发槽后才炸）。
    role 限定 user/assistant（DB 里只有这两种，实测 `SELECT DISTINCT role`）。
    content **不设长度上限**：历史里的助手长回复（实测最长 3773 字符，公式推导类
    还会更长）截断会改变注入语义，且 12MB 的整体 body 上限已经兜住了总量。
    Rust 侧的 history 只发 role/content 两个字段，与此一一对应。"""
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str = Field(max_length=MAX_MESSAGE_CHARS)
    current_url: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    page_title: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    user_id: int = 0
    history: list[HistoryItem] = Field(default_factory=list, max_length=MAX_HISTORY_ITEMS)
    summary: str = Field(default="", max_length=MAX_TEXT_FIELD_CHARS)
    needs_summary: bool = False
    # 前端上报的页面特效实时状态（如 "sakura,rain" 或 ""），供 agent 感知真实开关状态
    current_effects: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    # 前端上报的夜间模式实时状态（"on"/"off"），供 agent 感知真实开关状态（与特效同理）
    current_darkmode: str = Field(default="", max_length=MAX_SHORT_FIELD_CHARS)
    # 多模态图片输入：前端压缩后的 dataURL 数组（20260828 单图 → 20260828s 多图，
    # 最多 6 张、每张 ≤1MB；qwen3.8-flash 原生支持图像）。兼容旧版单串（golden 直连）
    image: str | list[str] = ""
    # 跨轮执行记忆（20260904 C3）：本会话最近执行的 checker 验收回执渲染文本
    # （Rust 侧从 execution_log 读最近 8 条渲染成 "· 屏幕显示「…」" 式行）——
    # 下轮质疑"你刚才屏上写了什么"时据实回答，不重发不编造
    executions: str = Field(default="", max_length=MAX_TEXT_FIELD_CHARS)
    # 跨轮待办（20260923）：本会话"已提出、还没被确认"的写操作（Rust 侧从
    # pending_action 表读最新一条仍 pending 的行渲染成一行）。与 executions 同
    # 语义的系统事实注入——短应答/授权式轮次据此定"那件事"，而不是回历史里挑
    # 一句自然语言当目标（13:19 事故）。空串 = 没有待办。
    pending_action: str = Field(default="", max_length=MAX_TEXT_FIELD_CHARS)

    # ── 写操作确认（20260921）────────────────────────────────────────
    # conversation_id：确认令牌的绑定维度之一（令牌只在这个会话里有效）。
    # Rust 侧显式转发 body 白名单里的字段，缺了它 conv 恒为 None ⇒ 令牌验不过。
    conversation_id: int | None = None
    # confirm_token：**隐藏确认请求**的凭据（前端点了确认框上的「确定」）。
    # 这是唯一凭据，绑定 uid + 会话 + 10 分钟，服务端零状态（uvicorn 2 workers
    # ⇒ 内存 pending 表在另一个 worker 上不存在）。Rust 侧对带此字段的请求
    # **跳过用户消息入库**——所以它不会在历史里留下一条空用户消息。
    confirm_token: str = Field(default="", max_length=MAX_CONFIRM_TOKEN_CHARS)

    @field_validator("image")
    @classmethod
    def _check_images(cls, v):
        """图片限额：**条数**与**单张体积**。dataURL 是 base64，直接进 prompt，
        没有上限时一张几十 MB 的图能把请求体和上下文一起撑爆（20260916 加固）。
        形状（单串 / 数组）保持原样不动——兼容 golden 直连的旧单串写法。"""
        items = [v] if isinstance(v, str) else list(v)
        items = [x for x in items if x]
        if len(items) > MAX_IMAGES:
            raise ValueError(f"最多 {MAX_IMAGES} 张图片")
        for x in items:
            if len(x) > MAX_IMAGE_CHARS:
                raise ValueError(f"单张图片过大（{len(x)} > {MAX_IMAGE_CHARS} 字符）")
        return v


class ChatResponse(BaseModel):
    reply: str
    success: bool
    error: str | None = None
    new_summary: str | None = None
    # 跨轮执行记忆（20260904 C3，同步路径）：本次请求 checker 验收回执原始行
    # （{skill,tool,args,result,ts}）——Rust 同步响应手读 data["executions"] 落库
    executions: list = []


_agent = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    setup_logging()
    logger.info("Initialising LangChain agent ...")
    _agent = create_agent()
    logger.info("Agent ready")
    # 后台预热图谱查询的 embedding 客户端（首次 TLS 建连 ~3s，不预热的话
    # 重启后第一个查询会被上游超时掐掉→静默降级）。不阻塞启动，失败也不影响
    threading.Thread(target=wordgraph.warm, name="wordgraph-warm", daemon=True).start()
    # 后台预热 RAG 语料索引：一是首个检索不再吃冷启动延迟，二是 planner 的文档锚点
    # 要靠语料把《标题》解析成 id（20260920 方案①）。不阻塞启动，失败只降级。
    rag_search.warm_async()
    yield
    logger.info("Agent shutting down")


app = FastAPI(title="Saudade Blog Agent", version="1.0.0", lifespan=lifespan)


# ── 链路追踪（可观测最小集）──
# 全链路约定：X-Request-ID 由 Rust 透传（无则本中间件生成）；同一请求的所有日志
# （含线程池内 agent 图节点日志，经 _submit_with_context 显式传播 context）带同一
# tid。响应头回写 X-Request-ID，供调用方/Rust 把上下游日志关联起来。
_TRACE_ID_HEADER = "X-Request-ID"


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    trace_id = request.headers.get(_TRACE_ID_HEADER) or uuid.uuid4().hex[:12]
    set_trace_id(trace_id)
    # 注意：不在 call_next 后 reset——流式响应（/chat/stream）的生成器在
    # call_next 返回后才被消费，提前 reset 会让流式期间的日志 tid 变回 "-"。
    # contextvar 按任务隔离，每请求必覆盖式 set，无跨请求泄漏。
    response = await call_next(request)
    response.headers[_TRACE_ID_HEADER] = trace_id
    return response


# 请求体积上限：Content-Length 直接拦（在解析 body 之前，见上面常量区的注释）。
# ⚠️ 分块传输（无 Content-Length）不走这条——那条路只能靠字段级限额兜
# （见 ChatRequest 的 Field / _check_images），这一点如实写在这里不假装全覆盖。
@app.middleware("http")
async def body_limit_middleware(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        logger.warning("请求体过大：%s bytes > %d bytes", cl, MAX_BODY_BYTES)
        return JSONResponse({"error": "payload_too_large", "limit": MAX_BODY_BYTES},
                            status_code=413)
    return await call_next(request)


# ── 服务间身份断言（20260917，外部审计的"高"项）──
# 问题：agent 的 user_id 直接来自请求体，而 IoT 工具用它签用户 JWT（能操作那个人的
# 设备）。当前靠"只听回环"兜着——一旦 systemd 改 0.0.0.0 / nginx 误反代 / 本机进程
# 被攻破，就能伪造任意 user_id。
# 修法：Rust 用同一个 JWT_SECRET 签一条**短时效的身份断言**（aud=agent，60s）放在
# `X-Agent-Assertion` 头里，agent 验签通过后**用它覆盖请求体里的 user_id**。
# 这样边界从"回环"升级成"签名"——直连 agent 的人也伪造不出别人的身份。
# 滚动上线：`AGENT_REQUIRE_ASSERTION=0`（默认）时缺头只记 WARNING、行为不变；
# Rust 部署完再在 .env 打开它，避免"先重启 agent"把在途请求打成 401。
_ASSERTION_HEADER = "X-Agent-Assertion"
_ASSERTION_AUD = "agent"


def _verify_user_assertion(token: str) -> int | None:
    """验 Rust 签的身份断言，返回其中的用户 id；任何一步不成立返回 None。

    手写 HS256 校验（与 tools/base.py 的 `_sign_user_jwt` 同源）：只为一条内部断言
    引一个 JWT 依赖不值得，而 python 标准库就够（hmac + base64 + json）。
    """
    claims = _verify_assertion_claims(token)
    return claims["uid"] if claims else None


def _verify_assertion_claims(token: str) -> dict | None:
    """验签并返回断言的声明（uid + role）；任何一步不成立返回 None。

    role 是 20260920 加的（秘书类功能地基：agent 侧要知道"我代表的是谁、他能
    让我做什么"）。**Rust 还没部署带 role 的断言时这里是 None**，由调用方按
    "身份不明"处理（agent/authz.py：零权限 + shadow 只记不拦），不做任何默认授予。
    """
    try:
        from config.settings import settings
        secret = (settings.jwt_secret or "").encode()
        if not secret:
            return None
        h_b64, p_b64, sig_b64 = token.split(".")
        signing_input = f"{h_b64}.{p_b64}".encode()
        expected = hmac.new(secret, signing_input, hashlib.sha256).digest()
        got = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        if not hmac.compare_digest(expected, got):
            return None
        payload = json.loads(base64.urlsafe_b64decode(p_b64 + "=" * (-len(p_b64) % 4)))
        if payload.get("aud") != _ASSERTION_AUD:
            return None
        if float(payload.get("exp") or 0) < time.time():
            return None
        uid = int(payload.get("sub") or 0)
        if uid <= 0:
            return None
        role = payload.get("role")
        return {"uid": uid, "role": str(role) if role else None}
    except Exception:
        return None


def _resolve_principal(request: Request, body_uid: int) -> Principal:
    """以签名为准解析调用者身份（uid + role），返回显式 principal（见上面注释）。

    秘书类功能的地基：把"谁在说话、他能让我做什么"从到达图之前就固定下来。
    role 只认签名里的（Rust 从 DB 查、不信登录 token 里的旧角色，与
    middleware.rs 同一条纪律）——**回退信任 body 的分支里 role 恒为 None**，
    绝不因为"读不到角色"就默认授予任何权限。
    """
    from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
    from config.settings import settings
    token = request.headers.get(_ASSERTION_HEADER) or ""
    claims = _verify_assertion_claims(token) if token else None
    if claims:
        if claims["uid"] != body_uid:
            logger.warning("[auth] 断言覆盖 body.user_id：%s → %s（body 不可信）",
                           body_uid, claims["uid"])
        return Principal(uid=claims["uid"], role=claims["role"], source=SOURCE_ASSERTION)
    if settings.agent_require_assertion:
        logger.warning("[auth] 缺少/无效身份断言（%s）→ 401", "无头" if not token else "验签失败")
        raise HTTPException(401, "缺少有效的服务间身份断言")
    if token:
        logger.warning("[auth] 身份断言验签失败，回退信任 body.user_id=%s", body_uid)
    return Principal(uid=body_uid, role=None, source=SOURCE_BODY)


def _resolve_user_id(request: Request, body_uid: int) -> int:
    """uid 版（保留给只关心 uid 的调用方/测试）；语义与 _resolve_principal 一致。"""
    return _resolve_principal(request, body_uid).uid


# 台账记录的"现时"有效期（分钟，20260925）：超过它的记录只能说明"当时是那样"，
# 不能拿来回答"现在怎样"。10 分钟 = 服务器状态这类指标够用的新鲜度（再短会让
# "再问一句现状"每次都多跑一次查询，再长就把三小时前的读数当成现场）。
# 只管**现时状态类询问**；取值指代（规则 6b，"第二条写的什么"）问的是历史事实，
# 照抄摘要永远是对的，不受此限。
EXEC_STALE_MINUTES = 10

# 台账行行首的时间戳（Rust 侧渲染时补的 `MM-DD HH:MM`，见 chat.rs render_exec_row）。
# 两侧的排除项都是为了**只认自己那一行的时间**：前面不许接数字或连字符（挡掉完整日期
# `2026-09-05 14:06:57` 里的后两段）、后面不许接数字或冒号（挡掉带秒的时间）。看不懂
# 的一律不标——标注宁缺勿错，标错了是凭空给模型一个假事实。
_EXEC_TS_RE = re.compile(r"(?<![\d-])(\d{2})-(\d{2}) (\d{2}):(\d{2})(?![\d:])")


def _age_phrase(minutes: int) -> str:
    if minutes <= 0:
        return "刚刚"
    if minutes < 60:
        return f"{minutes} 分钟前"
    if minutes < 60 * 24:
        h, m = divmod(minutes, 60)
        return f"{h} 小时前" if not m else f"{h} 小时 {m} 分前"
    return f"{minutes // (60 * 24)} 天前"


def annotate_exec_ages(exec_txt: str, now: "datetime | None" = None) -> str:
    """给台账每行的行首时间补一个**系统算出来的相对年龄**：`09-25 00:42（3 小时前·已过期）`。

    动机（20260925 生产实证，trace 20260925T035331）：访客问"现在服务器怎么了"，
    台账里最近一条是 3 小时 11 分前的服务器状态，planner 零工具照抄摘要、narrator
    又写成"刚才查到的"——数据过期 + 措辞不实，两头都错。年龄是**事实**（系统算的），
    要不要据此重查是**决策**（planner 的规则 6c）——所以这里只标注、不拦截。

    行首时间戳由 Rust 渲染（`MM-DD HH:MM`，只有月日没有年）：跨年时"12-31 23:50"
    在 1 月 1 日按当年解析会变成"十一个月后的未来"，故超前 6 小时以上一律退一年。
    解析不了的时间戳（脏行/别的格式）**原样留着**——不猜、也不因为看不懂就标"刚刚"。
    """
    now = now or datetime.now()

    def _rep(m: "re.Match[str]") -> str:
        try:
            t = datetime(now.year, int(m.group(1)), int(m.group(2)),
                         int(m.group(3)), int(m.group(4)))
        except ValueError:
            return m.group(0)
        if (t - now).total_seconds() > 6 * 3600:
            try:
                t = t.replace(year=t.year - 1)
            except ValueError:
                return m.group(0)
        minutes = int((now - t).total_seconds() // 60)
        mark = "·已过期" if minutes > EXEC_STALE_MINUTES else ""
        return f"{m.group(0)}（{_age_phrase(minutes)}{mark}）"

    return _EXEC_TS_RE.sub(_rep, exec_txt)


def _ledger_block(req: ChatRequest, confirmed: bool = False) -> str:
    """确认与执行事实（系统台账）——两块合一注入（20260924）。

    此前是两条独立的 ctx_parts（`recent_executions` / `pending_action`），各自的定性
    括号都是事后补的补丁。合一之后"真做过的"与"还没做的"在同一个块里**互斥对照**，
    模型没有机会把一半读成另一半；两块都空时整块不注入（不占上下文）。

    `confirmed`（本轮是"点确定"那一跳，令牌已验签）：待办那一半**正被本轮执行**，
    不能再照旧说"等主人点头、尚未执行"——那句话是 20260923 洞⑥ 那族假话的样板文本，
    原样摆在上下文里等于给 narrator 递台词（20260922 两跑的"把办好的说成待确认"
    正是抄了它）。此时改写成执行中的定性，其余原样。
    """
    head = ("确认与执行事实（系统台账——泠月自己的动作记录，不是访客的浏览痕迹；"
            "两半互斥：『已执行』是真做过、系统验收过的，『待主人点头』是**还没做**的。"
            "这两块都是系统事实：不许否认它们存在，也不许把一半说成另一半。"
            "『已执行』行行首时间后面那个「（…前）」是系统按当前时间算好的年龄，"
            f"带「·已过期」的=超过 {EXEC_STALE_MINUTES} 分钟——只能说明「当时是那样」，"
            "不要拿它回答「现在怎样」，见规划纪律 6c；叙述里也不许把这种记录说成「刚才」）")
    # 年龄标注放在 [:1500] 截断**之后**：截断的额度留给台账原文本身，
    # 标注是系统补的事实，不该挤掉主人的执行记录。
    exec_txt = annotate_exec_ages(req.executions.strip()[:1500]) or "（本会话暂无记录）"
    if confirmed:
        # 待办那一半**整行改写、不回引 Rust 渲染的行**：那行尾巴写死了
        # "状态 awaiting（等主人点头，尚未执行）"，正是 20260923 洞⑥ 那族假话的
        # 样板文本（20260922 两跑的"把办好的说成待确认"就是抄了它）——本轮它已经被
        # 主人的那一下确定消费掉，再摆出来等于给 narrator 递台词。目标/参数不丢：
        # 本轮的执行回执（工具帧）本来就有，叙述以回执为准。
        return (head + "\n· 已执行（系统验收过）: " + exec_txt
                + "\n· 待办: 主人**刚刚点了「确定」**，本轮正在执行它——**不是**尚未执行，"
                  "也不是没生成过确认；办到哪一步一律以本轮执行回执为准")
    return (head + "\n· 已执行（系统验收过）: " + exec_txt
            + "\n· 待主人点头（还没做）: "
            + (req.pending_action.strip()[:800] or "（本会话暂无记录）"))


def _ledger_for_graph(req: ChatRequest, confirmed: bool = False) -> dict:
    """给图内判据（洞⑦ 台账否认）的台账事实——**与 `_ledger_block` 注入的同源**。

    判据要看的就是"系统给模型看过什么"：注入说待办还没办，判据就得认这一半非空；
    注入改写成了"正在执行"（confirmed），判据就不能再把否认当假话（那时候说
    "系统里已经没有待确认的了"是真话）。两个来源各算各的必然对不上。
    """
    return {"executions": req.executions.strip(),
            "pending": "" if confirmed else req.pending_action.strip()}


def _ctx_field(s, limit: int = 500) -> str:
    """系统上下文里的**客户端字段**：剥掉会破坏 `key=value; key=value` 结构的字符
    （20260925 审计）。

    这些值由**浏览器**给（`current_url`/`page_title` 直接来自页面 JS，`current_effects`/
    `current_darkmode` 来自访客本机的 localStorage），Rust 原样转发，而它们拼进的是
    `[System: …]` 那一段——**可信通道里不许有不可信文本**。不清洗的后果是：访客能在自己
    的字段里塞 `;`、`[System:` 之类，让系统上下文里长出第二段（例如伪造
    `current_darkmode=on` 覆盖真实状态）。

    今天它只是"自注入"（效果不比直接在对话框里打字更多，`page_title` 的来源
    `document.title` 全前端无人改写，第三方内容进不来）——但哪天 `page_title` 改成跟着
    文章标题/留言标题走（很容易发生），同一处代码会立刻从"低"变成"跨用户注入"。
    """
    return _CTX_UNSAFE_RE.sub(" ", str(s or ""))[:limit]


_CTX_UNSAFE_RE = re.compile(r"[\r\n\[\];=]")


def _build_messages(req: ChatRequest, confirm_grant: dict | None = None) -> list:
    """Build the message list from the request (sync, no blocking).

    `confirm_grant`：已验签的确认令牌 payload（见 /chat/stream）。只用于**台账定性**
    （这一轮是"用户刚点了确定"那一跳，见 `_ledger_block`）——授权判据不看它，
    令牌验签的唯一权威在调用方。非流式 `/chat`（golden/评测直连）不传 = 普通轮。
    """
    messages = []
    ctx_parts = [f"user_id={req.user_id}, page={_ctx_field(req.current_url)}, "
                 f"title={_ctx_field(req.page_title)}"]
    ctx_parts.append(f"current_effects={_ctx_field(req.current_effects, 200) or 'none'}")
    ctx_parts.append(f"current_darkmode={_ctx_field(req.current_darkmode, 20) or 'off'}")
    # 20260902 时间锚（幻觉事故 13:34 实证）：会话断点续接/问候语场景模型会锚定
    # 历史里的旧时间戳编造"现在"（05:29 会话 13:34 续接 → 编"现在 05:34"）。
    # 当前时刻必须作为系统事实注入（与 current_effects/darkmode 同语义，格式与
    # get_current_time 工具一致），模型不得自行推算；executor 规则同源约束。
    _now = datetime.now()
    _weekdays = ('星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日')
    ctx_parts.append(f"current_time={_now.strftime(f'%Y年%m月%d日 {_weekdays[_now.weekday()]} %H:%M')}")
    if req.summary:
        ctx_parts.append(f"conversation_summary: {req.summary}")
    if req.executions or req.pending_action:
        ctx_parts.append(_ledger_block(req, confirmed=bool(confirm_grant)))
    # 20260905 重复提问注入（18:17:36/18:18:52 实证：同句重发时 narrator 逐字
    # 复读上轮回复，两条一字不差的回复并排出现在会话里——qwen 对同句重问的
    # 最优策略判断是原样复读，prompt 软约束压不住，需确定性旁路）。
    # 检测：当前消息与历史最近一条 user 消息字面相同（去首尾空白）。宁精确勿
    # 误伤——仅原句重发才点破，近似新问法不触发（不打断正常追问）。
    if req.message.strip():
        prev_user = next((h.content for h in reversed(req.history)
                          if h.role == "user"), None)
        if isinstance(prev_user, str) and req.message.strip() == prev_user.strip():
            ctx_parts.append(
                "repeat_ask_note: 访客原句重发了刚才的问题——若上轮已答过：先点破"
                "（'这个问题你刚才问过啦'），再压缩成两三句要点重述（不复读原文全文、"
                "不重复举例/收尾句），并追问一句新意图；本轮工具查证带回新事实才按新"
                "事实完整叙述")
    ctx = f"[System: {'; '.join(ctx_parts)}]"
    messages.append(HumanMessage(content=ctx))

    # 消费 Rust 转发的全部 20 条历史（20260828 对齐：此前 Rust 传 20、这里取 12，
    # 8 条白传且两个魔数散落两处易失同步；Rust 侧已排除当前消息，history 是纯历史）
    # 孤儿 user 裁剪（20260901）：中断/并发窗口的轮次只入库 user 无 assistant 回复
    # （该轮回复未完成即断流），注入后模型把孤儿当待答问题（trace 实证：历史遗留
    # "谈谈你对穹妹的看法"未完成，模型整篇回复穹妹）。正常轮次严格成对
    # （user→assistant），user 后非 assistant 即孤儿，整条跳过不注入。
    hist = req.history[-20:]
    for i, h in enumerate(hist):
        if h.role == "user":
            if i + 1 >= len(hist) or hist[i + 1].role != "assistant":
                continue  # 孤儿 user（该轮回复未入库），不注入
            messages.append(HumanMessage(content=h.content))
        else:
            # 恢复 assistant 角色（曾全部包成 HumanMessage + [assistant]: 前缀——
            # 模型会把历史当"用户说的"，多轮上下文质量打折；角色语义对齐后
            # 模型对"谁说过什么"的区分不再依赖前缀文本）
            messages.append(AIMessage(content=h.content))

    # 多模态（20260828 单图 → 20260828s 多图）：图片 + 文字转 OpenAI content 数组
    # （qwen 实测支持，100x100 红图识别正确）。多图循环拼 content 数组，每张一个
    # image_url 块（视觉 token 注入消息序列尾部，前缀零污染，缓存命中不受影响）。
    # 图片本体不进历史（Rust 侧落库加 "[图片]"/"[图片×N]" 标记）。
    if req.image:
        content: list = []
        if isinstance(req.image, str):
            imgs = [req.image]
        else:
            imgs = req.image
        for url in imgs:
            content.append({"type": "image_url", "image_url": {"url": url}})
        content.append({"type": "text", "text": f"[当前问题]: {req.message or '请描述这些图片'}"})
        messages.append(HumanMessage(content=content))
    else:
        # 20260901：当前消息加 [当前问题] 锚点——历史 user 消息与当前消息都是裸
        # HumanMessage，多窗口并发时当前请求的历史可能含另一窗口的孤儿用户消息
        # （该窗口回复未完成入库），两条 user 相邻无 assistant 回复隔离时模型把
        # 旧问题当当前问题回答（trace 实证：输入"椎名真白"整篇回复穹妹）。
        # 前端 sending/idle 跨窗同步只能缩小竞态窗口不能消除（收尾瞬间仍可发送），
        # 锚点让模型明确最后一条才是当前问题，历史只是背景。
        messages.append(HumanMessage(content=f"[当前问题]: {req.message}"))
    return messages


def _run_agent_sync(messages: list, thread_id: str, user_id: int = 0,
                    principal: Principal | None = None,
                    ledger: dict | None = None) -> tuple[str, str, list]:
    """Run agent synchronously in a thread. Returns (reply, nav_line, exec_rows)."""
    # user_id 注入 configurable：设备类工具（list_devices/device_oled_display）
    # 经 RunnableConfig 读取并以用户身份签发 JWT 调用 device-service
    # principal 一并注入：execute 的权限判据读它（agent/authz.py；缺省 = 身份不明）
    # ledger 一并注入：gate 的台账否认判据（洞⑦）读它，见 _ledger_for_graph
    # recursion_limit 覆盖默认 9999（等效无界）：幻觉重试循环有界
    config = {"configurable": {"thread_id": thread_id, "user_id": user_id,
                               "principal": principal or Principal(uid=user_id)},
              "recursion_limit": RECURSION_LIMIT}
    full_reply = ""
    nav_line = ""
    exec_rows: list = []  # 跨轮执行记忆（20260904 C3）：checker 验收回执，累计语义末批即全量
    for mode, data in _agent.stream(
        graph_input(messages, ledger=ledger or {}),
        config,
        stream_mode=["messages", "updates"],
    ):
        # 手写图里有多个 LLM 节点（planner 产出计划文本、model 产出回复），
        # 只有 model 节点的 AIMessageChunk 是给访客看的回复——其余按 node 过滤掉，
        # 否则计划会漏进对话（create_agent 时代只有一个 model 节点，无需过滤）
        if mode == "updates":
            ex_upd = data.get("execute")
            if ex_upd and ex_upd.get("receipts"):
                exec_rows = ex_upd["receipts"]
            continue
        chunk, meta = data
        if (isinstance(chunk, SystemMessage) and chunk.content
                and str(chunk.content).startswith("[Fallback 决定]")):
            # gate fallback（20260903，validate→fallback 无重考轮）：gate 是终节点，
            # 其后无新一轮 model 文本——最终回复直接替换为 fallback 正文（去前缀）。
            # nav_line 不清：工具帧是系统真实执行的命令（与叙述文本解耦），照常下发。
            _fb = str(chunk.content).split(":", 1)
            full_reply = _fb[1].strip() if len(_fb) > 1 else ""
        elif isinstance(chunk, AIMessageChunk) and chunk.content and meta.get("langgraph_node") == "model":
            full_reply += str(chunk.content)
        elif isinstance(chunk, ToolMessage) and chunk.content:
            text = str(chunk.content)
            if text.startswith("NAVIGATE:") or text.startswith("AUTO_NAVIGATE:"):
                nav_line = text
            elif text.startswith("EFFECT:") or text.startswith("DARKMODE:"):
                nav_line = text
    reply = full_reply.strip()
    # 不再在这里拼入 nav/effect 命令行——由调用方在摘要剥离之后追加，
    # 避免回复末尾的 SUMMARY: 截断把 EFFECT:/NAVIGATE: 命令一起吞掉
    return reply, nav_line, exec_rows


def _summarize_dialogue(user_msg: str, history: list[HistoryItem], old_summary: str) -> str:
    """needs_summary 轮的独立对话摘要（与 agent 回复解耦，随图并行执行）。

    实现已收进 `agent/summarizer.py`（20260920）：那里有不信任输入的围栏、输出清洗
    与 fail-empty 取向，以及 tests/test_side_tasks.py 的回归锁。这里只留一句转发——
    历史背景（为什么不用"回复末尾顺带输出 SUMMARY"）见该模块头注。
    """
    return summarize(user_msg, history, old_summary)


# ---------------------------------------------------------------------------
# /chat  — 非流式（供 Rust 后端调用）
# ---------------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")
    principal = _resolve_principal(request, req.user_id)   # 身份以签名为准（见 _resolve_principal）
    req.user_id = principal.uid

    messages = _build_messages(req)
    # 每请求独立线程：LangGraph 的 MemorySaver 线程状态会随对话无限累积，
    # 长对话（教程连载等）会让输入上下文与 worker 内存持续膨胀直至截断/被杀。
    # 对话连续性由请求体中的 DB 历史(最近20条) + chat_summary 摘要承担，无需线程累积。
    thread_id = f"user_{req.user_id}_{uuid.uuid4().hex[:8]}"

    try:
        loop = asyncio.get_event_loop()
        # 摘要独立化（needs_summary 轮）：与 agent 图并行做后端总结——输入是原始
        # 历史数据而非模型回复，杜绝"回复耦合生成"时代的推断/编造（曾出现摘要
        # 编造"助手调用工具"污染记忆）。生成失败返回空 → 不入库，旧摘要保留。
        summary_task = None
        if req.needs_summary:
            summary_task = _submit_with_context(
                loop, _summarize_dialogue, req.message, req.history, req.summary
            )
        reply, nav_line, exec_rows = await _submit_with_context(
            loop, _run_agent_sync, messages, thread_id, req.user_id, principal,
            _ledger_for_graph(req))
        new_summary = None
        if summary_task is not None:
            new_summary = (await summary_task).strip() or None

        # 空回复兜底：qwen 偶发空内容 → 下发人设内恢复语，避免前端静默无感知
        if not reply.strip():
            logger.warning("Agent returned empty reply for message=%r", req.message[:60])
        final_reply = reply.strip() or _RECOVERY_SENTENCE

        # 摘要剥离之后再把导航/特效命令行追加回去，确保命令不被 SUMMARY 截断吞掉
        final_nav = nav_line
        if final_nav and not final_reply.startswith("NAVIGATE:") and not final_reply.startswith("AUTO_NAVIGATE:"):
            if final_nav.startswith("EFFECT:"):
                final_reply = final_reply + "\n" + final_nav
            else:
                final_reply = final_nav + "\n" + final_reply

        return ChatResponse(reply=final_reply, success=True, new_summary=new_summary,
                            executions=exec_rows)
    except Exception as e:
        logger.exception("Agent invocation failed")
        return ChatResponse(reply="", success=False, error=str(e))


# ---------------------------------------------------------------------------
# /chat/stream — SSE 流式（供前端 Live2D 调用）
# ---------------------------------------------------------------------------
# ── 过程行文本人话化（20260905 issue5：执行过程显示与真实执行对齐）──
# 旧实现病灶：
#   ① 计划行直接贴 plan 机器契约原文（SKILL=/PARAMS= JSON），[:60] 截断成残句，
#     用户看到的是半截内部格式而非动作描述；
#   ② 「✅ 工具执行完成」由 ToolMessage 分支固定模板发出，不查 checker 验收——
#     受阻执行（空结果/错误帧，verdict BLOCK）同样显示"完成"；且任何完成帧都
#     不带实际内容（跳去哪/显示什么/检索什么全无），与用户可见的事实对不上；
#   ③ 命令类完成帧（"🛠 调用工具：页面跳转 navigate_to"）没有目标细节。
# 修复：计划行解析 TOOLS spec 成中文动作预告；完成/受阻行改由 execute update
#   的 receipts（checker PASS 回执）与 blocked（受阻清单）驱动——验收通过才发
#   ✅，受阻发 ✗；预告与完成共用 _tool_action_text 渲染，展示前后一致。
#   注：gate 打回/通过的「✗ 质检打回」「✓ 质检通过」行不受影响（见 gate 分支）。

_EFFECT_CN = {"sakura": "樱花", "rain": "大雨", "snow": "雪花"}

# 无参只读点名工具 → 中文动作（planner 直接点名展开，见 skills._EXPLICIT_TOOLS）
_NOARG_VERB = {
    "list_guestbook": "查看留言板",
    "list_talks": "查看说说",
    "list_notes": "查看文章列表",
    "list_devices": "查看设备列表",
    "get_announcements": "查看公告",
    "get_current_time": "查看当前时间",
    # 20260913 新入白名单的站点信息类（此前 planner 点不到，无中文动作词；
    # 缺省会落到"执行 get_social_links"的内部格式）
    "get_blog_info": "查看博客信息",
    "get_social_links": "查看社交链接",
    "get_site_map": "查看站点结构",
    "get_top_notes": "查看置顶文章",
    "list_categories": "查看分类",
    "list_tags": "查看标签",
    # 管理助手报表（20260921）：**不在** _EXPLICIT_TOOLS 里（planner 点不到名，
    # 只由 ops_report / moderation_report / user_report 三个技能模板展开），
    # 但过程行渲染走的是同一张表——缺了就显示"执行 get_server_status"。
    "get_server_status": "查看服务器状态",
    "get_service_health": "查看服务健康",
    "get_moderation_status": "查看审核状况",
    "get_user_stats": "查看用户统计",
    # 后台文章列表（20260921 第二轮，**读**）：无参，同上面四个报表工具——
    # planner 点不到名（不在 _EXPLICIT_TOOLS），由 admin_notes 技能模板展开。
    "list_admin_notes": "查看后台文章列表",
    # 用户自己的数据（20260923）：planner 直接点名（在 _EXPLICIT_TOOLS 里）
    "list_my_favorites": "查看我的收藏",
    "get_unread_summary": "查看未读汇总",
    "list_notifications": "查看站内通知",
    # 自己的信箱（20260923 批 8）。措辞与 Rust `render_exec_row` 的同名臂同源
    # ——"查看站内信"（不是"查看留言"：那是 list_guestbook 的公开留言板）。
    "list_my_messages": "查看站内信",
}

_REASON_CN = {"unknown_tool": "未知工具", "args_parse": "参数解析失败",
              "empty_result": "结果为空", "error_frame": "执行出错",
              "cmd_shape": "返回格式异常",
              # 参数引用失败（agent/refs.py 的原因码，20260919）
              "ref_unknown_tool": "引用的工具尚未执行",
              "ref_unparsed": "引用的返回不是结构化数据",
              "ref_index_range": "引用的序号越界",
              "ref_path_missing": "引用的字段不存在",
              "ref_not_scalar": "引用取到的不是单个值",
              # 写操作目标无据（graph.execute 的目标校验，20260921 第二轮）
              "unknown_target": "目标未经确认",
              # 上游服务不可用（_check_spec 的 kind=unavailable 分支，20260916 就有；
              # 20260921 补中文——此前会原样打出英文原因码，用户看到 "unavailable"）
              "unavailable": "服务不可用",
              # 目标不存在（20260923 三轮，kind=not_found）：与上一条分开。此前
              # planner 拿错 id（把「共 3 条」的 3 当 id）时，工具报的是 unavailable
              # ⇒ 过程行显示「服务不可用」，用户读成"系统挂了"，而真问题是"你要标的
              # 那条不存在"（trace `20260923T130033_9` 用户原话："显示服务不可用"）
              "target_not_found": "目标不存在",
              # 后台规则拒绝（账号冻结/解冻，20260926）：后端那三条策略（不能冻自己 /
              # 不能冻超管 / 管理员之间不可互冻）与 agent 侧预检都走这个码。
              # 漏了这行不会静默——它会原样打出英文码 `policy_refused` 给访客看。
              # 与"服务不可用"刻意分开：那个的下一步是稍后重试，这个重试一万次也一样
              # （要换的是目标或身份，见 graph 规则里那条"不要改参重试"）。
              "policy_refused": "后台规则拒绝"}


# 参数引用（agent/refs.py 的 $<工具>[<序号>].<字段>）在过程行里的可读来源名。
# 预告帧在 execute **之前**发，此刻引用还没解析，参数里就是 `$search_notes[0].noteKey`
# 这种内部语法——直接拼进去访客会看到 `读取文章 $search_notes[0].noteKe`。故按来源
# 工具译成"上一步<来源>的第 N 条"，与解析后的完成帧（读的是回执里的实际值）
# 语义一致：预告说"要读上一步检索的第 1 条"，完成说"读取文章 12"。
_REF_SOURCE_CN = {
    "search_notes": "检索结果", "rag_search": "检索结果", "list_notes": "文章列表",
    "list_talks": "说说列表", "list_guestbook": "留言列表",
    # 后台写轮最常见的引用源（20260921 第二轮）：`$list_admin_notes[0].noteKey`
    # 是"把《X》设为私密"的标准走法（先读列表拿 id 再写），缺了它就往过程行里
    # 打内部工具名。
    "list_admin_notes": "后台文章列表",
    # 用户自己的数据（20260923 批 6/7）：`$list_my_favorites[0].noteId` 是"取消收藏
    # 那一篇"的标准走法（先读自己的收藏夹拿 id 再撤），`$list_notifications[0].id`
    # 是"把那条公告标记已读"的走法。缺了这两个来源名，过程行会打出内部工具名。
    "list_my_favorites": "收藏列表",
    "list_notifications": "通知列表",
}


def _ref_phrase(value: str) -> str:
    """引用字面量 → 过程行可读短语（非引用或形态不认识 → 泛称，绝不打印原语法）。"""
    m = re.match(r"^\$([a-z_][a-z0-9_]*)\[(\d+)\]", str(value or "").strip())
    if not m:
        return "上一步返回"
    src = _REF_SOURCE_CN.get(m.group(1), f"{m.group(1)} 的返回")
    return f"上一步{src}的第 {int(m.group(2)) + 1} 条"


def _leaf(value, normalize=None, word=None) -> str:
    """写工具的参数值 → 过程行可读短语（20260921 第二轮）。

    三个来源各自成序，缺一不可：引用走 `_ref_phrase`（**绝不能打印 `$…` 原语法**）、
    认识的取值走中文词、其余按原值截断——写工具的预告帧与完成帧都经这里，
    两帧必须给主人看到**同一句话**（预告说"设为私密"、完成说"设为 private"会让人
    以为改了两次）。
    """
    s = str(value if value is not None else "").strip()
    if not s:
        return ""
    if s.startswith("$"):
        return _ref_phrase(s)
    if normalize is not None:
        got = normalize(s)
        if got is not None:
            return word[got] if word else str(got)
    return s[:12]


def _names_phrase(value) -> str:
    """标签名/ id 列表 → 「A、B、C」。"""
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    out = [_leaf(v) for v in items[:3]]
    out = [x for x in out if x]
    if len(items) > 3:
        out.append(f"等 {len(items)} 个")
    return "「" + "、".join(out) + "」" if out else "（空）"


def _tool_action_text(name: str, args: dict | None) -> str:
    """TOOLS spec 参数 → 中文动作正文（预告/回执完成帧共用，前后一致）。

    参数值截断防长文本撑爆过程行；navigate 路径经 NAV_MAP 反查中文别名
    （反查失败展示路径本身——路径是 execute 实际下发的真实值，不硬凑）。
    引用形态的参数（$tool[0].field）译成来源短语，不打印内部语法。
    """
    a = args or {}
    if name == "create_tag":
        # 一级/二级只差一个父 id；标题为空（planner 漏参）时也要给出一行像样的中文
        title = _leaf(a.get("title"))
        pid = _leaf(a.get("parent_id"))
        # 颜色（20260921）：点名了才显示——过程行是"这件事长什么样"的预告，
        # 参数里带色就说明用户点了色（没点名时 color 根本不进 args）
        hexval = A.match_tag_color(a.get("color")) if a.get("color") else None
        color = f"，颜色 {A.describe_color(hexval)}" if hexval else ""
        if not title:
            return "新建标签"
        return (f"新建二级标签「{title}」（父标签 id {pid}）{color}" if pid
                else f"新建一级标签「{title}」{color}")
    if name == "set_article_status":
        aid = _leaf(a.get("article_id"))
        bits = [_leaf(a.get("status"), A.normalize_status, A.STATUS_CN),
                _leaf(a.get("is_top"), A.normalize_top,
                      {1: "置顶", 0: "取消置顶"})]
        head = f"修改文章 {aid}" if aid else "修改文章状态"
        bits = [b for b in bits if b]
        return f"{head}：{'、'.join(bits)}" if bits else head
    if name == "set_article_tags":
        aid = _leaf(a.get("article_id"))
        head = f"修改文章 {aid} 的标签" if aid else "修改文章标签"
        acts = []
        if a.get("replace") is not None:
            # replace=[] 是有语义的（清空标签），"改成（空）"读起来像出错，直说清空
            acts.append("清空全部标签" if not a.get("replace")
                        else "改成 " + _names_phrase(a.get("replace")))
        if a.get("add"):
            acts.append("加上 " + _names_phrase(a.get("add")))
        if a.get("remove"):
            acts.append("去掉 " + _names_phrase(a.get("remove")))
        return f"{head}：{'、'.join(acts)}" if acts else head
    if name == "navigate_to":
        path = str(a.get("path") or "").strip()
        if path:
            label = next((k for k, v in NAV_MAP.items() if v == path), path)
            return f"页面跳转「{label[:20]}」"
        return "页面跳转"
    if name == "toggle_dark_mode":
        on = str(a.get("mode") or a.get("action") or "").lower() in ("on", "开", "true")
        return "开启夜间模式" if on else "关闭夜间模式"
    if name == "toggle_effect":
        eff_raw = str(a.get("effect") or "")
        eff = _EFFECT_CN.get(eff_raw, eff_raw or "页面")
        on = str(a.get("action") or "").lower() in ("on", "开", "true")
        return f"{'开启' if on else '关闭'}{eff}特效"
    if name == "device_oled_display":
        text = str(a.get("text") or "").strip()
        return f"屏幕显示「{text[:24]}」" if text else "屏幕显示"
    if name == "rag_search":
        q = str(a.get("query") or "").strip()
        return f"站内检索「{q[:24]}」" if q else "站内检索"
    if name == "search_notes":
        k = str(a.get("keyword") or "").strip()
        return f"检索文章「{k[:24]}」" if k else "检索文章"
    if name == "get_moderation_status":
        # 聚焦某一类时把"看的是哪一类"写进过程行（20260922）：只写「查看审核状况」
        # 会让"主人问被驳回的、agent 却在看全部"这种偏差在过程行里看不出来。
        focus = {"ai_passed": "AI 直接通过的", "ai_rejected": "被 AI 驳回的",
                 "pending": "等人复批的"}.get(str(a.get("status") or "").strip())
        return f"查看审核状况（只看{focus}）" if focus else "查看审核状况"
    if name == "get_article_detail":
        aid = str(a.get("article_id") or "").strip()
        if aid.startswith("$"):
            return f"读取文章（{_ref_phrase(aid)}）"
        return f"读取文章 {aid[:12]}" if aid else "读取文章"
    if name in ("update_tag", "delete_tag"):
        # 第四轮（20260921）漏了这一处：四个写工具的过程行原样打出 `执行 update_tag`
        # 这种内部工具名（主人看到的是英文工具名，而不是"要做什么"）。20260922 补上。
        title = _leaf(a.get("name"))
        if not title:
            return "修改标签" if name == "update_tag" else "删除标签"
        if name == "delete_tag":
            return f"删除标签「{title}」"
        acts = []
        if str(a.get("new_title") or "").strip():
            acts.append(f"改名为「{_leaf(a.get('new_title'))}」")
        hexval = A.match_tag_color(a.get("color")) if a.get("color") else None
        if hexval:
            acts.append(f"颜色→{A.describe_color(hexval)}")
        if str(a.get("parent_tag") or "").strip():
            acts.append(f"移到「{_leaf(a.get('parent_tag'))}」下面")
        elif str(a.get("to_level") or "").strip() == "one":
            acts.append("改成一级标签")
        elif str(a.get("to_level") or "").strip() == "two":
            acts.append("改成二级标签")
        head = f"修改标签「{title}」"
        return f"{head}：{'、'.join(acts)}" if acts else head
    if name in ("create_category", "update_category", "delete_category"):
        title = _leaf(a.get("new_title") or a.get("title") or a.get("name"))
        if name == "create_category":
            return f"新建分类「{title}」" if title else "新建分类"
        if name == "delete_category":
            return f"删除分类「{title}」" if title else "删除分类"
        if not title:
            return "修改分类"
        acts = []
        if str(a.get("new_title") or "").strip():
            acts.append(f"改名为「{_leaf(a.get('new_title'))}」")
        for key, cn in (("path_name", "路径"), ("introduce", "简介"),
                        ("icon", "图标"), ("color", "颜色")):
            if str(a.get(key) or "").strip():
                acts.append(f"{cn}→{_leaf(a.get(key))}")
        head = f"修改分类「{title}」"
        return f"{head}：{'、'.join(acts)}" if acts else head
    if name in ("create_announcement", "update_announcement", "delete_announcement"):
        # 公告（20260922 第五轮）：过程行**只报标题，不打印正文**——正文是主人要
        # 对全体访客说的话，过程行只是一行"在做什么"的预告，预览在确认框里。
        title = _leaf(a.get("title"))
        if name == "create_announcement":
            return f"发布公告「{title}」" if title else "发布公告"
        if name == "delete_announcement":
            return f"删除公告「{title}」" if title else "删除公告"
        acts = []
        if str(a.get("new_title") or "").strip():
            acts.append(f"改名为「{_leaf(a.get('new_title'))}」")
        if str(a.get("content") or "").strip():
            acts.append("正文更新")
        head = f"修改公告「{title}」" if title else "修改公告"
        return f"{head}：{'、'.join(acts)}" if acts else head
    if name in ("audit_board_comment", "delete_board_comment"):
        # 河灯留言（20260922 第六轮）：留言没有标题，**正文片段就是它唯一的身份**
        # ⇒ 过程行报片段（与"报标题不报正文"的公告同一条取向：只报认得出是哪一条的
        # 那一小截，完整正文留给确认框）。12 字截断沿用 _leaf 的统一口径。
        quote = _leaf(a.get("quote"))
        if name == "delete_board_comment":
            return f"删除留言（含「{quote}」的那条）" if quote else "删除留言"
        v = A.normalize_verdict(a.get("verdict"))
        cn = A.BOARD_VERDICT_CN.get(v or "", "")
        head = f"人工复核留言（含「{quote}」的那条）" if quote else "人工复核留言"
        return f"{head}：{cn}" if cn else head
    if name in ("add_favorite", "remove_favorite"):
        # 用户自己的收藏（20260923 批 7）：过程行只报 id，**不报《标题》**——写行
        # 带标题会被下一轮读成"我读过这篇"的指代证据（同 execution_log 那条纪律）。
        # 措辞与 Rust `render_exec_row` 的同名臂**逐字一致**：预告帧与落库回执行
        # 是同一件事的两处渲染，两处不一样会让主人以为发生了两件事。
        aid = _leaf(a.get("article_id"))
        what = "收藏文章" if name == "add_favorite" else "取消收藏文章"
        return f"{what} {aid}" if aid else what
    if name == "read_messages":
        # 标记信已读（20260923 批 8）：与下面通知那条同一形状，措辞与 Rust
        # `render_exec_row` 逐字一致（预告帧与落库回执是同一件事的两处渲染）。
        if _norm_true(a.get("all")):
            return "标记站内信已读（全部未读）"
        mid = _norm_id_list(a.get("ids"))
        if mid:
            shown = "、".join(_leaf(i) for i in mid[:3])
            more = f" 等 {len(mid)} 封" if len(mid) > 3 else ""
            return f"标记站内信已读（{shown}{more}）"
        return "标记站内信已读"
    if name == "read_notifications":
        # 标记已读（20260923 批 7）：说清**标的是哪几条**（全标 / 具体 id 列表）。
        if _norm_true(a.get("all")):
            return "标记站内通知已读（全部未读）"
        ids = _norm_id_list(a.get("ids"))
        if ids:
            shown = "、".join(_leaf(i) for i in ids[:3])
            more = f" 等 {len(ids)} 条" if len(ids) > 3 else ""
            return f"标记站内通知已读（{shown}{more}）"
        return "标记站内通知已读"
    if name in ("freeze_account", "unfreeze_account"):
        # 账号冻结 / 解冻（20260926）：过程行只报**账号名**（账号没有《标题》可写，
        # 见 graph._POPUP_TITLE_TOOLS 那条注），**不报 uid**——uid 是内部编号，
        # 主人核对靠名字。措辞与 Rust `render_exec_row` 的同名臂**逐字一致**：
        # 预告帧与落库回执是同一件事的两处渲染，两处不一样会让主人以为发生了两件事。
        verb = "冻结账号" if name == "freeze_account" else "解冻账号"
        acct = _leaf(a.get("name"))
        return f"{verb}「{acct}」" if acct else verb
    if name == "send_user_notice":
        # 给单个账号发通知（20260926）：与冻结族同一条纪律——只报**账号名**、
        # **不报 uid**、**不报正文**（正文是主人刚在确认卡上核对过的那段话，
        # 过程行里再抄一遍只会让卡片上面的字和下面的字看起来是两件事）。
        # 措辞与 Rust `render_exec_row` 的同名臂**逐字一致**。
        acct = _leaf(a.get("name"))
        return f"给账号「{acct}」发通知" if acct else "给账号发通知"
    if name == "get_weather":
        # 天气（20260926 补臂）：Rust `render_exec_row` 的同名臂一直有，Python 这半
        # 漏了 ⇒ 过程行显示「执行 get_weather」（内部工具名带下划线）。
        loc = _leaf(a.get("location"))
        return f"查看天气「{loc}」" if loc else "查看天气"
    if name == "list_dashboard_todos":
        # 后台首页待办 / 日程（20260926 补臂）：**读**的那件（无参）。
        return "查看待办列表"
    if name == "create_dashboard_todo":
        # 后台首页待办 / 日程（20260926 补臂）：**写**的那件。正文按
        # device_oled_display 的同款截断（24 字）——待办正文上限 200 字，过程行放不下；
        # 落库回执（Rust render_exec_row）按列宽自己去截，两侧**措辞一致**即可。
        body = str(a.get("text") or "").strip()
        if not body:
            return "添加待办"
        due = str(a.get("date") or "").strip()
        head = f"添加待办「{body[:24]}」"
        return f"{head}（{due}）" if due else head
    if name == "complete_dashboard_todo":
        # 后台首页待办 / 日程（20260926 第十轮）：**勾完成**那件。正文同样按 24 字截断
        # （待办正文上限 200 字，过程行放不下；落库回执那侧按列宽自己去截，两侧**措辞
        # 一致**即可，同上面 create_dashboard_todo 那条注）。这里刻意**不写**「已完成」：
        # 这一行是**预告**（执行前发的过程行），而后端在幂等分支上是真 no-op；把结果写进
        # 动作名会让"本来就是完成"那一次看起来也改了什么（回执那侧另有 `changed` 判据）。
        body = str(a.get("text") or "").strip()
        return f"把待办「{body[:24]}」勾成完成" if body else "勾完成待办"
    if name in _NOARG_VERB:
        return _NOARG_VERB[name]
    return f"执行 {name}"


def _specs_from_plan(plan: str) -> list:
    """plan 契约文本 → TOOLS spec 的 (工具名, 参数 dict|None) 列表。

    切分规则与 graph.parse_plan 一致（`;` 分隔；「（无）」= 空清单）；参数
    解析失败/空参给 None——预览只出动作词，不硬猜参数。
    """
    out = []
    m = re.search(r"TOOLS\s*[:=]\s*(.+)", plan or "", re.IGNORECASE)
    if not m:
        return out
    for spec in m.group(1).split(";"):
        spec = spec.strip()
        if not spec or spec in ("（无）",):
            continue
        nm = spec.split("(", 1)[0].strip()
        am = re.match(r"^[^(]+\((.+)\)\s*$", spec, re.DOTALL)
        args = None
        if am:
            try:
                obj = json.loads(am.group(1))
                if isinstance(obj, dict):
                    args = obj
            except Exception:
                pass
        out.append((nm, args))
    return out


def _run_agent_stream_to_queue(messages: list, thread_id: str, queue: asyncio.Queue, loop, user_id: int = 0,
                               stop_event: threading.Event | None = None,
                               principal: Principal | None = None,
                               confirm_grant: dict | None = None,
                               conversation_id: int | None = None,
                               ledger: dict | None = None):
    """Run agent in a thread, push each chunk into an asyncio.Queue."""
    # user_id 注入 configurable（设备类工具经 RunnableConfig 读取，见 _run_agent_sync 注释）；
    # stop_event 一并注入——图内 model/tools 节点检查它实现断连中断（见 graph.AgentCancelled）
    # principal 一并注入（权限判据的输入，见 _run_agent_sync 注释）
    # conversation_id 一并注入（20260921）：**确认令牌的签发维度**——弹窗侧
    #   confirm.sign 把它签进令牌，验签侧要求一致（令牌换个会话就作废）
    # recursion_limit 覆盖默认 9999（等效无界，见 _run_agent_sync 注释）
    # confirm_grant：已验签的确认令牌 payload（**验签在 /chat/stream，不在图内**）
    #   ——非空即"用户在确认框上点了确定"这一轮，见 graph.graph_input
    # ledger：本请求注入给模型的两块台账原文（**由调用方算好传进来**）。图内的台账
    #   否认判据看的是"系统给模型看过什么"，而 `req` 只活在 /chat/stream 的处理器里、
    #   这里只有 messages ⇒ 台账必须由调用方（拿得到 req 的地方）传进来，与
    #   `_run_agent_sync` 的第 5 个参数同源同义（20260924：这行曾按 req/grant 直写，
    #   两个名字在本函数里都不存在 ⇒ 流式路径每次请求 NameError、零帧退出）。
    config = {"configurable": {"thread_id": thread_id, "user_id": user_id, "stop_event": stop_event,
                               "principal": principal or Principal(uid=user_id),
                               "conversation_id": conversation_id},
              "recursion_limit": RECURSION_LIMIT}
    try:
        # 双 stream_mode：
        #   "messages" —— token 级文本/工具结果帧（原逻辑不变）
        #   "updates"  —— 节点级状态更新：planner 的规划占位帧 / model 的最终回复
        #                 收集 / gate 的检查判定。gate fallback 时向前端发
        #                 __RESET__ 清空重绘 + 注入 fallback 文本作为最终回复
        #                 （叙述校验不过的轮次文本已作废，不重置会累积显示错误内容）
        # 过程帧记账：
        #   process_emitted —— 本次请求已发过过程步骤（决定收尾是否补"质检通过"）
        #   emitted —— 已发过程步骤的 key 集合（同一占位/完成帧同轮只发一次）
        #   is_chat_skill —— SKILL=chat：无执行可查，收尾不发"✓ 质检通过"，
        #     避免对闲聊展示虚假的质检过程
        # 最终回复正文（trace 落盘用）：updates 的 model 帧里取最后一条
        # AIMessage；gate fallback 时覆盖为 fallback 文本
        final_reply = ""
        process_emitted = False
        emitted: set = set()
        is_chat_skill = False
        # 跨轮执行记忆（20260904 C3）：checker 验收回执累计（execute update 是
        # 累计语义——末批即本次请求全量），流收尾时 __EXEC__ 帧发 Rust 落库
        exec_rows: list = []
        # 完成帧 diff 起点（20260905 issue5）：receipts 全量累计，已发条数起点
        # 之后为新增回执（同一 update 内顺序与执行顺序一致）
        receipt_sent = 0

        def emit_process(text: str, key: str = ""):
            nonlocal process_emitted
            if key:
                if key in emitted:
                    return
                emitted.add(key)
            process_emitted = True
            asyncio.run_coroutine_threadsafe(queue.put(f"__PROCESS__:{text}"), loop).result()

        def emit_reset(reason: str):
            asyncio.run_coroutine_threadsafe(queue.put(f"__RESET__:{reason}"), loop).result()

        for mode, data in _agent.stream(
            graph_input(messages, confirm_grant=confirm_grant,
                        ledger=ledger),
            config,
            stream_mode=["messages", "updates"],
        ):
            # 客户端断开检查：停止驱动图（不再发起新的 LLM 调用/工具执行）。
            # 节点级检查（model/tools raise AgentCancelled）兜住"正在节点内"的窗口；
            # 此处兜住"节点间迭代"的窗口（断连→感知最多 2s，见 event_stream 轮询）
            if stop_event is not None and stop_event.is_set():
                logger.info("[stream] cancelled by client disconnect (loop check)")
                break
            if mode == "messages":
                chunk, meta = data
                # 入队前过滤（同 _run_agent_sync）：只有 model 节点的回复文本帧、
                # 以及工具结果帧进队列；planner 的内部输出不发给前端（gate 无文本）
                if isinstance(chunk, AIMessageChunk):
                    if chunk.content and meta.get("langgraph_node") == "model":
                        # 20260903：model 零工具（不 bind_tools），不再有 tool_calls
                        # 占位帧；"🛠 正在调用工具…"占位改由 planner updates 分支
                        # 在计划含执行清单时发（execute 执行期间几秒静默，防"卡死"）
                        asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
                elif isinstance(chunk, ToolMessage) and chunk.content:
                    # 工具结果帧转发前端展示（命令帧解析/正文展示）。过程行不在
                    # 此发——旧"✅ 工具执行完成"固定模板不查 checker 验收：受阻
                    # 执行（空结果/错误帧）同样显示"完成"，且完成帧不带实际内容。
                    # 20260905 issue5 起完成/受阻行由 execute update 的
                    # receipts/blocked 驱动（见下方 execute 分支），此处只转发
                    asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
            elif mode == "updates":
                # 最终回复正文收集（trace 落盘）：model 节点的完整 AIMessage
                # （20260903 拓扑：model 只走一次收尾叙述轮，天然是最终轮）
                model_upd = data.get("model")
                if model_upd and model_upd.get("messages"):
                    _m = model_upd["messages"][-1]
                    if isinstance(_m, AIMessage) and not _m.tool_calls and _m.content:
                        final_reply = str(_m.content)
                # 计划（planner 是 invoke 非流式——messages 通道不会有其 chunk，
                # 规划占位帧在此发：所有技能都有"规划中"第一阶段反馈）
                planner_upd = data.get("planner")
                if planner_upd:
                    plan = str(planner_upd.get("plan", ""))
                    if plan.startswith("SKILL="):
                        emit_process("🧭 规划中…", key="planning")
                        if plan.startswith("SKILL=chat"):
                            # chat 快道：只发占位帧，不发计划明细（避免每条闲聊都有过程行）
                            is_chat_skill = True
                        else:
                            # 计划行人话化（20260905 issue5）：不再贴 plan 机器
                            # 契约原文（SKILL=/PARAMS= 截断成残句），改发 TOOLS
                            # spec 的中文动作摘要——与回执完成帧共用渲染、前后一致。
                            # 收尾轮（TOOLS 空/（无），execute 后叙事轮）不发——
                            # 无动作可预告，避免"计划:执行规划动作"式空行
                            acts = [_tool_action_text(nm, ar)
                                    for nm, ar in _specs_from_plan(plan)]
                            if acts:
                                hint = "、".join(acts)
                                if len(hint) > 100:
                                    hint = hint[:100].rstrip() + "…"
                                emit_process("🧭 计划：" + hint)
                        # 计划含执行清单 → execute 将确定性执行（期间几秒静默，
                        # 无此占位帧前端会像"卡死"）；完成/受阻帧由 execute
                        # update 的 receipts/blocked 驱动（见下方 execute 分支）
                        if "\nTOOLS: " in plan and "TOOLS: （无）" not in plan:
                            emit_process("🛠 正在调用工具…", key="tool_running")
                # execute 的 checker 验收回执（20260904 C3）：累计语义——每次
                # execute update 的 receipts 都是请求内全部 PASS 行，末批即全量
                ex_upd = data.get("execute")
                if ex_upd:
                    # 写操作确认弹窗（20260921）：execute 在同意闸上把"有意向但没判成
                    # 命令"的写 spec 收成一次确认（见 graph._confirm_popup），本轮
                    # **零执行、零 LLM**——图直接路由到 END（route_after_execute），
                    # narrator 结构上不会跑，也就不可能出现"已经建好啦"这类叙述。
                    # 回复正文由 adminops 确定性给出（confirm_text），走 AI 帧是为了
                    # 让 Rust 照常落库（前端切会话回头还能看见这段问句）。
                    if ex_upd.get("pending_confirm"):
                        popup = ex_upd["pending_confirm"]
                        emit_process("✋ 等待主人确认…", key="confirm_popup")
                        asyncio.run_coroutine_threadsafe(
                            queue.put("__CONFIRM__:" + json.dumps(
                                {"id": uuid.uuid4().hex[:8], "q": popup.get("q", ""),
                                 "opts": popup.get("opts") or [], "token": popup.get("token", ""),
                                 # 令牌失效时刻（20260924）：前端据此起倒计时、到点把
                                 # 卡片结算成"已过期，未执行"，不让它永远停在"已确认"。
                                 # 服务端仍以验签为唯一凭据——这个数只驱动展示。
                                 "exp": popup.get("exp") or 0},
                                ensure_ascii=False)),
                            loop).result()
                        # 跨轮待办（20260923）：同一件事的**结构化形态**落库（Rust 侧
                        # 收到即写、不转发前端）。与弹窗同轮发出是刻意的——主人可能
                        # 点完就切走/关页面，晚发等于没发；下一轮 planner 靠它认人。
                        pa = ex_upd.get("pending_action") or {}
                        if pa:
                            asyncio.run_coroutine_threadsafe(
                                queue.put("__PENDING__:" + json.dumps(pa, ensure_ascii=False)),
                                loop).result()
                        text = str(ex_upd.get("confirm_text") or "")
                        if text:
                            final_reply = text
                            asyncio.run_coroutine_threadsafe(
                                queue.put(AIMessageChunk(content=text)), loop).result()
                    # 过程行以 checker 验收为准（20260905 issue5）：✅ 完成帧只对
                    # 新增 PASS 回执发（receipts 累计，diff 起点后为新增，带实际
                    # 内容）；BLOCK 受阻项发 ✗ 行——真实执行失败不再显示"完成"
                    if ex_upd.get("receipts"):
                        rows = ex_upd["receipts"]
                        exec_rows = rows  # 全量（流收尾 __EXEC__ 用）
                        for i in range(receipt_sent, len(rows)):
                            r = rows[i]
                            emit_process("✅ " + _tool_action_text(
                                str(r.get("tool") or ""), r.get("args")),
                                key=f"receipt_{i}")
                        receipt_sent = len(rows)
                    for b in ex_upd.get("blocked") or []:
                        # ✗ 行在前、✅ 行在后（本轮两列表分开到达，不混排）；
                        # 同 spec 跨轮重复受阻只提示首次（key 按 spec 去重）
                        spec = str(b.get("spec") or "")
                        reason = _REASON_CN.get(str(b.get("reason") or ""),
                                                str(b.get("reason") or "执行受阻"))
                        bnm, bargs = "", None
                        for _nm, _ar in _specs_from_plan("TOOLS: " + spec):
                            bnm, bargs = _nm, _ar
                        emit_process("✗ " + _tool_action_text(bnm or str(b.get("tool") or ""),
                                                              bargs) + f"未成功（{reason}）",
                                     key=f"blocked_{spec}")
                # gate 检查判定（20260903：reflector/REVISE/LLM-QC 已废除——gate
                # 是终节点只收尾不重考：pass → done 收尾；fail → fallback 文本
                # 直接替换最终回复，见 graph.gate_node 注释）
                upd = data.get("gate")
                if not upd:
                    continue
                if upd.get("fallback_text"):
                    # fallback：叙述校验不过 → 前端 RESET 清空已展示文本重绘，
                    # 注入 fallback 文本（人设内如实回复）作为最终回复
                    reason = "叙述校验未通过，已替换为如实回复"
                    emit_process("✗ 质检打回：" + reason, key="gate_fallback")
                    emit_reset(reason)
                    final_reply = upd["fallback_text"]
                    emitted.clear()
                    asyncio.run_coroutine_threadsafe(
                        queue.put(AIMessageChunk(content=upd["fallback_text"])), loop).result()
                else:
                    # 检查通过收尾（gate 恒 done=True）；chat 快道无执行可查，
                    # 不发（见 is_chat_skill）
                    if process_emitted and not is_chat_skill:
                        emit_process("✓ 质检通过")
        else:
            # for 自然耗尽（无 break）= graph 完整跑完，未被断连打断
            logger.info("[stream] graph complete (uninterrupted)")
        # trace 落盘：最终回复随 producer 收尾记录（finish_trace 落盘时并入）
        record("producer", "stream_end", reply=final_reply)
        # 跨轮执行记忆帧（20260904 C3）：checker 验收回执（本次请求全部行）随流
        # 尾发出，Rust 收帧落库 execution_log（读取侧限最近 8 条）。放 None 之前
        # ——event_stream 收到即裸转发，Rust 在 __END__ 前解析完即可
        if exec_rows:
            asyncio.run_coroutine_threadsafe(
                queue.put("__EXEC__:" + json.dumps(exec_rows, ensure_ascii=False)),
                loop).result()
        asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()
    except AgentCancelled:
        # 图内节点检测到断连 → 静默收尾（客户端已断开，无帧可发；不放异常
        # 避免 event_stream 误发 __ERROR__ 到已断开的连接）
        logger.info("[stream] graph cancelled by client disconnect")
        asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()
    except Exception as e:
        asyncio.run_coroutine_threadsafe(queue.put(e), loop).result()
        # 20260905 哨兵补发：异常入队后仍须收尾 None——event_stream 遇异常对象
        # 即发 __ERROR__ 返回（不会读到 None），但 run_one/golden 等进程内消费者
        # 的 drain 线程只认 None 终止，缺哨兵会让调用方 t.join() 永久挂起
        # （实测：LLM client 未初始化时整进程挂到被 timeout 杀，exit 124/144）
        asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()


def _record_invalid_confirm(trace_id: str, uid: int, conv_id, token_len: int) -> None:
    """给被拒的确认请求落一份最小 trace。

    20260924 补：此前这条路径在 `start_trace` **之前** return（见下方 chat_stream），
    于是一个**自称"没执行任何改动"的回复在 trace 语料里完全不存在**——主人报"我点
    了确定但什么都没发生"时，agent 侧零证据，只能靠前端日志（而前端在这条链路的
    正路径上也没有留痕，见 chat-stream.js 的 reportConfirm）。落盘失败不影响回复。

    元数据由 `confirm.invalid_trace_meta` 给定（纯函数、被单测锁住：**只记令牌长度，
    绝不记令牌本身**）——那条纪律属于确认模块的语义，不属于这个调用点。
    """
    try:
        start_trace(trace_id, uid, f"invalid_confirm_{uuid.uuid4().hex[:8]}",
                    confirm.invalid_trace_meta(uid, conv_id, token_len))
        record("confirm", "rejected", reason="invalid_token",
               conversation_id=conv_id, token_len=int(token_len))
        finish_trace(trace_id, "invalid_confirm_token", 0.0, 1)
    except Exception:                        # trace 是观测，不是业务：绝不因它中断
        logger.exception("invalid confirm trace dump failed")


async def _invalid_confirm_stream():
    """确认令牌验不过时的最小 SSE 流：**一句话 + 结束帧，零执行零 LLM**。

    刻意复用正常帧协议（AI 文本帧 + `__END__`）而不是直接抛 4xx：前端这条隐藏
    请求走的是同一个 `sendMessage` 流循环，返 HTTP 错误会落进"发送失败"兜底、
    弹一条重试按钮（隐藏轮不该出现任何用户可见的重试控件）。走正常帧则：
    Rust 照常落库（主人回头能看见"确认已失效，没有执行任何改动"这句），
    前端照常渲染成一条 assistant 消息。

    正文里点明两种失效原因（超时 / 不同会话）：令牌是四重绑定的，主人看到的
    "我明明点了"多数是其中一种，说清楚才知道下一步该做什么。
    """
    text = "这次确认已经失效了（超过 10 分钟、或者不是在同一个会话里点的），我没有执行任何改动。需要的话跟我说一遍要做什么，我再问一次。"
    yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"
    yield "data: __END__\n\n"


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    # request: FastAPI 注入的原始请求对象（req 是 body 模型）——断连感知用，
    # 见 event_stream 的 receive 监听任务（20260827b 断连中断修复）
    logger.info("POST /chat/stream user=%s msg=%r needs_summary=%s",
                req.user_id, (req.message or "")[:40], req.needs_summary)
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")
    principal = _resolve_principal(request, req.user_id)   # 身份以签名为准（见 _resolve_principal）
    req.user_id = principal.uid
    # 隐藏确认请求（20260921）：带 confirm_token 的这轮，**令牌就是唯一凭据**
    # ——Rust 侧已经因为它是隐藏请求而跳过了用户消息入库（历史里没有"用户说要写"
    # 这条记录）；授权（scope）之后仍照常判，但"用户同意过"这件事只由这张令牌
    # 证明（签名 / 10 分钟 / uid / 会话四重绑定，见 agent/confirm.py）。
    # 验不过 = **零执行**：连图都不进——进图会照着 message 文本重新规划，那正是
    # 要避免的"再走一轮对话"。只如实回一句，不调 planner、不调工具。
    grant = None
    if req.confirm_token.strip():
        grant = confirm.verify(req.confirm_token, principal.uid, req.conversation_id)
        if grant is None:
            logger.warning("[confirm] 令牌验签失败（uid=%s conv=%s 长度=%d）→ 零执行",
                           principal.uid, req.conversation_id, len(req.confirm_token))
            _record_invalid_confirm(get_trace_id(), principal.uid,
                                    req.conversation_id, len(req.confirm_token))
            return StreamingResponse(
                _invalid_confirm_stream(), media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    # 并发闸（20260916 加固）：LLM 流是最贵的资源（单次最长 180s），无闸时并发涌进来
    # 只会一起排队到超时。**只加在生产路径 /chat/stream 上**——`/chat` 是非流式直连
    # 入口（评测脚本/golden 用，线上 rust.log 实测零访问），不占这条预算。
    # 释放一律由 event_stream 的 finally 负责（正常收尾/断连取消/超时/异常都走到）；
    # 下面这段 try 只兜"响应还没交出去就抛了"这种极小窗口，免得漏一个槽位把闸卡死。
    if not await _try_acquire_slot():
        raise HTTPException(503, "Agent busy（并发已满），请稍后重试")
    try:
        messages = _build_messages(req, confirm_grant=grant)
        # 每请求独立线程：避免 MemorySaver 线程状态随长对话无限累积（见 /chat 注释）
        thread_id = f"user_{req.user_id}_{uuid.uuid4().hex[:8]}"
        # trace 落盘（roadmap 步骤 2）：请求级 recorder 挂 contextvar——producer
        # 经 _submit_with_context 的 copy_context 继承，图节点内 record 命中；
        # 收尾由 event_stream finally 统一 finish_trace（见其注释，超时场景也要落盘）
        start_trace(get_trace_id(), req.user_id, thread_id, {
            "message": (req.message or "")[:200], "has_image": bool(req.image),
            "needs_summary": bool(req.needs_summary), "history_len": len(req.history),
            # 会话 id（20260924 补）：trace 里此前只有每请求随机的 thread_id，**没有
            # 会话身份**——跨源对账与事故复盘都缺这个最基本的锚点（20260924 复盘
            # "哪几张确认卡片属于同一个会话"时，只能靠 rust.log 的 chat POST 反推，
            # 而隐藏确认请求的轮次在 rust.log 里也认不出来）。会话 id 是系统事实、
            # 不含凭据（令牌/口令绝不进 trace，见下方 has_confirm 只记布尔）。
            "conversation_id": req.conversation_id,
            # 本轮是否带跨轮执行回执（gate 的"回执豁免"就靠这个事实，判据复扫时
            # 没有它就只能反推——20260921 补记，见 _state_action_claim 注释）
            "has_exec": bool(req.executions),
            # 本轮是不是"确认框点确定"的隐藏请求（20260921）。**只记布尔**——
            # 令牌本身是唯一凭据，绝不进 trace/日志（见 agent/confirm.py 头注）。
            "has_confirm": bool(req.confirm_token),
        })
    except Exception:
        _release_slot()
        raise

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        # 摘要独立化：needs_summary 轮与流式生成并行做后端总结（输入为原始历史，
        # 不依赖模型回复），流结束时随 __SUMMARY__ 帧返回（Rust 解析入库）
        summary_task = None
        if req.needs_summary:
            summary_task = _submit_with_context(
                loop, _summarize_dialogue, req.message, req.history, req.summary
            )

        # 断连中断机制（20260827c 终版）：stop_event 经 config 注入 agent 图，
        # event_stream 感知客户端断开即置位——agent 线程（producer）下次迭代/
        # 图内节点（model/tools）检查后立即停止，不再发起新的 LLM 调用或工具
        # 执行。原实现只在 yield 时感知断连：卡在 queue.get() 期间断连完全无感知，
        # agent 无察觉地继续执行 ReAct 循环——实测曾见断连后仍执行
        # device_oled_display 写操作（用户只问了在线设备）。
        #
        # 断连感知实现（20260827c 结论，两次踩坑后确认）：
        #   ✗ request.is_disconnected()：starlette 1.3.1 非阻塞检查（内部
        #     anyio.CancelScope 在 await 前立即取消），仅在 http.disconnect 已躺在
        #     receive 通道里时返回 True——uvicorn 通道被动读取，流式空闲期恒 False。
        #   ✗ 自建 request.receive() 后台任务：与 starlette StreamingResponse 的
        #     listen_for_disconnect 抢同一个 receive 通道（双消费者竞态）。
        #   ✓ 真实机制（starlette 1.3.1 × uvicorn 0.51 spec_version 2.3 老路径）：
        #     StreamingResponse.__call__ 的 task_group 里 listen_for_disconnect 挂起
        #     在 receive() 上，TCP 断开（FIN/RST）→ uvicorn connection_lost 投递
        #     http.disconnect → listen_for_disconnect 返回 → cancel_scope.cancel()
        #     → 迭代本生成器的 stream_response 任务被取消 → 当前 await/yield 点抛
        #     asyncio.CancelledError（毫秒级，比 2s 轮询快）。下方
        #     except asyncio.CancelledError 将其标记为 client_disconnect 留痕，
        #     finally 置位 stop_event——agent 停止。20260827 版正是靠这条链
        #     （CancelledError → finally → stop_event）在工作，is_disconnected 轮询
        #     实际从未触发。
        stop_event = threading.Event()

        # 并发启动生产者（不要 await 完成！否则所有 chunk 会在队列里攒到
        # 生成结束才一次性下发，等于没有流式）——边生成边推送
        producer_task = _submit_with_context(
            loop, _run_agent_stream_to_queue, messages, thread_id, queue, loop, req.user_id, stop_event,
            principal, grant, req.conversation_id,
            # 台账事实（洞⑦ 判据）：与 _build_messages 注入的同源，**在这里算**
            # ——req 只在这个作用域里（20260924 的线上事故就是把它写进了被调函数）
            _ledger_for_graph(req, confirmed=bool(grant)),
        )

        # 可观测性：请求生命周期账本（帧数/退出原因，finally 汇总）
        frames = 0
        end_reason = "unknown"
        nav_line = ""
        # 命令帧独占一行契约（20260903 实证）：execute 的命令帧先于 narrator
        # 叙述帧到达，Rust 存库（strip_command_lines）与前端（cleanAgentText）
        # 都是行级过滤——命令与正文无换行拼接成单行时整行被剥空（chat_history
        # 3465 空行 → 转跳后回复丢失）。yield 出命令帧后置位；下一个文本帧
        # （叙述首帧或连发的命令帧）前插换行，保证命令各自独占一行
        pending_nl = False
        had_output = False
        started = loop.time()
        last_frame = started
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    # 2s 轮询间隔顺带承担兜底超时（原长等待语义保留，双保险）：
                    # 1) 空闲超时：LLM/线程池异常挂起时超过 STREAM_IDLE_TIMEOUT 无输出帧即终止
                    #    （曾出现 API 无响应占满 8 线程池、后续对话全部排队卡死）
                    # 2) 总时长上限：agent 工具调用循环/超长生成时每轮都有帧会重置空闲计时，
                    #    用 STREAM_TOTAL_TIMEOUT 总时长硬上限保证流必会终止
                    elapsed = loop.time() - started
                    if elapsed >= STREAM_TOTAL_TIMEOUT:
                        end_reason = "total_timeout"
                        logger.error("Chat stream total timeout (%.0fs) reached, aborting", elapsed)
                        yield f"data: __ERROR__:{json.dumps('生成时间过长，请稍后重试', ensure_ascii=False)}\n\n"
                        return
                    if loop.time() - last_frame >= STREAM_IDLE_TIMEOUT:
                        end_reason = "idle_timeout"
                        logger.error("Chat stream idle timeout (%.0fs), aborting", elapsed)
                        yield f"data: __ERROR__:{json.dumps('服务响应超时，请稍后重试', ensure_ascii=False)}\n\n"
                        return
                    continue
                last_frame = loop.time()
                if chunk is None:
                    end_reason = "producer_done"
                    # 终止帧必须是最后一帧（20260923）：哨兵由生产者在 finally 里投出
                    # （它在所有帧之后），**哨兵之后再入队的帧永远不会被本循环取走**
                    # （循环已 break）——那就是"悄悄丢了内容"。而三端对"终止帧之后还能
                    # 不能有帧"的语义各不相同：Rust 见 __END__ 即 break 停止转发，前端
                    # 却是 continue 继续读到连接关闭，此前没有一端把它写成断言。这里只查
                    # 不改：残留照旧丢弃（不留不确定性），但必须响亮。
                    residue = queue.qsize()
                    if residue:
                        logger.error("[stream] 终止帧之后生产者又投了 %d 个帧（协议违约："
                                     "这些帧不会下发，检查 queue.put 是否晚于哨兵 None）", residue)
                    break
                if isinstance(chunk, Exception):
                    end_reason = "producer_error"
                    # 这里**不能**用 logger.exception：异常是在生产者线程里捕获后
                    # 经队列送过来的，本协程没有"正在处理中"的异常，exc_info 取到
                    # (None,None,None)，日志只剩一行 "NoneType: None" ——真正的
                    # traceback 全丢了（20260921 22:37 排障就是被这个坑住的：只
                    # 知道流炸了、不知道炸在哪）。把异常对象本身交给 exc_info，
                    # logging 会用它自带的 __traceback__ 渲染。
                    logger.error("Agent streaming failed: %s: %s",
                                 type(chunk).__name__, chunk, exc_info=chunk)
                    yield f"data: __ERROR__:{json.dumps(str(chunk), ensure_ascii=False)}\n\n"
                    return
                # 过程展示/质检重置控制帧（__PROCESS__:<步骤> / __RESET__:<原因>）：
                # JSON 编码原样转发，前端归档到灰色可折叠过程行
                # __CONFIRM__（20260921）走同一族：**必须 JSON 编码**（帧体是
                # `__CONFIRM__:<json>`，裸帧在 Rust 的 JSON 解析分支之前拦才行，
                # 那种做法漏一次就把整坨 JSON 累积进回复并落库）。Rust 侧同族处理
                # ——只转发、不累积、不落库。
                if isinstance(chunk, str) and (chunk.startswith("__PROCESS__")
                                               or chunk.startswith("__RESET__")
                                               or chunk.startswith("__CONFIRM__")):
                    had_output = True
                    frames += 1
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    continue
                if isinstance(chunk, str) and chunk.startswith("__PENDING__:"):
                    # 跨轮待办帧（20260923）：与 __EXEC__ 同族——同样**必须带
                    # "data: " 前缀**（Rust 的 SSE 解析是 strip_prefix(b"data: ")，
                    # 裸 yield 到不了落库分支），Rust 落库后吞掉、不转发前端
                    # （前端无此帧协议，透传会被当正文渲染）。
                    yield f"data: {chunk}\n\n"
                    continue
                if isinstance(chunk, str) and chunk.startswith("__EXEC__:"):
                    # 跨轮执行记忆帧（20260904 C3）：Rust 收帧解析落库 execution_log，
                    # 不转发前端（前端无此帧兜底，Rust 吞掉不 yield）。
                    # ★ 必须带 "data: " 前缀（20260904 上线首轮 E2E 抓出）：Rust 的
                    # SSE 帧解析 strip_prefix(b"data: ") 无前缀即空 payload 丢弃——
                    # 裸 yield 的 __EXEC__ 从未到达 Rust 落库分支。golden 内部链路
                    # 不经 SSE 文本协议（queue 直收 str）故测不到，只有线上 E2E 能暴露
                    yield f"data: {chunk}\n\n"
                    continue
                if isinstance(chunk, AIMessageChunk) and chunk.content:
                    had_output = True
                    frames += 1
                    text = str(chunk.content)
                    # 命令帧独占一行契约：命令帧后第一个叙述帧前插换行（若 LLM
                    # 没自带头部换行）——叙述 delta 任意切分，只在此处加一次，
                    # 后续 delta 内联，绝不能逐帧加换行
                    if pending_nl:
                        if not text.startswith("\n"):
                            text = "\n" + text
                        pending_nl = False
                    # JSON 编码避免文本内的 \n\n 破坏 SSE 帧边界
                    yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"
                elif isinstance(chunk, ToolMessage) and chunk.content:
                    text = str(chunk.content)
                    if text.startswith("NAVIGATE:") or text.startswith("AUTO_NAVIGATE:"):
                        nav_line = text
                        had_output = True
                        frames += 1
                        # 连发命令帧也要各自独占一行（无叙述间隔时前插换行）
                        if pending_nl and not text.startswith("\n"):
                            text = "\n" + text
                        pending_nl = True
                        yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"
                    elif text.startswith("EFFECT:") or text.startswith("DARKMODE:"):
                        nav_line = text
                        had_output = True
                        frames += 1
                        if pending_nl and not text.startswith("\n"):
                            text = "\n" + text
                        pending_nl = True
                        yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"

            # 空输出兜底：整轮无任何帧（qwen 偶发空内容）→ 补发人设内恢复语，
            # 前端不会静默无感知（Rust 空回复不存历史、UI 无任何反馈即"卡死"）
            if not had_output:
                logger.warning("Chat stream ended with no output (agent produced no text/no command)")
                frames += 1
                yield f"data: {json.dumps(_RECOVERY_SENTENCE, ensure_ascii=False)}\n\n"

            # 独立摘要结果帧（必须在 __END__ 之前：Rust 收到 __END__ 即终止解析）
            if summary_task is not None:
                new_summary = (await summary_task).strip()
                if new_summary:
                    yield f"data: __SUMMARY__:{json.dumps(new_summary, ensure_ascii=False)}\n\n"

            yield f"data: __{'NAV_END' if nav_line else 'END'}__\n\n"
        except asyncio.CancelledError:
            # 客户端断开 → starlette listen_for_disconnect → cancel_scope.cancel()
            # → 本生成器抛 CancelledError（见上方机制注释）。标记断连并重抛——
            # 必须 re-raise：task_group 依赖取消传播做干净收尾（不 re-raise 会被
            # uvicorn 当作正常完成，连接可能不关闭）。
            end_reason = "client_disconnect"
            logger.info("[stream] client disconnected (starlette cancel), aborting")
            raise
        except Exception as e:
            # yield 写失败（客户端断开后继续写帧 → uvicorn ClientDisconnected）：
            # 留痕并静默收尾——不吞的话 uvicorn 对 ClientDisconnected 是静默的
            # （无 ERROR 日志），断连事件就会像"从未发生"一样（20260827b 可观测性）
            end_reason = "yield_failed"
            logger.warning("[stream] yield 失败（客户端断开?）: %s", e)
        finally:
            # 可观测性：请求生命周期汇总（所有退出路径——断连/超时/异常/正常收尾）
            # unknown 归一：连接被外部关闭（如 head 截断管道）时生成器非异常
            # 终止（GeneratorExit 类路径，不匹配任何 except），end_reason 保持
            # 初值——归一为 client_closed，日志/trace 均可解释
            if end_reason == "unknown":
                end_reason = "client_closed"
            logger.info("[stream] end reason=%s duration=%.1fs frames=%d",
                        end_reason, loop.time() - started, frames)
            # 并发槽位归还：**所有退出路径都经过这里**（正常收尾/断连取消/空闲与总超时/
            # 异常/客户端提前关闭），与 chat_stream 开头的 _try_acquire_slot 成对。
            _release_slot()
            # trace 落盘：所有退出路径统一收尾（超时场景 producer 还挂着，
            # 落中途 trace——事件序列最后一条即挂点，如 model llm_start 后无
            # llm_done 就是 LLM API 侧慢；dumped 后线程晚到的事件丢弃不补写）
            finish_trace(get_trace_id(), end_reason, loop.time() - started, frames)
            # 任何退出路径（断连/超时/正常收尾）都通知生产者停止：
            # 图内节点检查 stop_event 后终止，避免 agent 在无人接收时继续消耗
            stop_event.set()
            # 客户端提前断开时，取消尚未完成的生产者任务
            # （线程池任务 cancel 无效，真正的中断由 stop_event 驱动，
            #   见 _run_agent_stream_to_queue/图内节点）
            if not producer_task.done():
                producer_task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# /review — 留言 AI 审核（20260905：博客留言板「河灯集」入库前同步调用）
# ---------------------------------------------------------------------------


class ReviewRequest(BaseModel):
    """AI 审核请求。content = 待审留言文本。

    20260917 加限额（外部审计指出）：此前 `content: str` 无上限——/review 由 Rust
    的 AI 审核同步调用，一次请求可以顶着 12MB 的整体 body 上限灌进来（模型只取前
    500 字符，但解析/strip/日志前都要先在内存里过一遍）。留言本身有 2000 字上限，
    这里给 4000 留一倍余量。（审计说"日志会记录完整 text"不准确：实际是
    `content=%.60s`，只打 60 字符。）"""
    content: str = Field(max_length=4000)
    author: str = Field(default="", max_length=100)
    # 发起人 uid（20260925 审计 A5 起由 Rust 带上）：只用于身份断言核对与日志留痕。
    # 缺省 0 = 老调用方（那时只能靠断言本身，或用不了断言时退回"身份不明"）。
    uid: int = Field(default=0, ge=0)


@app.post("/review")
def review_message(request: Request, req: ReviewRequest):
    """对一条留言做 pass/flag 一次裁决（qwen 低随机、无思考链、同步、25s 上限）。

    语义：pass = 内容可公开展示；flag = 拦下进待审（是否还需人工放行由 Rust 按
    manualReviewEnabled 开关决定——本端点只给裁决，不知道站点开关组合）。模型/
    网络异常直接抛 500（调用方 Rust 侧超时 / 非 200 / 解析失败一律**转人工待审**，
    不是放行——`talks.rs::board_approved` 的三个失败分支都返回 `(0, None, None)`，
    即"宁可多一次人工，绝不放行未经审核的内容"。兜底决策在调用方，此处不吞异常，
    保证 Rust 日志可见性）。

    20260925 审计两处加固：
    · **身份**：此前这个端点没有任何身份断言，严重度完全押在"8010 只听回环"这一个
      部署事实上（谁连得上就能免费烧模型额度）。现在与 `/chat` 同款走
      `_resolve_principal`（同一个开关 `AGENT_REQUIRE_ASSERTION`）。
      **部署顺序是硬要求**：该开关在生产 .env 里**已经是 1**（不是"以后再打开"）⇒
      必须先部署 Rust 的 `talks.rs`（它开始随请求发 `X-Agent-Assertion`），
      再部署这半；顺序反了这条端点会成片 401，每一条留言都转人工待审。
    · **并发**：`REVIEW_CONCURRENCY` 槽位（默认 4），排队超 3s 如实 503 —— 调用方
      对这个端点的失败处理是"转人工待审"，不会漏审。
    """
    principal = _resolve_principal(request, req.uid)
    if not _review_slots.acquire(timeout=REVIEW_QUEUE_WAIT):
        logger.warning("[review] 并发闸已满（%d 槽），排队 %.1fs 未拿到 → 503",
                       REVIEW_CONCURRENCY, REVIEW_QUEUE_WAIT)
        raise HTTPException(503, "审核队列已满，请稍后重试")
    try:
        from agent import moderator
        text = (req.content or "").strip()
        # 实现收进 agent/moderator.py（20260920）：不信任输入（访客正文进围栏）、
        # 输出白名单、fail-open 取向，以及 tests/test_side_tasks.py 的回归锁。
        try:
            result = moderator.review(text)
        except Exception:
            logger.exception("[review] LLM 调用失败（Rust 侧将转人工待审）")
            raise
    finally:
        _review_slots.release()
    logger.info("[review] verdict=%s reason=%.60s uid=%s content=%.60s",
                result["verdict"], result["reason"], principal.uid, text)
    return result


class GraphQueryRequest(BaseModel):
    """图谱向量检索请求。q = 访客在展示柜里输入的查询串。"""
    # 前端本来就截到 64 字符（locate.ts QUERY_MAX），这里留一倍余量做上限——
    # 超长串会白烧一次 embedding 调用（20260916 加固）。
    q: str = Field(default="", max_length=128)


@app.post("/graph/query")
async def graph_query(req: GraphQueryRequest):
    """首页展示柜「文章向量空间」的向量检索：查询串 → 图谱里最近的若干词。

    **可降级端点**：任何失败都返回 HTTP 200 + ok=false（前端据此退回本地关键词
    匹配），绝不抛 4xx/5xx——这不是 CRUD 资源，404 之类的语义在这里只会让调用方
    的降级分支更难写。

    鉴权不在这里：本服务只监听 127.0.0.1，走 nginx 的那一层在 Rust
    （`/api/public/graph/query` 要求登录，防匿名刷 embedding 费用）。

    跑在线程池里（纯 Python 点积 + 一次阻塞的 HTTP embedding 调用），并显式传播
    context 以便日志带上 tid。
    """
    loop = asyncio.get_running_loop()
    return await _submit_with_context(loop, wordgraph.query_words,
                                      req.q, wordgraph.TOP_K_DEFAULT)


@app.get("/health")
async def health():
    return {"status": "ok", "agent_ready": _agent is not None}


if __name__ == "__main__":
    import uvicorn
    # 机器内存有限（3.7GB），4 个 worker 会周期性被系统杀掉导致对话连接中断；
    # 2 个 worker + 每 worker 8 线程 executor 足够博客并发，且更稳定
    uvicorn.run(app, host="127.0.0.1", port=8010, workers=2)
