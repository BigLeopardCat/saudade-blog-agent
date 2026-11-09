"""Owner-bound run admission and views over public DBOS APIs."""

import hashlib
import json

from dbos import WorkflowSerializationFormat

from agent.principal import is_verified_identity
from agent.scheduling.backend import APP

WORKFLOW = "saudade.chat.execute.v1"
QUEUE = "chat-executions"
STREAM = "events"
ACTIVE = ["PENDING", "ENQUEUED", "DELAYED"]


def identifier(owner, run_id):
    return f"chat_{owner}_{run_id}"


class RunConflict(ValueError):
    pass


class ChatRuns:
    def __init__(self, backend):
        self.backend, self.client = backend, backend.client

    @staticmethod
    def owner(principal):
        if not is_verified_identity(principal) or not principal.known_role:
            raise PermissionError("verified chat identity required")
        return principal.uid

    def rows(self, owner, **filters):
        return self.client.list_workflows(application_name=APP, name=WORKFLOW,
            user=str(owner), load_input=False, load_output=True, **filters)

    def owned(self, principal, run_id):
        owner = self.owner(principal)
        rows = self.rows(owner, workflow_ids=[identifier(owner, run_id)], limit=1)
        row = rows[0] if rows else None
        if (row is None or row.authenticated_user != str(owner)
                or (row.attributes or {}).get("role") != principal.known_role):
            raise LookupError("chat run not found")
        return row

    @staticmethod
    def view(row):
        attrs = row.attributes or {}
        return {"run_id": attrs["run_id"], "conversation_id": attrs["conversation_id"],
                "title": attrs["title"], "status": row.status,
                "task_id": attrs.get("task_id"), "turn_id": attrs.get("turn_id"),
                "outcome": (row.output or {}).get("status")
                if isinstance(row.output, dict) else None}

    def create(self, principal, submission):
        owner = self.owner(principal)
        body = json.loads(submission.body)
        if (not isinstance(body, dict)
                or body.get("conversation_id") != submission.conversation_id):
            raise ValueError("conversation mismatch")
        digest = hashlib.sha256(submission.body.encode()).hexdigest()
        wid = identifier(owner, submission.run_id)
        with self.backend.mutation(owner):
            existing = self.rows(owner, workflow_ids=[wid], limit=1)
            if existing:
                row = self.owned(principal, submission.run_id)
                if (row.attributes or {}).get("digest") != digest:
                    raise RunConflict("run identifier already has another request")
                return self.view(row)
            active = self.rows(owner, status=ACTIVE, limit=5)
            if len(active) >= 4:
                raise RunConflict("too many active chat runs")
            if any((row.attributes or {}).get("conversation_id") == submission.conversation_id
                   for row in active):
                raise RunConflict("conversation already has an active run")
            attrs = {"run_id": submission.run_id, "conversation_id": submission.conversation_id,
                     "role": principal.known_role, "digest": digest,
                     "title": str(body.get("message") or "继续任务")[:160],
                     "task_id": body.get("resume_task") or body.get("task_id"),
                     "turn_id": body.get("turn_id")}
            self.client.enqueue({"workflow_name": WORKFLOW, "queue_name": QUEUE,
                "workflow_id": wid, "authenticated_user": str(owner), "attributes": attrs,
                "deduplication_id": f"chat_{owner}_{submission.conversation_id}",
                "serialization_type": WorkflowSerializationFormat.PORTABLE},
                {"body": submission.body, "ticket": submission.ticket})
            return self.view(self.owned(principal, submission.run_id))

    def list(self, principal, conversation_id=None):
        rows = self.rows(self.owner(principal), limit=100, sort_desc=True,
            attributes={"conversation_id": conversation_id} if conversation_id else None)
        return {"enabled": True, "items": [self.view(row) for row in rows
                          if (row.attributes or {}).get("role") == principal.known_role]}

    def cancel(self, principal, run_id):
        row = self.owned(principal, run_id)
        if row.status in ACTIVE:
            self.client.cancel_workflow(row.workflow_id)
        return self.view(self.owned(principal, run_id))
