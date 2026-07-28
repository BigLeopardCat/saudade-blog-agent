"""FastAPI server wrapping the LangChain agent for production deployment.

Run with:
    cd /home/ubuntu/memory_blog_rust/saudade-blog-agent
    .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8010
"""

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessageChunk

from agent import create_agent
from utils import setup_logging

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str
    current_url: str = ""
    page_title: str = ""
    user_id: int = 0
    history: list[dict] = []


class ChatResponse(BaseModel):
    reply: str
    success: bool
    error: str | None = None


_agent = None
_session_id = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent, _session_id
    setup_logging()
    logger.info("Initialising LangChain agent ...")
    _agent = create_agent()
    _session_id = str(uuid.uuid4())
    logger.info(f"Agent ready, session={_session_id}")
    yield
    logger.info("Agent shutting down")


app = FastAPI(title="Saudade Blog Agent", version="1.0.0", lifespan=lifespan)


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")

    messages = []
    ctx = f"[System: user_id={req.user_id}, page={req.current_url}, title={req.page_title}]"
    messages.append(HumanMessage(content=ctx))

    for h in req.history[-6:]:
        if h["role"] == "user":
            messages.append(HumanMessage(content=h["content"]))
        else:
            messages.append(HumanMessage(content=f"[assistant]: {h['content']}"))

    messages.append(HumanMessage(content=req.message))

    config = {"configurable": {"thread_id": _session_id}}
    full_reply = ""

    try:
        for chunk, _metadata in _agent.stream(
            {"messages": messages},
            config,
            stream_mode="messages",
        ):
            if isinstance(chunk, AIMessageChunk) and chunk.content:
                full_reply += str(chunk.content)

        return ChatResponse(reply=full_reply.strip(), success=True)
    except Exception as e:
        logger.exception("Agent invocation failed")
        return ChatResponse(reply="", success=False, error=str(e))


@app.get("/health")
async def health():
    return {"status": "ok", "agent_ready": _agent is not None}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8010)
