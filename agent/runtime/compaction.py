"""Capacity-driven standard summarization at durable model boundaries."""

import uuid

from langchain.agents.middleware import AgentMiddleware, SummarizationMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from openai import OpenAIError

from agent.principal import SOURCE_ASSERTION
from agent.runtime import memory
from agent.runtime.budget import BudgetExceeded
from agent.runtime.context_selection import summary_cutoff
from agent.runtime.execution_state import capture, restore
from agent.runtime.memory_media import hydrate
from agent.runtime.message_content import estimate_tokens
from agent.runtime.summarization import SummaryUnavailable, summarize
from agent.runtime.turns import check_cancelled
from config.settings import settings
from utils.trace import record


def _protected(messages, anchor, pending=False):
    """Keep the task's original request and steering verbatim, outside its summary."""
    start = next((i for i, message in enumerate(messages) if anchor and message.id == anchor), None)
    if start is None:
        start = max((i for i, message in enumerate(messages)
                 if isinstance(message, HumanMessage) and not memory.is_context(message)
                 and message.additional_kwargs.get("lc_source") != "summarization"
                 and not message.additional_kwargs.get("task_input")), default=len(messages))
    protected = {message.id for message in messages[start:] if isinstance(message, HumanMessage)}
    if pending or (messages and isinstance(messages[-1], ToolMessage)):
        exchange = max(
            (
                i
                for i, message in enumerate(messages)
                if isinstance(message, AIMessage) and message.tool_calls
            ),
            default=len(messages),
        )
        protected.update(message.id for message in messages[exchange:])
    return messages[start].id if start < len(messages) else None, protected


def _complete_compaction(anchored, state):
    update = state.get("context_update") or {}
    if update.get("compaction_pending"):
        return {**(anchored or {}), "context_update": {**update, "compaction_pending": False}}
    return anchored


def _coverage(previous, covered, summary):
    """Compose replacements across several compactions of the same task."""
    return {"summary": summary,
            "covered_ids": list(dict.fromkeys([
                *(previous or {}).get("covered_ids", []), *(m.id for m in covered)])),
            "covered_tool_calls": list(dict.fromkeys([
                *(previous or {}).get("covered_tool_calls", []),
                *(m.tool_call_id for m in covered if isinstance(m, ToolMessage))]))}


def _replacement(messages, summary, covered_ids):
    """Keep trusted context first and preserve the relative order of retained facts."""
    leading = 0
    while leading < len(messages) and memory.is_context(messages[leading]):
        leading += 1
    return [*messages[:leading], summary,
            *(m for m in messages[leading:] if m.id not in covered_ids)]


def _apply_summary(messages, source, update, guard, protected, principal):
    """Stamp summary provenance and replace only the source messages it covered."""
    summary, *preserved = update["messages"][1:]
    preserved = [m for m in preserved if m.id != guard.id]
    actor = ({"uid": principal.uid, "role": principal.known_role}
             if getattr(principal, "source", None) == SOURCE_ASSERTION else None)
    summary = summary.model_copy(update={"id": uuid.uuid4().hex, "additional_kwargs": {
        **summary.additional_kwargs, **({"memory_actor": actor} if actor else {})}})
    kept = protected | {message.id for message in preserved}
    covered = [message for message in source if message.id not in kept]
    replacement = _replacement(messages, summary, {message.id for message in covered})
    counts = {"source_messages": len(covered), "before_tokens": estimate_tokens(source),
              "after_tokens": estimate_tokens([summary, *preserved]),
              "protected_messages": len(protected), "phase": "task" if any(
                  isinstance(m, AIMessage) and m.tool_calls for m in covered) else "history"}
    return summary, covered, replacement, counts


class ConversationCompaction(AgentMiddleware):
    """Reserve complete input/output space and compact only under capacity pressure."""

    def __init__(self, session):
        self.session = session
        self.failed = False

    @hook_config(can_jump_to=["model"])
    def before_model(self, state, runtime):
        session = self.session
        if self.failed or state.get("resume") or state.get("terminal"):
            return None
        check_cancelled(session.config)
        restore(session, state)
        messages = list(state["messages"])
        anchor, protected = _protected(messages, state.get("context_anchor"), state.get("proposal"))
        anchored = {"context_anchor": anchor} if anchor != state.get("context_anchor") else None
        system = [session.system_message] if session.system_message else []
        budget, menu = session.budget, session.menu
        prompt = [*system, *hydrate(messages, session.transient)]
        before = budget.input_tokens(prompt, menu, model_key=session.model)
        capacity = min(budget.limits.input_tokens,
                       budget.limits.context_tokens - session.output_limit)
        trigger = int(capacity * settings.agent_compaction_trigger)
        continuing = bool((state.get("context_update") or {}).get("compaction_pending"))
        if before < trigger and not continuing:
            return anchored
        source = [m for m in messages if not memory.is_context(m) and m.id not in protected]
        fixed = [*system, *(m for m in messages if memory.is_context(m) or m.id in protected)]
        target = int(capacity * settings.agent_compaction_target)
        keep = max(1, target - budget.input_tokens(fixed, menu)
                   - settings.agent_summary_output_tokens)
        if len(source) < 2 or estimate_tokens(source) <= keep:
            return _complete_compaction(anchored, state)
        cutoff = summary_cutoff(source, keep, budget)
        if not cutoff:
            self.failed = True
            record("memory", "compaction_skipped", reason="oversized_exchange")
            return anchored
        guard = next((m for m in reversed(messages) if m.id in protected
                      and isinstance(m, HumanMessage) and not memory.is_context(m)), None)
        if guard is None:
            return anchored
        update = self._summarize(source, cutoff, guard, runtime, before, trigger)
        if update is None:
            return anchored
        summary, covered, replacement, counts = _apply_summary(
            messages, source, update, guard, protected, session.principal)
        if not covered:
            return anchored
        after = budget.input_tokens([*system, *hydrate(replacement, session.transient)], menu)
        if after >= before:
            self.failed = True
            record("memory", "compaction_skipped", reason="no_reduction")
            return anchored
        record("memory", "compacted", **counts,
               request_before_tokens=before, request_after_tokens=after,
               trigger_tokens=trigger, target_tokens=target)
        saved = {"messages": [update["messages"][0], *replacement], "context_anchor": anchor,
                 "progress_keys": capture(session)["progress_keys"],
                 "context_update": {**_coverage(state.get("context_update"), covered, summary),
                                    "compaction_pending": after > target}}
        if after > target:
            saved["jump_to"] = "model"
        return saved

    def _summarize(self, source, cutoff, guard, runtime, before, trigger):
        from models import get_llm

        try:
            model = get_llm(streaming=False, max_tokens=settings.agent_summary_output_tokens,
                            enable_thinking=False, max_retries=0)
            middleware = SummarizationMiddleware(
                model=model, trigger=("tokens", 1), keep=("messages", len(source) + 1 - cutoff),
                token_counter=estimate_tokens, trim_tokens_to_summarize=None,
                summarizer=lambda old: summarize(old, budget=self.session.budget,
                                                config=self.session.config, model=model))
            return middleware.before_model({"messages": [*source, guard]}, runtime)
        except (OpenAIError, TimeoutError, ValueError) as error:
            if isinstance(error, SummaryUnavailable) and error.reason in (
                    "task_tokens", "model_calls", "task_seconds", "task_time_unknown"):
                raise BudgetExceeded(error.reason) from error
            self.failed = True
            record("memory", "compaction_skipped", reason=(
                error.reason if isinstance(error, SummaryUnavailable) else type(error).__name__),
                   input_tokens=before, trigger_tokens=trigger)
            return None
