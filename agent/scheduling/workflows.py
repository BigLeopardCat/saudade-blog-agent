"""DBOS owns timer and execution recovery; these workflows only compose product actions."""

import asyncio
import logging
from datetime import datetime, timezone

from dbos import DBOS, WorkflowSerializationFormat, error

from agent.scheduling.backend import (
    EXECUTE,
    EXECUTION_QUEUE,
    ONCE,
    PERIODIC,
    REMINDER_QUEUE,
    storage_name,
)
from agent.scheduling.models import Definition

_actions = None
logger = logging.getLogger(__name__)


def configure(actions):
    global _actions
    _actions = actions


def actions():
    if _actions is None:
        raise RuntimeError("schedule worker actions have not been configured")
    return _actions


@DBOS.step()
def clock():
    return datetime.now(timezone.utc).isoformat()


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1)
async def condition(owner, identifier):
    try:
        return {
            "satisfied": await asyncio.to_thread(actions().host.article_public, owner, identifier)
        }
    except PermissionError:
        return {"blocked": True}


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1)
async def state(payload):
    return await asyncio.to_thread(actions().state, payload)


async def ready(payload):
    while True:
        current = await state(payload)
        if current != "paused":
            return current == "ready"
        await DBOS.recv_async(topic="schedule_control", timeout_seconds=60)


@DBOS.step()
async def perform(payload, run_id):
    # Fresh permissions are checked inside the effect step, including recovery.
    return await asyncio.to_thread(actions().perform, payload, run_id)


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1)
async def notify(payload, run_id, result):
    if not await asyncio.to_thread(actions().deliverable, payload):
        return {"delivered": False, "reason": "schedule_disabled"}
    return await asyncio.to_thread(
        actions().host.notify,
        payload["owner_id"],
        run_id,
        payload["definition"]["title"],
        result["text"],
    )


@DBOS.workflow(name=EXECUTE, serialization_type=WorkflowSerializationFormat.PORTABLE)
async def execute(payload: dict, run_id: str):
    if not await ready(payload):
        return {"status": "blocked", "reason": "schedule_disabled"}
    result = await perform(payload, run_id)
    if result["status"] in {"completed", "incomplete"}:
        try:
            notification = await notify(payload, run_id, result)
        except error.DBOSMaxStepRetriesExceeded:
            logger.warning("Scheduled result notification failed", extra={"run_id": run_id})
            notification = {"delivered": False, "reason": "delivery_failed"}
        result = {**result, "notification": notification}
    return result


async def dispatch(payload, suffix, occurrence_at):
    name = storage_name(payload["owner_id"], payload["schedule_id"])
    run_id = f"{name}_{suffix}"
    handle = await DBOS.enqueue_workflow_with_options_async(
        {
            "workflow_name": EXECUTE,
            "queue_name": (
                REMINDER_QUEUE if payload["definition"]["action"] == "reminder" else EXECUTION_QUEUE
            ),
            "workflow_id": run_id,
            "authenticated_user": str(payload["owner_id"]),
            "deduplication_id": name,
            "duplication_policy": "return-existing",
            "serialization_type": WorkflowSerializationFormat.PORTABLE,
        },
        {**payload, "occurrence_at": occurrence_at},
        run_id,
    )
    if handle.workflow_id != run_id:
        return {"status": "skipped", "reason": "previous_run_active"}
    return await handle.get_result()


@DBOS.workflow(name=ONCE, serialization_type=WorkflowSerializationFormat.PORTABLE)
async def once(payload: dict):
    definition = Definition.model_validate(payload["definition"])
    start = datetime.fromisoformat(payload["start_at"])
    now = datetime.fromisoformat(clock())
    if now < start:
        await DBOS.sleep_async((start - now).total_seconds())
        now = datetime.fromisoformat(clock())
    if (now - start).total_seconds() > definition.grace_seconds:
        return {"status": "expired", "reason": "missed_execution_window"}
    while definition.condition and definition.expires_at is not None:
        if now >= definition.expires_at:
            return {"status": "expired", "reason": "condition_deadline"}
        value = await condition(payload["owner_id"], definition.condition.article_id)
        if value.get("blocked"):
            return {"status": "blocked", "reason": "owner_not_authorized"}
        if value["satisfied"]:
            break
        await DBOS.sleep_async(
            min(
                definition.condition.check_interval_seconds,
                (definition.expires_at - now).total_seconds(),
            )
        )
        now = datetime.fromisoformat(clock())
    if not await ready(payload):
        return {"status": "cancelled"}
    return await dispatch(payload, "once", start.isoformat())


@DBOS.workflow(name=PERIODIC, serialization_type=WorkflowSerializationFormat.PORTABLE)
async def periodic(scheduled_time: datetime, payload: dict):
    definition = Definition.model_validate(payload["definition"])
    now = datetime.fromisoformat(clock())
    if (now - scheduled_time).total_seconds() > definition.grace_seconds:
        return {"status": "skipped", "reason": "missed_execution_window"}
    if not await ready(payload):
        return {"status": "skipped", "reason": "schedule_disabled"}
    return await dispatch(payload, str(int(scheduled_time.timestamp())), scheduled_time.isoformat())
