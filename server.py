"""HTTP service entry point: uvicorn server:app --host 127.0.0.1 --port 8010."""

import asyncio
import functools
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from langchain_core.messages import AIMessageChunk
from pydantic import BaseModel, Field

from agent import (
    checkpoint,
    confirm,
    runtime_probe,
)
from agent.api.authentication import resolve_principal as _resolve_principal
from agent.api.context import build_messages
from agent.api.events import control_event, text_stream
from agent.api.events import frame as sse_frame
from agent.api.events import response as stream_response
from agent.api.routes.knowledge_graph import router as knowledge_graph_router
from agent.api.routes.moderation import router as moderation_router
from agent.api.schemas import ChatRequest, ChatResponse, HistoryItem
from agent.api.schemas import QuotaInfo as QuotaInfo
from agent.approval_api import router as approval_router
from agent.block_reasons import REASONS as _BLOCK_REASONS
from agent.principal import Principal
from agent.runtime.budget import current_budget
from agent.runtime.control import AgentCancelled, RequestControl
from agent.runtime.progress import ProgressEmitter, StreamProgress
from agent.runtime.workers import RequestWorkers
from agent.summarizer import summarize
from agent.transport import collect_turn, emit_to_queue

# The block-reason map remains a shared compatibility surface for existing
# authorization and receipt tests; transport does not render model planning text.
from config.settings import settings
from rag import search as rag_search
from rag import wordgraph
from utils.logging import get_trace_id, set_trace_id, setup_logging
from utils.trace import finish_trace, record, start_trace

logger = logging.getLogger(__name__)

# Shared error text remains part of the authorization/receipt contract; the
# transport no longer renders model-loop blocked updates itself.
_REASON_CN = {key: value[0] for key, value in _BLOCK_REASONS.items()}


class _NativeCancelled:
    """Worker cancellation sentinel; it must not become a successful END frame."""
def _submit_with_context(loop, func, *args, jobs):
    """Submit only tracked work to the lifespan-owned executor."""
    return jobs.submit(app.state.workers.executor, func, *args)

# 流式输出兜底超时（秒）：与前端 120s 空闲超时对齐。
# LLM/线程池异常挂起时主动终止流，避免对话无限等待（见 event_stream 的 wait_for）
# （20261020 起真值源是 Settings 的 AGENT_STREAM_IDLE_TIMEOUT——写 .env 就生效，
# 判据 tests/test_settings_surface.py ⑥；此前这里硬编码，改一次值要动一次码。）
STREAM_IDLE_TIMEOUT = settings.stream_idle_timeout
# 流式总时长硬上限（秒）：agent 工具调用循环/超长生成时每轮都有输出帧，
# 空闲超时不会触发（帧流动会重置），需用总时长兜底保证流必会终止
STREAM_TOTAL_TIMEOUT = settings.stream_total_timeout

# Deprecated compatibility export. NativeRuntime bounds work with RequestBudget.
RECURSION_LIMIT = settings.recursion_limit

# Shared public error text; detailed exceptions belong in logs and traces.
PRODUCER_ERROR_TEXT = "服务这边出了点问题，这一轮没能生成回复，请再说一次。"

# 空回复恢复语：agent 流正常收尾但无任何输出（qwen 偶发空内容）时补发的人设内
# 兜底文本——否则前端静默无感知（Rust 空回复不存历史、UI 无任何反馈，即"卡死"）
_RECOVERY_SENTENCE = "喵呜……主人抱歉，泠月喵刚才脑袋卡壳了，没有生成出回复，请主人再问一遍喵～ 🐾"

# ── 输入限额与并发闸（20260916 加固）──
# HTTP body and worker admission limits. Field limits live with the API schemas.
MAX_BODY_BYTES = settings.max_body_bytes
MAX_CONCURRENT_STREAMS = settings.max_concurrent_streams
STREAM_QUEUE_WAIT = 3.0         # 秒；排队超过这个时间就如实 503，不让请求无声堆着

# 并发闸（每 worker 一个；uvicorn --workers 2 ⇒ 全局 2×MAX_CONCURRENT_STREAMS）。
# Python 3.10+ 起 asyncio.Semaphore() 不在构造时绑事件循环，模块级创建是安全的。
_stream_slots = asyncio.BoundedSemaphore(MAX_CONCURRENT_STREAMS)

async def _try_acquire_slot() -> bool:
    """拿并发槽位；排队超过 STREAM_QUEUE_WAIT 秒返回 False（调用方回 503，别无声排队）。"""
    workers = app.state.workers
    if workers is None or workers.closing:
        return False
    try:
        await asyncio.wait_for(_stream_slots.acquire(), timeout=STREAM_QUEUE_WAIT)
        if workers.closing:
            _stream_slots.release()
            return False
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

_agent = None


def _runtime_holds_context() -> bool:
    """Whether the live runtime owns persistent conversation history."""
    return runtime_probe.holds_context(_agent)


def _thread_id_for(req) -> str:
    """Use the same conversation coordinate for HTTP and SSE."""
    return checkpoint.thread_id_for(req.user_id, req.conversation_id,
                                    _runtime_holds_context())


def _build_arm():
    """Build the native production runtime; retired selections fail at startup."""
    from agent.runtime.native import build

    arm = (settings.agent_arm or "native").strip().lower()
    if arm != "native":
        raise RuntimeError(f"AGENT_ARM={arm!r} 已退役，请配置 AGENT_ARM=native。")
    return build(), "native"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the runtime after validating settings, then warm optional retrieval services."""
    global _agent, _stream_slots
    setup_logging()
    logger.info("Initialising LangChain agent ...")
    from config.settings import settings
    _stream_slots = asyncio.BoundedSemaphore(MAX_CONCURRENT_STREAMS)
    workers = RequestWorkers(request_timeout=STREAM_TOTAL_TIMEOUT)
    app.state.workers = workers
    try:
        _provider = settings.check_provider()
        logger.info("LLM provider ready (provider=%s)", _provider)
        # Missing credentials keep liveness available; readiness reports the failure.
        if not settings.is_api_key_configured:
            logger.warning(
                "LLM 凭据缺失：provider=%s 的 api_key 为空 —— 进程会照常启动，"
                "但每一条对话都会失败（/ready 现在报 missing_credentials）。", _provider)
        _agent, _arm = _build_arm()
        logger.info("Agent ready (arm=%s)", _arm)
        # Optional warmups do not block startup.
        workers.warm_up(wordgraph.warm)
        workers.warm_up(rag_search.warm)
        yield
    finally:
        workers.stop()
        try:
            await workers.aclose()
        finally:
            _agent = None
            app.state.workers = None
            await asyncio.to_thread(checkpoint.close_pool)
        logger.info("Agent stopped; request workers and checkpoint pool closed")


def _app_version() -> str:
    """Read the installed project version; identify bare source checkouts explicitly."""
    try:
        return _pkg_version("saudade-agent")
    except PackageNotFoundError:
        return "0.0.0+source"


app = FastAPI(title="Saudade Blog Agent", version=_app_version(), lifespan=lifespan)
app.state.workers = None


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


app.include_router(approval_router(lambda: _agent, _resolve_principal))
app.include_router(moderation_router)
app.include_router(knowledge_graph_router)


def _build_messages(req: ChatRequest, confirm_grant: dict | None = None) -> list:
    """Adapt the request using the active runtime's history ownership."""
    return build_messages(req, holds_context=_runtime_holds_context())


def _runtime_events(messages: list, *, thread_id: str, user_id: int,
                    principal: Principal | None, conversation_id: int | None,
                    confirm_grant: dict | None = None, ledger: dict | None = None,
                    recent_tools: list[str] | None = None, stop_event=None,
                    open_tasks: str = "", progress_emitter=None):
    """Bind verified request coordinates to the native Runtime event contract."""
    stream_events = getattr(_agent, "stream_events", None)
    if not callable(stream_events):
        raise RuntimeError("native Runtime stream_events is not available")
    config = {"configurable": {
        "thread_id": thread_id, "user_id": user_id,
        "conversation_id": conversation_id,
        "principal": principal or Principal(uid=user_id),
        "stop_event": stop_event,
        "progress_emitter": progress_emitter,
        "budget": current_budget(),
    }}
    state = {"messages": messages}
    if confirm_grant is not None:
        state["confirm_grant"] = confirm_grant
    return stream_events(state, config)


def _run_agent_sync(messages: list, thread_id: str, user_id: int = 0,
                    principal: Principal | None = None,
                    ledger: dict | None = None,
                    recent_tools: list[str] | None = None,
                    conversation_id: int | None = None) -> tuple[str, str, list]:
    """Run one native turn and return the legacy non-streaming response shape."""
    turn = collect_turn(_runtime_events(
        messages, thread_id=thread_id, user_id=user_id, principal=principal,
        conversation_id=conversation_id, ledger=ledger, recent_tools=recent_tools))
    return turn.reply, turn.navigation, turn.execution


def _summarize_dialogue(user_msg: str, history: list[HistoryItem], old_summary: str) -> str:
    """Generate independent memory from dialogue data; failures preserve the old summary."""
    return summarize(user_msg, history, old_summary)


# ---------------------------------------------------------------------------
# /chat  — 非流式（供 Rust 后端调用）
# ---------------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")
    principal = _resolve_principal(request, req.user_id)
    req.user_id = principal.uid

    # Host quota decisions precede admission and any model work.
    if req.quota_blocked:
        _limit = req.chat_quota.limit if req.chat_quota is not None else 0
        logger.info("[quota] 额度用尽，本轮零执行（uid=%s limit=%s）", req.user_id, _limit)
        return ChatResponse(reply=_quota_blocked_text(_limit), success=True, error=None)

    messages = _build_messages(req)
    # 每请求独立线程（**没挂 saver 时**）：LangGraph 的 MemorySaver 线程状态会随对话无限
    # 累积，长对话（教程连载等）会让输入上下文与 worker 内存持续膨胀直至截断/被杀。
    # 对话连续性由请求体中的 DB 历史(最近20条) + chat_summary 摘要承担，无需线程累积。
    # 挂了 saver ⇒ 会话坐标（见 `_thread_id_for`），那正是"图自持上下文"的实现方式。
    thread_id = _thread_id_for(req)

    if not await _try_acquire_slot():
        raise HTTPException(503, "Agent busy（并发已满），请稍后重试")
    loop = asyncio.get_running_loop()
    jobs = app.state.workers.request(loop, _release_slot, control=RequestControl(
        threading.Event(), time.monotonic() + STREAM_TOTAL_TIMEOUT))
    try:
        loop = asyncio.get_running_loop()  # 非 get_event_loop()：后者 3.10 起已废弃
        # 摘要独立化（needs_summary 轮）：与 agent 图并行做后端总结——输入是原始
        # 历史数据而非模型回复，杜绝"回复耦合生成"时代的推断/编造（曾出现摘要
        # 编造"助手调用工具"污染记忆）。生成失败返回空 → 不入库，旧摘要保留。
        summary_task = None
        if req.needs_summary:
            summary_task = _submit_with_context(
                loop, _summarize_dialogue, req.message, req.history, req.summary,
                jobs=jobs
            )
        reply, nav_line, exec_rows = await _submit_with_context(
            loop, _run_agent_sync, messages, thread_id, req.user_id, principal,
            None, None, req.conversation_id, jobs=jobs)
        new_summary = None
        if summary_task is not None:
            new_summary = (await summary_task).strip() or None

        # 空回复兜底：qwen 偶发空内容 → 下发人设内恢复语，避免前端静默无感知
        if not reply.strip():
            logger.warning("Agent returned empty reply for message=%r", req.message[:60])
        final_reply = reply.strip() or _RECOVERY_SENTENCE

        # 摘要剥离之后再把导航/特效命令行追加回去，确保命令不被 SUMMARY 截断吞掉
        final_nav = nav_line
        if final_nav and not final_reply.startswith(("NAVIGATE:", "AUTO_NAVIGATE:")):
            if final_nav.startswith("EFFECT:"):
                final_reply = final_reply + "\n" + final_nav
            else:
                final_reply = final_nav + "\n" + final_reply

        return ChatResponse(reply=final_reply, success=True, new_summary=new_summary,
                            executions=exec_rows)
    except Exception:
        # 内部异常的**明细只进日志**（`logger.exception` 带 traceback），不下发。此前这里
        # 是 `reply=""` + `error=str(e)`：异常名与内部路径顺着 `error` 出到调用方，
        # 而 HTTP 状态码仍是 200 ⇒ 调用方若只看状态码就把它当成"成功但空回复"。
        # 两条通道同族（20260927 的 SSE 事故就是这一族的另一条路），故两处同修：
        # 这里把**话术**填进 reply、把 success 置假（Rust 侧按 `success` 判，见
        # `src/routes/chat.rs::agent_reply_of`），明细留给日志。
        logger.exception("Agent invocation failed")
        return ChatResponse(reply=PRODUCER_ERROR_TEXT, success=False,
                            error=PRODUCER_ERROR_TEXT)

    finally:
        jobs.close()


# ---------------------------------------------------------------------------
# /chat/stream — SSE 流式（供前端 Live2D 调用）
# ---------------------------------------------------------------------------
def _run_agent_stream_to_queue(messages: list, thread_id: str, queue: asyncio.Queue, loop,
                               user_id: int = 0,
                               stop_event: threading.Event | None = None,
                               principal: Principal | None = None,
                               confirm_grant: dict | None = None,
                               conversation_id: int | None = None,
                               ledger: dict | None = None,
                               open_tasks: str = "",
                               recent_tools: list[str] | None = None,
                               progress_emitter: ProgressEmitter | None = None):
    """Run native events in the worker and enqueue protocol-safe values.

    Queue insertion is the only async boundary here. Runtime exceptions are
    delivered before the terminal sentinel, so the HTTP adapter cannot emit a
    successful end frame after an error or cancellation.
    """
    visible_parts = []
    def put(value):
        if loop.is_closed():
            raise AgentCancelled()
        if stop_event is not None and stop_event.is_set():
            raise AgentCancelled()
        pending = queue.put(value)
        try:
            future = asyncio.run_coroutine_threadsafe(pending, loop)
        except RuntimeError:
            pending.close()
            raise AgentCancelled() from None
        try:
            future.result(timeout=5)
            if isinstance(value, AIMessageChunk):
                visible_parts.append(str(value.content or ""))
        except TimeoutError:
            future.cancel()
            raise AgentCancelled() from None
    try:
        events = _runtime_events(
            messages, thread_id=thread_id, user_id=user_id, principal=principal,
            conversation_id=conversation_id, confirm_grant=confirm_grant,
            ledger=ledger, recent_tools=recent_tools, stop_event=stop_event,
            open_tasks=open_tasks, progress_emitter=progress_emitter)
        emit_to_queue(events, put)
        put(None)
    except AgentCancelled:
        logger.info("[stream] native runtime cancelled")
        if not loop.is_closed():
            loop.call_soon_threadsafe(queue.put_nowait, _NativeCancelled())
    except Exception as exc:
        logger.exception("[stream] native runtime producer failed: %s", exc)
        put(exc)
        try:
            put(None)
        except (AgentCancelled, RuntimeError):
            logger.info("[stream] producer sentinel consumer already closed")
    finally:
        record("producer", "stream_end", reply="".join(visible_parts))


def _record_invalid_confirm(trace_id: str, uid: int, conv_id, token_len: int,
                            reason: str = "invalid_token", detail: str = "",
                            exit_reason: str = "invalid_confirm_token") -> None:
    """给被拒的确认请求落一份最小 trace。

    20260924 补：此前这条路径在 `start_trace` **之前** return（见下方 chat_stream），
    于是一个**自称"没执行任何改动"的回复在 trace 语料里完全不存在**——主人报"我点
    了确定但什么都没发生"时，agent 侧零证据，只能靠前端日志（而前端在这条链路的
    正路径上也没有留痕，见 chat-stream.js 的 reportConfirm）。落盘失败不影响回复。

    元数据由 `confirm.invalid_trace_meta` 给定（纯函数、被单测锁住：**只记令牌长度，
    绝不记令牌本身**）——那条纪律属于确认模块的语义，不属于这个调用点。

    `reason` / `detail`（20260929 批 F）只为**区分两个出口**：`invalid_token`（验签
    没过）与 `invalid_pick`（令牌没问题、是"只办第几件"那个记号读不懂）。两者都是
    零执行，但复盘时要走的路完全不同——一个是"令牌过期/换了会话"，一个是"前端把
    下标写坏了"。`detail` **不许带令牌**（它只装 `confirm.narrow` 给的那句原因）。
    """
    try:
        start_trace(trace_id, uid, f"invalid_confirm_{uuid.uuid4().hex[:8]}",
                    confirm.invalid_trace_meta(uid, conv_id, token_len))
        record("confirm", "rejected", reason=reason,
               conversation_id=conv_id, token_len=int(token_len),
               detail=str(detail or "")[:120])
        # 退出原因**显式传入**（不给默认值拼一个 "invalid_confirm_invalid_token"：
        # 存量那条 `invalid_confirm_token` 是既有观测面的字面量，改一个字就是换了一个
        # 退出原因、跨源对账的旧账本会对不上）。
        finish_trace(trace_id, exit_reason, 0.0, 1)
    except Exception:                        # trace 是观测，不是业务：绝不因它中断
        logger.exception("invalid confirm trace dump failed")


INVALID_CONFIRM_TEXT = (
    "这次确认已经失效了（超过 10 分钟、或者不是在同一个会话里点的），我没有执行任何改动。"
    "需要的话跟我说一遍要做什么，我再问一次。"
)

INVALID_PICK_TEXT = ("我没看清你要处理的是哪一个（这张卡上的选择记号读不出来），"
                     "所以这次**一个任务都没有处理**。要处理的话跟我说一遍，我再问一次。")





QUOTA_BLOCKED_TEXT = (
    "额度用完啦（{limit} 轮终身额度，已经全部用掉了）……主人可以去个人中心的"
    "「对话额度」提交一份重置申请，管理员批准之后额度就会清零；在那之前我没法再"
    "回答新的问题了呢，抱歉喵 🐾"
)


def _quota_blocked_text(limit: int) -> str:
    """Return a deterministic refusal without tools or model calls."""
    if limit > 0:
        return QUOTA_BLOCKED_TEXT.format(limit=limit)
    return ("额度用完啦（终身额度已经全部用掉了）……主人可以去个人中心的"
            "「对话额度」提交一份重置申请，管理员批准之后额度就会清零；在那之前"
            "我没法再回答新的问题了呢，抱歉喵 🐾")



@app.post("/chat/stream/v2")
async def chat_stream(req: ChatRequest, request: Request):
    # request: FastAPI 注入的原始请求对象（req 是 body 模型）——断连感知用，
    # 见 event_stream 的 receive 监听任务（20260827b 断连中断修复）
    logger.info("POST /chat/stream/v2 user=%s msg=%r needs_summary=%s",
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
    # 要避免的"再走一轮对话"。只如实回一句，不调模型、不调工具。
    grant = None
    if req.confirm_token.strip():
        grant = confirm.verify(req.confirm_token, principal.uid, req.conversation_id)
        if grant is None:
            logger.warning("[confirm] 令牌验签失败（uid=%s conv=%s 长度=%d）→ 零执行",
                           principal.uid, req.conversation_id, len(req.confirm_token))
            _record_invalid_confirm(get_trace_id(), principal.uid,
                                    req.conversation_id, len(req.confirm_token))
            return stream_response(text_stream(INVALID_CONFIRM_TEXT))
        # 只办其中一件（20260929 批 F）：卡片列了 N 件时主人可以点「只办 1」——
        # 前端带回来的只是一个**下标**，收窄在**验签之后、任何消费之前**做
        # （`_build_messages` 与原生运行时读到的都必须是
        # 收窄后的那一份，否则会出现"卡上问一件、实际执行一批"）。收窄只可能让
        # 放行范围**变小**；记号读不懂/越界一律零执行（fail-closed），
        # 绝不"读不懂就当全部办"——那正是这一层唯一能出的重伤。
        if req.confirm_pick.strip():
            before = len(grant.get("specs") or [])
            grant, pick_err = confirm.narrow(grant, req.confirm_pick)
            if pick_err:
                logger.warning("[confirm] 挑选记号读不懂（uid=%s conv=%s pick=%r：%s）→ 零执行",
                               principal.uid, req.conversation_id,
                               req.confirm_pick[:32], pick_err)
                _record_invalid_confirm(get_trace_id(), principal.uid,
                                        req.conversation_id, len(req.confirm_token),
                                        reason="invalid_pick", detail=pick_err,
                                        exit_reason="invalid_confirm_pick")
                return stream_response(text_stream(INVALID_PICK_TEXT))
            logger.info("[confirm] 确认收窄：%d 件 → 1 件（%s）", before,
                        (grant.get("specs") or [{}])[0].get("tool"))
    # Rust 的额度裁决先于并发准入；remaining=0 不能代替 quota_blocked。
    if req.quota_blocked:
        _limit = req.chat_quota.limit if req.chat_quota is not None else 0
        logger.info("[quota] 额度用尽，本轮零执行（uid=%s limit=%s）", req.user_id, _limit)
        return stream_response(text_stream(_quota_blocked_text(_limit)))
    # The permit covers preparation and every worker job until actual completion.
    if not await _try_acquire_slot():
        raise HTTPException(503, "Agent busy（并发已满），请稍后重试")
    try:
        messages = _build_messages(req, confirm_grant=grant)
        # 每请求独立线程；挂了 saver ⇒ 会话坐标（两条路共用同一个函数，理由见 /chat 注释）
        thread_id = _thread_id_for(req)
        # producer 继承请求 trace 上下文，event_stream.finally 负责落盘。
        start_trace(get_trace_id(), req.user_id, thread_id, {
            "source": "production", "runtime": runtime_probe.request_context(_agent),
            "principal_role": principal.known_role, "principal_source": principal.source,
            "message": (req.message or "")[:200], "has_image": bool(req.image),
            "needs_summary": bool(req.needs_summary), "history_len": len(req.history),
            "conversation_id": req.conversation_id,
            "has_exec": bool(req.executions), "has_confirm": bool(req.confirm_token),
        })
    except Exception:
        _release_slot()
        raise

    stop_event = threading.Event()
    trace_id = get_trace_id()
    jobs = app.state.workers.request(asyncio.get_running_loop(), _release_slot,
                       control=RequestControl(
                           stop_event, time.monotonic() + STREAM_TOTAL_TIMEOUT),
                       on_complete=functools.partial(
                           finish_trace, trace_id, "client_closed", 0.0, 0))

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()  # 同上（event_stream 也是协程）
        progress = StreamProgress(loop, queue, jobs.control)
        started = progress.started

        # 摘要独立化：needs_summary 轮与流式生成并行做后端总结（输入为原始历史，
        # 不依赖模型回复），流结束时随 __SUMMARY__ 帧返回（Rust 解析入库）
        summary_task = None
        if req.needs_summary:
            summary_task = _submit_with_context(
                loop, _summarize_dialogue, req.message, req.history, req.summary,
                jobs=jobs
            )

        # The shared stop signal fences subsequent operations.
        _submit_with_context(
            loop, _run_agent_stream_to_queue, messages, thread_id, queue, loop,
            req.user_id, stop_event,
            principal, grant, req.conversation_id,
            None, "", None, progress.emitter, jobs=jobs,
        )

        # 可观测性：请求生命周期账本（帧数/退出原因，finally 汇总）
        frames = 0
        end_reason = "unknown"
        had_output = False
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
                        progress.mark("error")
                        yield sse_frame("error", "生成时间过长，请稍后重试")
                        return
                    if loop.time() - last_frame >= STREAM_IDLE_TIMEOUT:
                        end_reason = "idle_timeout"
                        logger.error("Chat stream idle timeout (%.0fs), aborting", elapsed)
                        progress.mark("error")
                        yield sse_frame("error", "服务响应超时，请稍后重试")
                        return
                    continue
                last_frame = loop.time()
                if chunk is None:
                    end_reason = "producer_done"
                    # No payload may follow the producer sentinel.
                    residue = queue.qsize()
                    if residue:
                        logger.error("[stream] 终止帧之后生产者又投了 %d 个帧（协议违约："
                                     "这些帧不会下发，检查 queue.put 是否晚于哨兵 None）", residue)
                    break
                if isinstance(chunk, _NativeCancelled):
                    end_reason = "cancelled"
                    return
                if isinstance(chunk, Exception):
                    end_reason = "producer_error"
                    # Preserve the worker exception traceback in server logs.
                    logger.error("Agent streaming failed: %s: %s",
                                 type(chunk).__name__, chunk, exc_info=chunk)
                    # Emit only the safe public message as a JSON payload.
                    progress.mark("error")
                    yield sse_frame("error", PRODUCER_ERROR_TEXT)
                    return
                if isinstance(chunk, str):
                    try:
                        kind, payload = control_event(chunk)
                    except ValueError:
                        end_reason = "producer_protocol_error"
                        progress.mark("error")
                        yield sse_frame("error", PRODUCER_ERROR_TEXT)
                        return
                    had_output |= kind in ("confirmation", "command")
                    frames += 1
                    if kind == "progress":
                        progress.mark("progress")
                    yield sse_frame(kind, payload)
                    continue
                if isinstance(chunk, AIMessageChunk) and chunk.content:
                    text = str(chunk.content)
                    had_output |= progress.reply(text)
                    frames += 1
                    yield sse_frame("text", text)

            # 空输出兜底：整轮无任何帧（qwen 偶发空内容）→ 补发人设内恢复语，
            # 前端不会静默无感知（Rust 空回复不存历史、UI 无任何反馈即"卡死"）
            if not had_output:
                logger.warning("Chat stream ended with no output "
                               "(agent produced no text/no command)")
                frames += 1
                progress.mark("reply")
                yield sse_frame("text", _RECOVERY_SENTENCE)

            # Send the independent summary before the terminal event.
            if summary_task is not None:
                new_summary = (await summary_task).strip()
                if new_summary:
                    yield sse_frame("summary", new_summary)

            yield sse_frame("done", {})
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
            jobs.on_complete = functools.partial(
                finish_trace, trace_id, end_reason, loop.time() - started, frames)

    async def managed_stream():
        body = event_stream()
        try:
            async for frame in body:
                yield frame
        finally:
            stop_event.set()
            try:
                await body.aclose()
            finally:
                jobs.close()

    return stream_response(managed_stream())


@app.get("/health")
async def health():
    """Report liveness and in-process configuration without credentials or service URLs."""
    from config.settings import settings
    # Diagnostics must stay available when configuration or the index is unavailable.
    try:
        _model = settings.active_llm_model
    except Exception:                               # noqa: BLE001
        _model = f"<认不出 provider={getattr(settings, 'llm_provider', '')!r}>"
    try:
        from rag.vector_index import route_status
        _rag = route_status()
    except Exception as exc:                        # noqa: BLE001
        logger.warning("/health 读向量档位失败（%s）——按未生效报", exc)
        _rag = {"enabled": bool(getattr(settings, "rag_hybrid_enabled", False)),
                "active": False, "missing": 0, "state": "unknown"}
    return {
        "status": "ok",
        "agent_ready": _agent is not None,
        "dials": {
            "agent_arm": runtime_probe.arm_name(_agent),
            "planner_native_thinking": False,
            "agent_fastpath": False,
            "llm_provider": getattr(settings, "llm_provider", ""),
            "llm_model": _model,
            "rag_hybrid_enabled": bool(_rag["enabled"]),
            "rag_hybrid_active": bool(_rag["active"]),
            "rag_state": str(_rag.get("state") or "unknown"),
            "vector_missing": int(_rag["missing"]),
            "checkpoint_backend": runtime_probe.checkpoint_backend(_agent),
            "checkpoint_db": runtime_probe.checkpoint_db(settings.checkpoint_dsn),
        },
    }


@app.delete("/checkpoint/{conversation_id}")
async def checkpoint_delete(conversation_id: str, request: Request, uid: int = 0):
    """Delete checkpoint state under the authenticated host cleanup protocol."""
    principal = _resolve_principal(request, uid)
    saver = checkpoint.live_saver(_agent)
    if saver is None:
        logger.info("[checkpoint] 级联删除跳过：本进程没挂 saver（conv=%s）", conversation_id)
        return {"deleted": False, "why": "no_saver"}
    if checkpoint.conversation_thread(conversation_id) is None:
        logger.warning("[checkpoint] 级联删除拒绝：会话 id 非法（%r）", conversation_id)
        return {"deleted": False, "why": "bad_conversation_id"}
    gone = await app.state.workers.run(checkpoint.delete_thread, saver, conversation_id)
    logger.info("[checkpoint] 级联删除 conv=%s deleted=%s by=%s",
                conversation_id, gone, principal.uid)
    return {"deleted": gone, "why": "" if gone else "still_readable"}


class DropTurnReq(BaseModel):
    """Original discarded user text and untrusted caller ID; empty text never deletes a turn."""
    text: str = ""
    uid: int = Field(default=0, ge=0)


@app.post("/checkpoint/{conversation_id}/drop_turn")
async def checkpoint_drop_turn(conversation_id: str, req: DropTurnReq, request: Request):
    """Remove only the matching last user turn and verify the resulting checkpoint."""
    principal = _resolve_principal(request, req.uid)
    out = await app.state.workers.run(
        checkpoint.drop_last_user_turn, _agent, conversation_id, req.text)
    logger.info("[checkpoint] 丢弃那一轮 conv=%s dropped=%s why=%s by=%s",
                conversation_id, out.get("dropped"), out.get("why"), principal.uid)
    return out


@app.get("/version")
async def version():
    """Report immutable build identity separately from live configuration."""
    return runtime_probe.version_payload(_app_version())


@app.get("/ready")
async def ready():
    """Report runtime and credential readiness without making a model request."""
    from config.settings import settings
    code, body = runtime_probe.ready_payload(_agent, settings)
    if app.state.workers is not None and app.state.workers.closing:
        code, body["ready"] = 503, False
        body["reasons"].append("shutting_down")
    return JSONResponse(body, status_code=code)


if __name__ == "__main__":
    import uvicorn
    # 机器内存有限（3.7GB），4 个 worker 会周期性被系统杀掉导致对话连接中断；
    # 2 个 worker + 每 worker 8 线程 executor 足够博客并发，且更稳定
    uvicorn.run(app, host="127.0.0.1", port=8010, workers=2)
