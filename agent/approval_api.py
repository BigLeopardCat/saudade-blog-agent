"""Authenticated approval lifecycle endpoints; no model or chat turn is created."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agent import checkpoint, confirm
from agent.runtime import confirmation
from agent.runtime.checkpoint_store import ConversationDeleted


class Cancellation(BaseModel):
    user_id: int = Field(gt=0)
    conversation_id: int = Field(gt=0)
    token: str = Field(min_length=1, max_length=confirm.MAX_TOKEN_CHARS)


def router(get_runtime, resolve_principal):
    api = APIRouter()

    @api.post("/confirmation/cancel")
    async def cancel(req: Cancellation, request: Request):
        principal = resolve_principal(request, req.user_id)
        grant = confirm.verify(req.token, principal.uid, req.conversation_id)
        if grant is None:
            raise HTTPException(403, "确认凭据无效或已过期")
        runtime = get_runtime()
        workers = getattr(request.app.state, "workers", None)
        if runtime is None or workers is None or workers.closing:
            raise HTTPException(503, "Agent not initialised")
        config = {"configurable": {
            "principal": principal, "user_id": principal.uid,
            "conversation_id": req.conversation_id,
            "thread_id": checkpoint.conversation_thread(req.conversation_id)}}
        try:
            result = await workers.run(confirmation.cancel, runtime, grant, config)
        except (ValueError, ConversationDeleted) as exc:
            raise HTTPException(409, str(exc)) from exc
        return result

    return api
