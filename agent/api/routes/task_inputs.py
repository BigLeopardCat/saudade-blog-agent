"""Task controls authenticate independently and never wait for the turn lock."""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agent.api.authentication import resolve_principal
from agent.api.dependencies import require_runtime, require_workers
from agent.runtime.task_inputs import MAX_TEXT, InputConflict, binding

router = APIRouter()


class InputRequest(BaseModel):
    user_id: int = Field(gt=0)
    conversation_id: int = Field(gt=0)
    task_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    input_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=MAX_TEXT)


class StatusRequest(BaseModel):
    user_id: int = Field(gt=0)
    conversation_id: int = Field(gt=0)
    task_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class ConfirmationRequest(BaseModel):
    user_id: int = Field(gt=0)
    conversation_id: int = Field(gt=0)
    task_id: str | None = Field(default=None, min_length=1, max_length=100,
                                pattern=r"^[A-Za-z0-9_-]+$")


async def operation(request, req, action, *args):
    principal = resolve_principal(request, req.user_id)
    inbox = require_runtime(request).inputs
    if inbox is None:
        raise HTTPException(503, "task_inputs_unavailable")
    try:
        scope = binding(principal, req.conversation_id)
        return await require_workers(request).run(action, req.task_id, scope, *args)
    except (ValueError, InputConflict) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/tasks/input")
async def submit(req: InputRequest, request: Request):
    inbox = require_runtime(request).inputs
    if inbox is None:
        raise HTTPException(503, "task_inputs_unavailable")
    item = await operation(request, req, inbox.submit, req.input_id, req.text)
    state = await operation(request, req, inbox.read)
    return {**state, "input": item}


@router.post("/tasks/inputs/status")
async def status(req: StatusRequest, request: Request):
    inbox = require_runtime(request).inputs
    if inbox is None:
        raise HTTPException(503, "task_inputs_unavailable")
    return await operation(request, req, inbox.read)


@router.post("/tasks/confirmation")
async def confirmation_status(req: ConfirmationRequest, request: Request):
    from agent.products.blog.confirmation import current_card

    principal = resolve_principal(request, req.user_id)
    result = await require_workers(request).run(
        current_card, require_runtime(request), principal, req.conversation_id, req.task_id)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/tasks/paused")
async def paused(request: Request, user_id: int = Query(gt=0), limit: int = Query(20, ge=1, le=20)):
    from agent.runtime.task_management import list_paused

    principal = resolve_principal(request, user_id)
    try:
        result = await require_workers(request).run(
            list_paused, require_runtime(request), principal, limit)
    except PermissionError as exc:
        raise HTTPException(403, "verified identity required") from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/tasks/cancel")
async def cancel_paused_task(req: StatusRequest, request: Request):
    from agent.runtime.task_management import cancel_paused

    principal = resolve_principal(request, req.user_id)
    try:
        result = await require_workers(request).run(
            cancel_paused, require_runtime(request), principal, req.conversation_id, req.task_id)
    except (InputConflict, TimeoutError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
