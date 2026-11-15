"""Completed delivery is retained independently of DBOS execution-history retention."""

import json
from urllib.parse import quote

from agent.scheduling.service import ScheduleNotFound
from tools import base
from tools.result import ok, unavailable


def read_result(service, actor, schedule_id, run_id, config):
    try:
        result = service.result(actor, schedule_id, run_id)
    except ScheduleNotFound:
        result = None
    if (isinstance(result, dict) and result.get("state") in {"success", "error"}
            and isinstance(result.get("result"), dict) and result["result"].get("text")):
        return ok(json.dumps(result, ensure_ascii=False), data=result)
    saved = base._admin_get(f"/api/protected/automation-results/{quote(run_id, safe='')}", config)
    if (isinstance(saved, dict) and saved.get("runId") == run_id
            and saved.get("scheduleId") == schedule_id and isinstance(saved.get("content"), str)
            and saved["content"] and saved.get("status") in {
                "completed", "incomplete", "failed", "unknown"}):
        data = {"id": run_id, "state": "result_saved", "source": "delivered_execution_result",
                "result": {"status": saved["status"], "text": saved["content"]}}
        return ok(json.dumps(data, ensure_ascii=False), data=data)
    if result is not None:
        return ok(json.dumps(result, ensure_ascii=False), data=result)
    return unavailable("The execution record is no longer available; its outcome remains unknown.")
