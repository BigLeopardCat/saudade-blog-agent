"""Authenticated chat endpoints with request-scoped runtime and work ownership."""

import asyncio
import functools
import logging
import threading
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from langchain_core.messages import AIMessageChunk

from agent import checkpoint, confirm, runtime_probe
from agent.api.authentication import resolve_principal as _resolve_principal
from agent.api.context import build_messages
from agent.api.dependencies import release_slot, require_runtime, require_workers, try_acquire_slot
from agent.api.events import control_event, text_stream
from agent.api.events import frame as sse_frame
from agent.api.events import response as stream_response
from agent.api.schemas import ChatRequest, ChatResponse, SummaryUpdate
from agent.api.turns import NativeCancelled, run_turn, stream_to_queue
from agent.products.blog.context import conversation_session
from agent.runtime.control import AgentCancelled, RequestControl
from agent.runtime.memory_scope import safe_request
from agent.runtime.progress import StreamProgress
from agent.summarizer import summarize
from config.settings import settings
from utils.logging import get_trace_id
from utils.trace import finish_trace, record, start_trace

logger = logging.getLogger(__name__)
router = APIRouter()
STREAM_IDLE_TIMEOUT = settings.stream_idle_timeout
STREAM_TOTAL_TIMEOUT = settings.stream_total_timeout
PRODUCER_ERROR_TEXT = "服务这边出了点问题，这一轮没能生成回复，请再说一次。"
_RECOVERY_SENTENCE = "喵呜……主人抱歉，泠月喵刚才脑袋卡壳了，没有生成出回复，请主人再问一遍喵～ 🐾"


def _record_invalid_confirm(
    trace_id: str,
    uid: int,
    conv_id,
    token_len: int,
    reason: str = "invalid_token",
    detail: str = "",
    exit_reason: str = "invalid_confirm_token",
) -> None:
    """Record a refusal without serializing confirmation credentials."""
    try:
        start_trace(
            trace_id,
            uid,
            f"invalid_confirm_{uuid.uuid4().hex[:8]}",
            confirm.invalid_trace_meta(uid, conv_id, token_len),
        )
        record(
            "confirm",
            "rejected",
            reason=reason,
            conversation_id=conv_id,
            token_len=int(token_len),
            detail=str(detail or "")[:120],
        )
        finish_trace(trace_id, exit_reason, 0.0, 1)
    except Exception:  # trace 是观测，不是业务：绝不因它中断
        logger.exception("invalid confirm trace dump failed")


INVALID_CONFIRM_TEXT = (
    "这次确认已经失效了（超过 10 分钟、或者不是在同一个会话里点的），我没有执行任何改动。"
    "需要的话跟我说一遍要做什么，我再问一次。"
)

INVALID_PICK_TEXT = (
    "我没看清你要处理的是哪一个（这张卡上的选择记号读不出来），"
    "所以这次**一个任务都没有处理**。要处理的话跟我说一遍，我再问一次。"
)


QUOTA_BLOCKED_TEXT = (
    "额度用完啦（{limit} 轮终身额度，已经全部用掉了）……主人可以去个人中心的"
    "「对话额度」提交一份重置申请，管理员批准之后额度就会清零；在那之前我没法再"
    "回答新的问题了呢，抱歉喵 🐾"
)


def _quota_blocked_text(limit: int) -> str:
    """Return a deterministic refusal without tools or model calls."""
    if limit > 0:
        return QUOTA_BLOCKED_TEXT.format(limit=limit)
    return (
        "额度用完啦（终身额度已经全部用掉了）……主人可以去个人中心的"
        "「对话额度」提交一份重置申请，管理员批准之后额度就会清零；在那之前"
        "我没法再回答新的问题了呢，抱歉喵 🐾"
    )


async def _refresh_summary(req, jobs, workers):
    """Maintain a derived cache only after the primary task has completed."""
    if not req.needs_summary or req.summary_batch is None:
        return None
    try:
        text = await jobs.submit(workers.executor, summarize, "",
                                 req.summary_batch.messages, req.summary)
    except (AgentCancelled, TimeoutError, RuntimeError) as error:
        record("summarizer", "skipped", reason=type(error).__name__)
        return None
    if not text.strip():
        return None
    batch = req.summary_batch
    record("summarizer", "coverage", base_id=batch.base_id, through_id=batch.through_id,
           messages=len(batch.messages))
    return SummaryUpdate(text=text.strip(), base_id=batch.base_id, through_id=batch.through_id)


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    runtime = require_runtime(request)
    workers = require_workers(request)
    principal = _resolve_principal(request, req.user_id)
    req.user_id = principal.uid
    req = safe_request(req, principal)

    if req.quota_blocked:
        _limit = req.chat_quota.limit if req.chat_quota is not None else 0
        logger.info("[quota] 额度用尽，本轮零执行（uid=%s limit=%s）", req.user_id, _limit)
        return ChatResponse(reply=_quota_blocked_text(_limit), success=True, error=None)

    messages = build_messages(req, holds_context=runtime_probe.holds_context(runtime))
    thread_id = checkpoint.thread_id_for(
        req.user_id, req.conversation_id, runtime_probe.holds_context(runtime)
    )

    if not await try_acquire_slot(request):
        raise HTTPException(503, "Agent busy（并发已满），请稍后重试")
    loop = asyncio.get_running_loop()
    jobs = workers.request(
        loop,
        functools.partial(release_slot, request),
        control=RequestControl(threading.Event(), time.monotonic() + STREAM_TOTAL_TIMEOUT,
                               session_id=conversation_session(principal, req.conversation_id)),
    )
    try:
        turn = (functools.partial(run_turn, task_id=req.task_id, resume_task=req.resume_task)
                if req.task_id or req.resume_task else run_turn)
        reply, nav_line, exec_rows = await jobs.submit(
            workers.executor,
            turn,
            runtime,
            messages,
            thread_id,
            req.user_id,
            principal,
            req.conversation_id,
        )
        update = await _refresh_summary(req, jobs, workers) if reply.strip() else None

        if not reply.strip():
            logger.warning("Agent returned empty reply for message=%r", req.message[:60])
        final_reply = reply.strip() or _RECOVERY_SENTENCE

        final_nav = nav_line
        if final_nav and not final_reply.startswith(("NAVIGATE:", "AUTO_NAVIGATE:")):
            if final_nav.startswith("EFFECT:"):
                final_reply = final_reply + "\n" + final_nav
            else:
                final_reply = final_nav + "\n" + final_reply

        return ChatResponse(
            reply=final_reply, success=True, new_summary=update.text if update else None,
            summary_coverage=({"base_id": update.base_id, "through_id": update.through_id}
                              if update else None), executions=exec_rows
        )
    except Exception:
        logger.exception("Agent invocation failed")
        return ChatResponse(reply=PRODUCER_ERROR_TEXT, success=False, error=PRODUCER_ERROR_TEXT)

    finally:
        jobs.close()


async def _summary_events(req, jobs, workers, had_output, awaiting_confirmation):
    """A prepared card finishes promptly; derived memory can advance on a later turn."""
    if awaiting_confirmation:
        if req.needs_summary:
            record("summary", "deferred", reason="awaiting_confirmation")
        return
    if had_output and req.needs_summary:
        yield sse_frame("progress", "正在整理会话摘要…")
    update = await _refresh_summary(req, jobs, workers) if had_output else None
    if req.needs_summary:
        yield sse_frame("progress", "会话摘要已更新。" if update else "本轮保留原有会话摘要。")
    if update:
        yield sse_frame("summary", update.model_dump())


@router.post("/chat/stream/v2")
async def chat_stream(req: ChatRequest, request: Request):
    logger.info(
        "POST /chat/stream/v2 user=%s msg=%r needs_summary=%s",
        req.user_id,
        (req.message or "")[:40],
        req.needs_summary,
    )
    runtime = require_runtime(request)
    workers = require_workers(request)
    principal = _resolve_principal(request, req.user_id)  # 身份以签名为准（见 _resolve_principal）
    req.user_id = principal.uid
    req = safe_request(req, principal)
    grant = None
    if req.confirm_token.strip():
        grant = confirm.verify(req.confirm_token, principal.uid, req.conversation_id)
        if grant is None:
            logger.warning(
                "[confirm] 令牌验签失败（uid=%s conv=%s 长度=%d）→ 零执行",
                principal.uid,
                req.conversation_id,
                len(req.confirm_token),
            )
            _record_invalid_confirm(
                get_trace_id(), principal.uid, req.conversation_id, len(req.confirm_token)
            )
            return stream_response(text_stream(INVALID_CONFIRM_TEXT))
        if req.confirm_pick.strip():
            before = len(grant.get("specs") or [])
            grant, pick_err = confirm.narrow(grant, req.confirm_pick)
            if pick_err or grant is None:
                logger.warning(
                    "[confirm] 挑选记号读不懂（uid=%s conv=%s pick=%r：%s）→ 零执行",
                    principal.uid,
                    req.conversation_id,
                    req.confirm_pick[:32],
                    pick_err,
                )
                _record_invalid_confirm(
                    get_trace_id(),
                    principal.uid,
                    req.conversation_id,
                    len(req.confirm_token),
                    reason="invalid_pick",
                    detail=pick_err,
                    exit_reason="invalid_confirm_pick",
                )
                return stream_response(text_stream(INVALID_PICK_TEXT))
            logger.info(
                "[confirm] 确认收窄：%d 件 → 1 件（%s）",
                before,
                (grant.get("specs") or [{}])[0].get("tool"),
            )
    if req.quota_blocked:
        _limit = req.chat_quota.limit if req.chat_quota is not None else 0
        logger.info("[quota] 额度用尽，本轮零执行（uid=%s limit=%s）", req.user_id, _limit)
        return stream_response(text_stream(_quota_blocked_text(_limit)))
    if not await try_acquire_slot(request):
        raise HTTPException(503, "Agent busy（并发已满），请稍后重试")
    try:
        messages = ([] if grant is not None or req.resume_task else
                    build_messages(req, holds_context=runtime_probe.holds_context(runtime)))
        thread_id = checkpoint.thread_id_for(
            req.user_id, req.conversation_id, runtime_probe.holds_context(runtime)
        )
        start_trace(
            get_trace_id(),
            req.user_id,
            thread_id,
            {
                "source": "production",
                "runtime": runtime_probe.request_context(runtime),
                "principal_role": principal.known_role,
                "principal_source": principal.source,
                "message": (req.message or "")[:200],
                "has_image": bool(req.image),
                "needs_summary": bool(req.needs_summary),
                "history_len": len(req.history),
                "conversation_id": req.conversation_id,
                "has_exec": bool(req.executions),
                "has_confirm": bool(req.confirm_token),
            },
        )
    except Exception:
        release_slot(request)
        raise

    stop_event = threading.Event()
    trace_id = get_trace_id()
    jobs = workers.request(
        asyncio.get_running_loop(),
        functools.partial(release_slot, request),
        control=RequestControl(stop_event, time.monotonic() + STREAM_TOTAL_TIMEOUT,
                               session_id=conversation_session(principal, req.conversation_id)),
        on_complete=functools.partial(finish_trace, trace_id, "client_closed", 0.0, 0),
    )

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()  # 同上（event_stream 也是协程）
        progress = StreamProgress(loop, queue, jobs.control)
        started = progress.started

        producer = (functools.partial(stream_to_queue, task_id=req.task_id,
                                      resume_task=req.resume_task,
                                      browser_feedback=req.browser_feedback,
                                      browser_result=(req.browser_result.model_dump()
                                                      if req.browser_result else None))
                    if req.task_id or req.resume_task or req.browser_feedback else stream_to_queue)
        jobs.submit(
            workers.executor,
            producer,
            runtime,
            messages,
            thread_id,
            queue,
            loop,
            req.user_id,
            stop_event,
            principal,
            grant,
            req.conversation_id,
            progress.emitter,
        )

        frames = 0
        end_reason = "unknown"
        had_output = False
        awaiting_confirmation = False
        last_frame = started
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
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
                    residue = queue.qsize()
                    if residue:
                        logger.error(
                            "[stream] 终止帧之后生产者又投了 %d 个帧（协议违约："
                            "这些帧不会下发，检查 queue.put 是否晚于哨兵 None）",
                            residue,
                        )
                    break
                if isinstance(chunk, NativeCancelled):
                    end_reason = "cancelled"
                    return
                if isinstance(chunk, Exception):
                    end_reason = "producer_error"
                    logger.error(
                        "Agent streaming failed: %s: %s",
                        type(chunk).__name__,
                        chunk,
                        exc_info=chunk,
                    )
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
                    awaiting_confirmation |= kind == "confirmation"
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

            if not had_output:
                logger.warning(
                    "Chat stream ended with no output (agent produced no text/no command)"
                )
                frames += 1
                progress.mark("reply")
                yield sse_frame("text", _RECOVERY_SENTENCE)

            async for frame in _summary_events(req, jobs, workers, had_output,
                                                awaiting_confirmation):
                yield frame

            yield sse_frame("done", {})
        except asyncio.CancelledError:
            end_reason = "client_disconnect"
            logger.info("[stream] client disconnected (starlette cancel), aborting")
            raise
        except Exception:
            end_reason = "yield_failed"
            logger.exception("[stream] stream delivery failed")
        finally:
            if end_reason == "unknown":
                end_reason = "client_closed"
            logger.info(
                "[stream] end reason=%s duration=%.1fs frames=%d",
                end_reason,
                loop.time() - started,
                frames,
            )
            jobs.on_complete = functools.partial(
                finish_trace, trace_id, end_reason, loop.time() - started, frames
            )

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
