"""One application's runtime, admission and worker resource lifecycle."""

import asyncio
import logging
from contextlib import ExitStack, asynccontextmanager

from fastapi import FastAPI

from agent.runtime.workers import RequestWorkers
from config.settings import settings
from rag import search as rag_search
from rag import wordgraph
from utils.logging import setup_logging

logger = logging.getLogger(__name__)
MAX_CONCURRENT_STREAMS = settings.max_concurrent_streams


def build_runtime(resources: ExitStack):
    """Build the only supported conversation runtime."""
    from agent.runtime.native import build

    return build(resources=resources)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the runtime after validating settings, then warm optional retrieval services."""
    setup_logging()
    logger.info("Initialising LangChain agent ...")
    from config.settings import settings

    app.state.chat_slots = asyncio.BoundedSemaphore(MAX_CONCURRENT_STREAMS)
    resources = ExitStack()
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
        app.state.runtime = build_runtime(resources)
        logger.info("Native agent ready")
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
            await asyncio.to_thread(resources.close)
        logger.info("Agent stopped; request workers and checkpoint pool closed")
