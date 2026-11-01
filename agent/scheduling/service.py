"""Administrator-owned schedule management, using DBOS as the state authority."""

from datetime import datetime

from dbos import WorkflowSerializationFormat

from agent import authz
from agent.principal import is_verified_identity
from agent.scheduling.backend import APP, CONTROL_QUEUE, ONCE, PERIODIC, storage_name
from agent.scheduling.models import Definition, fingerprint, utc_now


class ScheduleConflict(ValueError):
    pass


class ScheduleNotFound(ValueError):
    pass


def require_admin(principal):
    if not (is_verified_identity(principal) and authz.holds(principal, authz.SCOPE_ADMIN_CONSOLE)):
        raise PermissionError("administrator identity required")
    return principal.uid


def once_view(row):
    attributes = row.attributes or {}
    definition = attributes["definition"]
    status = (
        "cancelled"
        if attributes.get("cancelled")
        else "paused"
        if attributes.get("paused") and row.status != "SUCCESS"
        else {"SUCCESS": "finished", "ERROR": "failed", "CANCELLED": "cancelled"}.get(
            row.status, "scheduled"
        )
    )
    view = {
        "id": attributes["schedule_id"],
        "status": status,
        "definition": definition,
        "run_at": attributes["start_at"],
        "kind": "once",
    }
    return {**view, "version": fingerprint(view)}


def recurring_view(row):
    context = row["context"]
    view = {
        "id": context["schedule_id"],
        "status": row["status"].lower(),
        "definition": context["definition"],
        "kind": "recurring",
    }
    return {**view, "version": fingerprint(view)}


def run_view(row, *, full=False):
    output = row.output
    if not full and isinstance(output, dict):
        output = {key: output[key] for key in ("status", "reason", "notification") if key in output}
        if isinstance(row.output.get("text"), str):
            output["preview"] = row.output["text"][:240]
    return {
        "id": row.workflow_id,
        "state": row.status.lower(),
        "created_at_ms": row.created_at,
        "updated_at_ms": row.updated_at,
        "result": output,
        "error": "execution_failed" if row.error else None,
    }


class ScheduleService:
    def __init__(self, backend, check_actor, *, now=utc_now):
        self.backend, self.check_actor, self.now = backend, check_actor, now

    def authorize(self, principal):
        owner = require_admin(principal)
        actor = self.check_actor(owner)
        if require_admin(actor) != owner:
            raise PermissionError("verified account does not match schedule owner")
        return owner

    def _get(self, owner, identifier):
        recurring = self.backend.recurring(owner, identifier)
        if recurring:
            return recurring_view(recurring)
        once = self.backend.once(owner, identifier)
        if once:
            return once_view(once)
        raise ScheduleNotFound("schedule not found")

    def get(self, principal, identifier):
        return self._get(self.authorize(principal), identifier)

    def create(self, principal, identifier, definition):
        owner = self.authorize(principal)
        definition = Definition.model_validate(definition)
        value = definition.model_dump(mode="json")
        with self.backend.mutation(owner):
            try:
                previous = self._get(owner, identifier)
            except ScheduleNotFound:
                previous = None
            if previous:
                if previous["definition"] != value:
                    raise ScheduleConflict("schedule ID already contains another definition")
                return {"changed": False, "schedule": previous}
            # The owner lock serializes schedule admission across HTTP processes.
            prefix = storage_name(owner, "")
            recurring = self.backend.client.list_schedules(
                schedule_name_prefix=prefix, application_name=APP, workflow_name=PERIODIC
            )
            pending = self.backend.client.list_workflows(
                workflow_id_prefix=prefix,
                application_name=APP,
                name=ONCE,
                status=["ENQUEUED", "DELAYED", "PENDING", "CANCELLED"],
                attributes={"cancelled": False},
                load_input=False,
                load_output=False,
                limit=21,
            )
            if len(recurring) + len(pending) >= 20:
                raise ScheduleConflict("at most twenty pending schedules per administrator")
            now = self.now()
            definition.validate_creation(now)
            payload = {
                "owner_id": owner,
                "schedule_id": identifier,
                "definition": value,
                "start_at": (definition.resolved_time(now) or now).isoformat(),
            }
            if definition.cron:
                self.backend.client.create_schedule(
                    schedule_name=storage_name(owner, identifier),
                    workflow_name=PERIODIC,
                    schedule=definition.cron,
                    context=payload,
                    cron_timezone=definition.timezone,
                    queue_name=CONTROL_QUEUE,
                    automatic_backfill=False,
                    application_name=APP,
                )
            else:
                self.backend.enqueue_once(owner, identifier, payload)
            return {"changed": True, "schedule": self._get(owner, identifier)}

    def control(self, principal, identifier, action, version):
        owner = self.authorize(principal)
        with self.backend.mutation(owner):
            prior = self._get(owner, identifier)
            if prior["version"] != version:
                raise ScheduleConflict("schedule changed; inspect it before confirming again")
            if prior["status"] in {"finished", "failed", "cancelled"}:
                raise ScheduleConflict("schedule has already ended")
            desired = (
                {"pause": "paused", "resume": "active"}[action] if action != "cancel" else None
            )
            if prior["status"] == desired:
                return {"changed": False, "schedule": prior}
            if action == "resume":
                definition = Definition.model_validate(prior["definition"])
                if prior["kind"] == "once":
                    definition = definition.model_copy(
                        update={
                            "delay_seconds": None,
                            "run_at": datetime.fromisoformat(prior["run_at"]),
                        }
                    )
                definition.validate_creation(self.now())
            name = storage_name(owner, identifier)
            client = self.backend.client
            if prior["kind"] == "recurring":
                method = {
                    "pause": client.pause_schedule,
                    "resume": client.resume_schedule,
                    "cancel": client.delete_schedule,
                }[action]
                method(name)
            elif action == "resume":
                if prior["status"] != "paused":
                    return {"changed": False, "schedule": prior}
                client.update_workflow_attributes(name, {"paused": False}, merge=True)
                client.send(
                    name,
                    "resume",
                    topic="schedule_control",
                    serialization_type=WorkflowSerializationFormat.PORTABLE,
                )
                child = name + "_once"
                if client.list_workflows(workflow_ids=[child], application_name=APP):
                    client.send(
                        child,
                        "resume",
                        topic="schedule_control",
                        serialization_type=WorkflowSerializationFormat.PORTABLE,
                    )
            else:
                client.update_workflow_attributes(
                    name, {"paused": action == "pause", "cancelled": action == "cancel"}, merge=True
                )
                if action == "cancel":
                    client.cancel_workflow(name)
            view = (
                {**prior, "status": "cancelled"}
                if action == "cancel"
                else self._get(owner, identifier)
            )
            return {"changed": True, "schedule": view}

    def list(self, principal, limit=10, offset=0):
        owner = self.authorize(principal)
        prefix = storage_name(owner, "")
        recurring = [
            recurring_view(row)
            for row in self.backend.client.list_schedules(
                schedule_name_prefix=prefix, application_name=APP, workflow_name=PERIODIC
            )
        ]
        once = [
            once_view(row)
            for row in self.backend.client.list_workflows(
                workflow_id_prefix=prefix,
                application_name=APP,
                name=ONCE,
                limit=limit + offset + 1,
                sort_desc=True,
                load_input=False,
                load_output=False,
            )
        ]
        items = sorted(recurring, key=lambda item: item["id"]) + once
        summaries = [
            {
                "id": row["id"],
                "title": row["definition"]["title"],
                "action": row["definition"]["action"],
                "status": row["status"],
                "run_at": row.get("run_at"),
                "cron": row["definition"].get("cron"),
                "timezone": row["definition"]["timezone"],
            }
            for row in items[offset : offset + limit]
        ]
        return {
            "items": summaries,
            "has_more": len(items) > offset + limit,
            "next_offset": offset + limit if len(items) > offset + limit else None,
        }

    def runs(self, principal, identifier, limit=10, offset=0):
        owner = self.authorize(principal)
        client = self.backend.client
        name = storage_name(owner, identifier)
        rows = (
            client.list_workflows(workflow_ids=[name], application_name=APP)
            if self.backend.once(owner, identifier)
            else client.list_workflows(
                schedule_name=name,
                application_name=APP,
                limit=limit + 1,
                offset=offset,
                sort_desc=True,
                load_input=False,
            )
        )
        if self.backend.once(owner, identifier):
            rows = rows[offset:]
        return {
            "items": [run_view(row) for row in rows[:limit]],
            "has_more": len(rows) > limit,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def result(self, principal, identifier, run_id):
        owner = self.authorize(principal)
        name = storage_name(owner, identifier)
        if run_id != name and not run_id.startswith(name + "_"):
            raise ScheduleNotFound("execution not found")
        rows = self.backend.client.list_workflows(
            workflow_ids=[run_id], application_name=APP, load_input=False
        )
        if not rows:
            raise ScheduleNotFound("execution not found")
        return run_view(rows[0], full=True)
