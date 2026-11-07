"""Bounded host facts for this turn; none are execution or authorization inputs."""

from agent.api.schemas import ChatRequest
from utils.clock import format_local_now


def request_facts(req: ChatRequest) -> dict:
    """Project host display/history fields, excluding credentials and raw execution payloads."""
    facts: dict = {"current_time": format_local_now(), "current_weather": {"status": "unknown"}}
    if req.current_weather is not None:
        facts["current_weather"] = {"status": "observed", **req.current_weather.facts()}
    if req.chat_quota is not None:
        facts["chat_quota"] = req.chat_quota.model_dump()
    return facts
