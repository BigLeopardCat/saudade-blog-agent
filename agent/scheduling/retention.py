"""Prune terminal execution trees through public DBOS APIs; live plans remain intact."""

import argparse
import json
from contextlib import ExitStack
from datetime import timedelta

from agent.background.service import WORKFLOW as CHAT_WORKFLOW
from agent.scheduling.backend import APP, ONCE, PERIODIC, Backend
from agent.scheduling.checkpoints import Cleanup
from agent.scheduling.factory import configured_dsn
from agent.scheduling.models import utc_now
from config.settings import settings

KEEP_DAYS = 30


def collect(backend, *, apply=False, now=utc_now, cleanup=None):
    client = backend.client
    rows = client.list_workflows(
        application_name=APP,
        name=[ONCE, PERIODIC],
        has_parent=False,
        status=["SUCCESS", "ERROR", "CANCELLED"],
        limit=100,
        completed_before=(now() - timedelta(days=KEEP_DAYS)).isoformat(),
        load_input=False,
        load_output=False,
    )
    candidates = []
    for row in rows:
        if (row.attributes or {}).get("paused"):
            continue
        if client.list_workflows(
            parent_workflow_id=row.workflow_id,
            status=["PENDING", "ENQUEUED", "DELAYED"],
            limit=1,
            application_name=APP,
            load_input=False,
            load_output=False,
        ):
            continue
        candidates.append(row.workflow_id)
    if apply and candidates:
        if cleanup is None:
            raise ValueError("checkpoint cleanup is required before pruning execution evidence")
        for identifier in candidates:
            cleanup(identifier)
        client.delete_workflows(candidates, delete_children=True)
    return {"candidates": candidates, "deleted": candidates if apply else []}


def collect_chat_runs(backend, *, apply=False, now=utc_now):
    """Expire terminal transport streams without deleting conversation checkpoints."""
    rows = backend.client.list_workflows(application_name=APP, name=CHAT_WORKFLOW,
        status=["SUCCESS", "ERROR", "CANCELLED"], limit=100,
        completed_before=(now() - timedelta(days=KEEP_DAYS)).isoformat(),
        load_input=False, load_output=False)
    candidates = [row.workflow_id for row in rows]
    if apply and candidates:
        backend.client.delete_workflows(candidates)
    return {"candidates": candidates, "deleted": candidates if apply else []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not settings.scheduling_enabled:
        print(json.dumps({"skipped": "scheduling_disabled"}))
        return
    with ExitStack() as resources:
        backend = Backend(configured_dsn())
        resources.callback(backend.close)
        result = collect(backend, apply=args.apply, cleanup=Cleanup(backend.client, resources))
        result["chat_runs"] = collect_chat_runs(backend, apply=args.apply)
        print(json.dumps(result))


if __name__ == "__main__":
    main()
