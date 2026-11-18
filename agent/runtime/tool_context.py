"""Allocate observations from the complete next request, before tools do model work."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from langchain_core.messages import AIMessage, ToolMessage

from agent.runtime.memory_media import hydrate
from agent.runtime.message_content import estimate_tokens
from agent.runtime.prompt_budget import fit_history
from config.settings import settings
from utils.trace import record


@dataclass(frozen=True)
class ObservationPlan:
    max_tokens: int
    name: str = "get_article_detail"
    call_id: str = "source-" + "0" * 64
    completion_tokens: int = 0
    completion_calls: int = 0

    def message(self, text):
        return ToolMessage(content=text, name=self.name, tool_call_id=self.call_id)

    def fits(self, text):
        return estimate_tokens([self.message(text)]) <= self.max_tokens


_active: ContextVar[ObservationPlan | None] = ContextVar("agent_observation_plan", default=None)


def current_observation():
    return _active.get() or ObservationPlan(settings.agent_tool_result_tokens)


def plan_observation(session, prompt, call, anchor=None):
    """Share remaining input across unanswered calls and reserve the final model reply."""
    system = [session.system_message] if session.system_message else []
    messages = fit_history([*system, *hydrate(prompt, session.transient)], session.principal,
        session.menu, session.budget.limits, session.output_limit,
        tool_access=session.history_access, budget=session.budget, anchor=anchor,
        model_key=session.model)
    last = next((i for i in range(len(messages) - 1, -1, -1)
                 if isinstance(messages[i], AIMessage) and messages[i].tool_calls), None)
    calls = messages[last].tool_calls if last is not None else [call]
    answered = {m.tool_call_id for m in messages[(last + 1) if last is not None else 0:]
                if isinstance(m, ToolMessage)}
    pending = [c for c in calls if c["id"] not in answered]
    if not any(c["id"] == call["id"] for c in pending):
        pending.append(call)
    placeholders = [ToolMessage(content="", name=c["name"], tool_call_id=c["id"])
                    for c in pending]
    fixed = [*messages, *placeholders]
    room = session.budget.input_capacity(fixed, session.menu,
        output_limit=session.output_limit, model_key=session.model)
    overhead = estimate_tokens([ToolMessage(content="", name=call["name"],
                                           tool_call_id=call["id"])])
    # Small rounding differences between per-message and complete-request estimates stay reserved.
    share = max(0, room // len(pending) - 16)
    limit = min(settings.agent_tool_result_tokens, overhead + share)
    completion = (session.budget.input_tokens(fixed, session.menu, model_key=session.model)
                  + max(0, limit - overhead) * len(pending) + session.output_limit)
    plan = ObservationPlan(limit, call["name"], call["id"], completion, 1)
    record("budget", "observation_planned", call_id=call["id"],
           observation_tokens=limit, pending_results=len(pending),
           completion_tokens=completion, completion_calls=1)
    return plan


@contextmanager
def observation_scope(plan):
    token = _active.set(plan)
    try:
        yield plan
    finally:
        _active.reset(token)
