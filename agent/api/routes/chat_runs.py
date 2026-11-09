"""Authenticated submission, observation and explicit cancellation of background chat."""

import asyncio
from contextlib import aclosing

from dbos import error
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from agent.api.authentication import resolve_principal
from agent.api.events import KEEPALIVE_FRAME, frame, response
from agent.background.service import ACTIVE, STREAM, ChatRuns, RunConflict
from agent.scheduling.errors import BACKEND_ERRORS


def private_response(result: Response):
    result.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/chat/runs", dependencies=[Depends(private_response)])


class Submission(BaseModel):
    run_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    conversation_id: int = Field(gt=0)
    body: str = Field(min_length=1, max_length=8 * 1024 * 1024)
    ticket: str = Field(min_length=1, max_length=4096)


def service(request):
    principal = resolve_principal(request, 0)
    scheduling = request.app.state.scheduling
    if scheduling is None:
        raise HTTPException(503, "background chat is unavailable")
    return ChatRuns(scheduling.backend), principal


async def invoke(function, *args):
    try:
        return await asyncio.to_thread(function, *args)
    except PermissionError as exc:
        raise HTTPException(403, "verified identity required") from exc
    except LookupError as exc:
        raise HTTPException(404, "chat run not found") from exc
    except RunConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except BACKEND_ERRORS as exc:
        raise HTTPException(503, "chat run storage unavailable") from exc


@router.post("", status_code=202)
async def create(request: Request, body: Submission):
    runs, principal = service(request)
    return await invoke(runs.create, principal, body)


@router.get("")
async def list_runs(request: Request, conversation_id: int | None = Query(None, gt=0)):
    runs, principal = service(request)
    return await invoke(runs.list, principal, conversation_id)


@router.post("/{run_id}/cancel")
async def cancel(request: Request, run_id: str):
    runs, principal = service(request)
    return await invoke(runs.cancel, principal, run_id)


@router.get("/{run_id}/events")
async def events(request: Request, run_id: str, offset: int = Query(0, ge=0)):
    runs, principal = service(request)
    row = await invoke(runs.owned, principal, run_id)

    async def subscribe():
        cursor = offset
        terminal = False
        while True:
            try:
                async with aclosing(runs.client.read_stream_async(row.workflow_id, STREAM,
                        offset=cursor, timeout_seconds=10)) as stream:
                    async for item in stream:
                        yield f"id: {cursor}\n" + frame(item["event"], item["data"])
                        cursor += 1
                        terminal = item["event"] in {"done", "error"}
                break
            except error.DBOSStreamTimeoutError:
                yield KEEPALIVE_FRAME
        if not terminal:
            current = await invoke(runs.owned, principal, run_id)
            if current.status not in ACTIVE:
                yield frame("error", "后台任务已停止或中断，请核对已有结果。")

    # Closing this response closes only the DBOS reader, never the workflow.
    return response(subscribe())
