"""ASGI application: uvicorn agent.main:app --host 127.0.0.1 --port 8010."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from fastapi import FastAPI

from agent.api.lifecycle import lifespan
from agent.api.middleware import body_limit_middleware, trace_id_middleware
from agent.api.routes import (
    chat,
    conversations,
    knowledge_graph,
    moderation,
    operations,
    task_inputs,
)


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
    application.middleware("http")(trace_id_middleware)
    application.middleware("http")(body_limit_middleware)
    for router in (
        chat.router,
        conversations.router,
        task_inputs.router,
        knowledge_graph.router,
        moderation.router,
        operations.router,
    ):
        application.include_router(router)
    return application


app = create_app()
