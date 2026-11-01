"""Prune terminal execution trees through public DBOS APIs; live plans remain intact."""

import argparse
import json
from datetime import timedelta

from agent.scheduling.backend import APP, ONCE, PERIODIC, Backend
from agent.scheduling.factory import configured_dsn
from agent.scheduling.models import utc_now
from config.settings import settings

KEEP_DAYS = 30


def collect(backend, *, apply=False, now=utc_now):
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
        client.delete_workflows(candidates, delete_children=True)
    return {"candidates": candidates, "deleted": candidates if apply else []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not settings.scheduling_enabled:
        print(json.dumps({"skipped": "scheduling_disabled"}))
        return
    backend = Backend(configured_dsn())
    try:
        print(json.dumps(collect(backend, apply=args.apply)))
    finally:
        backend.close()


if __name__ == "__main__":
    main()
