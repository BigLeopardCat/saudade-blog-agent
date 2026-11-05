"""Administrator scheduling API; shares application services with the Agent's tools."""

import asyncio
import logging
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from agent.api.authentication import require_console
from agent.scheduling.errors import BACKEND_ERRORS
from agent.scheduling.models import ControlArguments, CreateArguments, Identifier
from agent.scheduling.service import ScheduleConflict, ScheduleNotFound

logger = logging.getLogger(__name__)


def private_response(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/automations", tags=["automations"], dependencies=[Depends(private_response)]
)
Limit = Annotated[int, Query(ge=1, le=20)]
Offset = Annotated[int, Query(ge=0, le=1000)]


async def invoke(request, operation, *args):
    principal = require_console(request, 0)
    service = request.app.state.scheduling
    if service is None:
        raise HTTPException(503, "scheduling is disabled")
    try:
        return await asyncio.to_thread(partial(getattr(service, operation), principal, *args))
    except PermissionError as exc:
        raise HTTPException(403, "administrator identity is no longer valid") from exc
    except ScheduleNotFound as exc:
        raise HTTPException(404, "schedule not found") from exc
    except ScheduleConflict as exc:
        raise HTTPException(409, "schedule changed or ended; inspect its current state") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except BACKEND_ERRORS as exc:
        logger.error("Scheduling API failed: %s", type(exc).__name__)
        raise HTTPException(503, "scheduling service is unavailable") from exc


@router.get("")
async def list_schedules(request: Request, limit: Limit = 5, offset: Offset = 0,
                         active_only: bool = False):
    return await invoke(request, "list", limit, offset, True) if active_only else await invoke(
        request, "list", limit, offset)


@router.post("")
async def create_schedule(request: Request, body: CreateArguments):
    return await invoke(
        request, "create", body.schedule_id, body.definition.model_dump(mode="json")
    )


@router.get("/{identifier}")
async def get_schedule(request: Request, identifier: Identifier):
    return await invoke(request, "get", identifier)


@router.post("/control")
async def control_schedule(request: Request, body: ControlArguments):
    return await invoke(request, "control", body.schedule_id, body.action, body.version)


@router.get("/{identifier}/runs")
async def list_runs(request: Request, identifier: Identifier, limit: Limit = 5, offset: Offset = 0):
    return await invoke(request, "runs", identifier, limit, offset)


@router.get("/{identifier}/runs/{run_id}")
async def run_result(request: Request, identifier: Identifier, run_id: str):
    return await invoke(request, "result", identifier, run_id)
