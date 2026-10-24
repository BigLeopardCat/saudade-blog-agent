"""Bounded host facts for this turn; none are execution or authorization inputs."""

import json
import re
from datetime import datetime

from agent.api.schemas import ChatRequest
from config.settings import settings
from utils.clock import format_local_now

_TIMESTAMP = re.compile(r"(?<![\d-])(\d{2})-(\d{2}) (\d{2}):(\d{2})(?![\d:])")
_OPEN_STATES = frozenset({"submitted", "running", "input_required"})


def _age_minutes(match, now):
    try:
        timestamp = datetime(now.year, *(int(part) for part in match.groups()))
        if (timestamp - now).total_seconds() > 6 * 3600:
            timestamp = timestamp.replace(year=now.year - 1)
    except ValueError:
        return None
    return int((now - timestamp).total_seconds() // 60)


def execution_summary(text: str) -> str:
    """Annotate host timestamps so old observations are distinguishable from current reads."""
    now = datetime.now()

    def annotate(match):
        minutes = _age_minutes(match, now)
        if minutes is None:
            return match.group(0)
        return f"{match.group(0)}（距本轮 {max(0, minutes)} 分钟）"

    return _TIMESTAMP.sub(annotate, text.strip()[:1500])


def _open_tasks(raw: str) -> list[dict]:
    try:
        rows = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    limit = max(1, min(settings.agent_task_inject_max, 3))
    tasks = []
    for row in rows:
        if not (isinstance(row, dict) and row.get("task_id")
                and isinstance(row.get("state"), str) and row["state"] in _OPEN_STATES):
            continue
        tasks.append({"task_id": str(row["task_id"])[:80], "state": row["state"],
                      "goal": str(row.get("goal") or "")[:200],
                      "pending_question": str(row.get("pending_question") or "")[:200]})
        cursor = row.get("cursor")
        cursor = cursor if type(cursor) is int and cursor >= 0 else 0
        steps = row.get("steps")
        total = row.get("total_steps", row.get("total"))
        if type(total) is int and total >= 0:
            tasks[-1]["total_steps"] = total
        if isinstance(steps, list):
            tasks[-1]["total_steps"] = max(tasks[-1].get("total_steps", 0), len(steps))
            tasks[-1]["remaining_steps"] = max(0, len(steps) - cursor)
            if cursor < len(steps) and isinstance(steps[cursor], dict):
                step = steps[cursor]
                tasks[-1]["next_step"] = str(step.get("label") or step.get("tool") or "")[:120]
        tasks[-1]["cursor"] = cursor
        if len(tasks) == limit:
            break
    return tasks


def request_facts(req: ChatRequest) -> dict:
    """Project host display/history fields, excluding credentials and raw execution payloads."""
    facts: dict = {"current_time": format_local_now()}
    if req.chat_quota is not None:
        facts["chat_quota"] = req.chat_quota.model_dump()
    if req.executions.strip():
        facts["recent_execution_summary"] = execution_summary(req.executions)
    if req.pending_action.strip():
        facts["reported_pending_action"] = req.pending_action.strip()[:800]
    tasks = _open_tasks(req.agent_tasks)
    if tasks:
        facts["reported_open_tasks"] = tasks
    return facts
