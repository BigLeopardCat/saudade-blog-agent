"""Task controls authenticate independently and never wait for the turn lock."""

from fastapi import APIRouter, HTTPException, Request
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
