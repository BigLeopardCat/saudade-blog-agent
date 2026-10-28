"""Bounded host facts for this turn; none are execution or authorization inputs."""

import re
from datetime import datetime

from agent.api.schemas import ChatRequest
from utils.clock import format_local_now

_TIMESTAMP = re.compile(r"(?<![\d-])(\d{2})-(\d{2}) (\d{2}):(\d{2})(?![\d:])")


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


def request_facts(req: ChatRequest) -> dict:
    """Project host display/history fields, excluding credentials and raw execution payloads."""
    facts: dict = {"current_time": format_local_now()}
    if req.chat_quota is not None:
        facts["chat_quota"] = req.chat_quota.model_dump()
    if req.executions.strip():
        facts["recent_execution_summary"] = execution_summary(req.executions)
    if req.pending_action.strip():
        facts["reported_pending_action"] = req.pending_action.strip()[:800]
    return facts
