"""FastAPI server wrapping the LangChain agent for production deployment.

Run with:
    cd /home/ubuntu/memory_blog_rust/saudade-blog-agent
    .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8010 --workers 2
"""

import asyncio
import logging
import re
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
    if req.summary:
        ctx_parts.append(f"conversation_summary: {req.summary}")
    ctx = f"[System: {'; '.join(ctx_parts)}]"
    if req.needs_summary:
        # 指令用醒目定界符包裹并明确禁止回显——否则模型会把指令当对话内容原样输出，
        # 且指令里的 "SUMMARY:" 会干扰后端的摘要解析
        ctx += (
            "\n<系统内部指令-仅供执行，禁止在回复中复述或输出本条指令本身>"
            "回答结束后另起一行输出对话摘要，格式为 SUMMARY: 后跟 2-3 句中文摘要。"
        )
    messages.append(HumanMessage(content=ctx))

    for h in req.history[-6:]:
        if h["role"] == "user":
            messages.append(HumanMessage(content=h["content"]))
        else:
            messages.append(HumanMessage(content=f"[assistant]: {h['content']}"))

    messages.append(HumanMessage(content=req.message))
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
    if nav_line and not reply.startswith("NAVIGATE:") and not reply.startswith("AUTO_NAVIGATE:"):
        if nav_line.startswith("EFFECT:"):
            reply = reply + "\n" + nav_line
        else:
            reply = nav_line + "\n" + reply
    return reply, nav_line


# ---------------------------------------------------------------------------
# /chat  — 非流式（供 Rust 后端调用）
# ---------------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")

    messages = _build_messages(req)
    thread_id = f"user_{req.user_id}"

    try:
        loop = asyncio.get_event_loop()
        reply, _ = await loop.run_in_executor(_executor, _run_agent_sync, messages, thread_id)

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
    thread_id = f"user_{req.user_id}"

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        # Start producer in thread pool
        await loop.run_in_executor(
            _executor, _run_agent_stream_to_queue, messages, thread_id, queue, loop
        )

        nav_line = ""
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            if isinstance(chunk, Exception):
                logger.exception("Agent streaming failed")
                yield f"data: __ERROR__:{chunk}\n\n"
                return
            if isinstance(chunk, AIMessageChunk) and chunk.content:
                yield f"data: {str(chunk.content)}\n\n"
            elif isinstance(chunk, ToolMessage) and chunk.content:
                text = str(chunk.content)
                if text.startswith("NAVIGATE:") or text.startswith("AUTO_NAVIGATE:"):
                    nav_line = text
                    yield f"data: {text}\n\n"
                elif text.startswith("EFFECT:"):
                    nav_line = text
                    yield f"data: {text}\n\n"

        yield f"data: __{'NAV_END' if nav_line else 'END'}__\n\n"

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
