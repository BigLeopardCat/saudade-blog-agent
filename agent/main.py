"""ASGI application: uvicorn agent.main:app --host 127.0.0.1 --port 8010."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agent.api.lifecycle import lifespan
from agent.api.middleware import BodyLimitMiddleware, trace_id_middleware
from agent.api.routes import (
    automations,
    chat,
    chat_runs,
    conversations,
    knowledge_graph,
    moderation,
    operations,
    task_inputs,
)
from agent.runtime.conversation_store import ConversationAccessDenied


def app_version() -> str:
    try:
        return _pkg_version("saudade-agent")
    except PackageNotFoundError:
        return "0.0.0+source"


def create_app() -> FastAPI:
    application = FastAPI(title="Saudade Blog Agent", version=app_version(), lifespan=lifespan)
    application.state.runtime = None
    application.state.workers = None
    application.state.chat_slots = None
    application.state.scheduling = None
    application.add_middleware(BodyLimitMiddleware)
    application.middleware("http")(trace_id_middleware)

    @application.exception_handler(ConversationAccessDenied)
    async def conversation_denied(request, exc):
        return JSONResponse({"detail": "conversation_not_found"}, status_code=404,
                            headers={"Cache-Control": "no-store"})

    for router in (
        chat.router,
        chat_runs.router,
        automations.router,
        conversations.router,
        task_inputs.router,
        knowledge_graph.router,
        moderation.router,
        operations.router,
    ):
        application.include_router(router)
    return application


app = create_app()
