"""FastAPI server wrapping the LangChain agent for production deployment.

Run with:
    cd /home/ubuntu/memory_blog_rust/saudade-blog-agent
    .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8010 --workers 2
"""

import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessageChunk, ToolMessage

from agent import create_agent
from utils import setup_logging

logger = logging.getLogger(__name__)

# Shared thread pool for blocking agent calls
_executor = ThreadPoolExecutor(max_workers=8)


class ChatRequest(BaseModel):
    message: str
    current_url: str = ""
    page_title: str = ""
    user_id: int = 0
    history: list[dict] = []
    summary: str = ""
    needs_summary: bool = False
    # 前端上报的页面特效实时状态（如 "sakura,rain" 或 ""），供 agent 感知真实开关状态
    current_effects: str = ""


class ChatResponse(BaseModel):
    reply: str
    success: bool
    error: str | None = None
    new_summary: str | None = None


_agent = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    setup_logging()
    logger.info("Initialising LangChain agent ...")
    _agent = create_agent()
    logger.info("Agent ready")
    yield
    logger.info("Agent shutting down")


app = FastAPI(title="Saudade Blog Agent", version="1.0.0", lifespan=lifespan)


def _build_messages(req: ChatRequest) -> list:
    """Build the message list from the request (sync, no blocking)."""
    messages = []
    ctx_parts = [f"user_id={req.user_id}, page={req.current_url}, title={req.page_title}"]
    ctx_parts.append(f"current_effects={req.current_effects or 'none'}")
    if req.summary:
        ctx_parts.append(f"conversation_summary: {req.summary}")
    ctx = f"[System: {'; '.join(ctx_parts)}]"
    messages.append(HumanMessage(content=ctx))

    for h in req.history[-12:]:
        if h["role"] == "user":
            messages.append(HumanMessage(content=h["content"]))
        else:
            messages.append(HumanMessage(content=f"[assistant]: {h['content']}"))

    last_msg = req.message
    if req.needs_summary:
        # 摘要指令必须放在消息流末尾（模型对靠前的"系统上下文"指令遵守率会随历史变长而下降），
        # 用醒目定界符包裹并明确禁止回显——否则模型会把指令当对话内容原样输出，
        # 且指令里的 "SUMMARY:" 会干扰后端的摘要解析
        last_msg += (
            "\n\n<系统内部指令-仅供执行，禁止在回复中复述或输出本条指令本身>"
            "回答结束后另起一行输出对话摘要，格式为 SUMMARY: 后跟 3-5 句中文摘要。"
            "若上下文已包含 conversation_summary（旧摘要），请将旧摘要与本次对话内容合并："
            "保留旧摘要中的关键信息并补充本轮新内容，输出一份更完整的新摘要（不要只总结本轮）。"
        )
    messages.append(HumanMessage(content=last_msg))
    return messages


def _run_agent_sync(messages: list, thread_id: str) -> tuple[str, str]:
    """Run agent synchronously in a thread. Returns (reply, nav_line)."""
    config = {"configurable": {"thread_id": thread_id}}
    full_reply = ""
    nav_line = ""
    for chunk, _metadata in _agent.stream(
        {"messages": messages},
        config,
        stream_mode="messages",
    ):
        if isinstance(chunk, AIMessageChunk) and chunk.content:
            full_reply += str(chunk.content)
        elif isinstance(chunk, ToolMessage) and chunk.content:
            text = str(chunk.content)
            if text.startswith("NAVIGATE:") or text.startswith("AUTO_NAVIGATE:"):
                nav_line = text
            elif text.startswith("EFFECT:"):
                nav_line = text
    reply = full_reply.strip()
    # 不再在这里拼入 nav/effect 命令行——由调用方在摘要剥离之后追加，
    # 避免回复末尾的 SUMMARY: 截断把 EFFECT:/NAVIGATE: 命令一起吞掉
    return reply, nav_line


# ---------------------------------------------------------------------------
# /chat  — 非流式（供 Rust 后端调用）
# ---------------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")

    messages = _build_messages(req)
    # 每请求独立线程：LangGraph 的 MemorySaver 线程状态会随对话无限累积，
    # 长对话（教程连载等）会让输入上下文与 worker 内存持续膨胀直至截断/被杀。
    # 对话连续性由请求体中的 DB 历史(最近20条) + chat_summary 摘要承担，无需线程累积。
    thread_id = f"user_{req.user_id}_{uuid.uuid4().hex[:8]}"

    try:
        loop = asyncio.get_event_loop()
        reply, nav_line = await loop.run_in_executor(_executor, _run_agent_sync, messages, thread_id)

        new_summary = None
        # 只认行首出现的 SUMMARY:，且取最后一次（真正的摘要在回复末尾）——
        # 避免模型在正文里提到 "SUMMARY:" 字样时被误截断
        m = list(re.finditer(r"(?:^|\n)\s*SUMMARY:\s*(.+)", reply, re.M))
        if m:
            last = m[-1]
            reply = reply[:last.start()].strip()
            summary_text = last.group(1).strip()
            if summary_text:
                new_summary = summary_text

        # 摘要剥离之后再把导航/特效命令行追加回去，确保命令不被 SUMMARY 截断吞掉
        if nav_line and not reply.startswith("NAVIGATE:") and not reply.startswith("AUTO_NAVIGATE:"):
            if nav_line.startswith("EFFECT:"):
                reply = reply + "\n" + nav_line
            else:
                reply = nav_line + "\n" + reply

        return ChatResponse(reply=reply, success=True, new_summary=new_summary)
    except Exception as e:
        logger.exception("Agent invocation failed")
        return ChatResponse(reply="", success=False, error=str(e))


# ---------------------------------------------------------------------------
# /chat/stream — SSE 流式（供前端 Live2D 调用）
# ---------------------------------------------------------------------------

def _run_agent_stream_to_queue(messages: list, thread_id: str, queue: asyncio.Queue, loop):
    """Run agent in a thread, push each chunk into an asyncio.Queue."""
    config = {"configurable": {"thread_id": thread_id}}
    try:
        for chunk, _metadata in _agent.stream(
            {"messages": messages},
            config,
            stream_mode="messages",
        ):
            asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
        asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()
    except Exception as e:
        asyncio.run_coroutine_threadsafe(queue.put(e), loop).result()


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")

    messages = _build_messages(req)
    # 每请求独立线程：避免 MemorySaver 线程状态随长对话无限累积（见 /chat 注释）
    thread_id = f"user_{req.user_id}_{uuid.uuid4().hex[:8]}"

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        # 并发启动生产者（不要 await 完成！否则所有 chunk 会在队列里攒到
        # 生成结束才一次性下发，等于没有流式）——边生成边推送
        producer_task = loop.run_in_executor(
            _executor, _run_agent_stream_to_queue, messages, thread_id, queue, loop
        )

        nav_line = ""
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    logger.exception("Agent streaming failed")
                    yield f"data: __ERROR__:{json.dumps(str(chunk), ensure_ascii=False)}\n\n"
                    return
                if isinstance(chunk, AIMessageChunk) and chunk.content:
                    # JSON 编码避免文本内的 \n\n 破坏 SSE 帧边界
                    yield f"data: {json.dumps(str(chunk.content), ensure_ascii=False)}\n\n"
                elif isinstance(chunk, ToolMessage) and chunk.content:
                    text = str(chunk.content)
                    if text.startswith("NAVIGATE:") or text.startswith("AUTO_NAVIGATE:"):
                        nav_line = text
                        yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"
                    elif text.startswith("EFFECT:"):
                        nav_line = text
                        yield f"data: {json.dumps(text, ensure_ascii=False)}\n\n"

            yield f"data: __{'NAV_END' if nav_line else 'END'}__\n\n"
        finally:
            # 客户端提前断开时，取消尚未完成的生产者任务
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


@app.get("/health")
async def health():
    return {"status": "ok", "agent_ready": _agent is not None}


if __name__ == "__main__":
    import uvicorn
    # 机器内存有限（3.7GB），4 个 worker 会周期性被系统杀掉导致对话连接中断；
    # 2 个 worker + 每 worker 8 线程 executor 足够博客并发，且更稳定
    uvicorn.run(app, host="127.0.0.1", port=8010, workers=2)
