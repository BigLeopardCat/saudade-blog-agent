"""One application's runtime, admission and worker resource lifecycle."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent import checkpoint
from agent.runtime.workers import RequestWorkers
from config.settings import settings
from rag import search as rag_search
from rag import wordgraph
from utils.logging import setup_logging

logger = logging.getLogger(__name__)
MAX_CONCURRENT_STREAMS = settings.max_concurrent_streams
# Retained configuration export; NativeRuntime uses RequestBudget instead.
RECURSION_LIMIT = settings.recursion_limit


def build_runtime():
    """Build the native production runtime; retired selections fail at startup."""
    from agent.runtime.native import build

    arm = (settings.agent_arm or "native").strip().lower()
    if arm != "native":
        raise RuntimeError(f"AGENT_ARM={arm!r} 已退役，请配置 AGENT_ARM=native。")
    return build(), "native"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the runtime after validating settings, then warm optional retrieval services."""
    setup_logging()
    logger.info("Initialising LangChain agent ...")
    from config.settings import settings

    app.state.chat_slots = asyncio.BoundedSemaphore(MAX_CONCURRENT_STREAMS)
    workers = RequestWorkers(request_timeout=settings.stream_total_timeout)
    app.state.workers = workers
    try:
        _provider = settings.check_provider()
        logger.info("LLM provider ready (provider=%s)", _provider)
        # Missing credentials keep liveness available; readiness reports the failure.
        if not settings.is_api_key_configured:
            logger.warning(
                "LLM 凭据缺失：provider=%s 的 api_key 为空 —— 进程会照常启动，"
                "但每一条对话都会失败（/ready 现在报 missing_credentials）。",
                _provider,
            )
        app.state.runtime, _arm = build_runtime()
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
            app.state.runtime = None
            app.state.workers = None
            await asyncio.to_thread(checkpoint.close_pool)
        logger.info("Agent stopped; request workers and checkpoint pool closed")
