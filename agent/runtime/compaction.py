"""Capacity-driven standard summarization at durable model boundaries."""

import uuid

from langchain.agents.middleware import AgentMiddleware, SummarizationMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from openai import OpenAIError

from agent.principal import SOURCE_ASSERTION
from agent.runtime import memory
from agent.runtime.execution_state import restore
from agent.runtime.memory_media import hydrate
from agent.runtime.message_content import estimate_tokens
from agent.runtime.summarization import SummaryUnavailable, summarize
from agent.runtime.turns import check_cancelled
from config.settings import settings
from utils.trace import record


def _protected(messages, anchor):
    """Keep the task's original request and steering verbatim, outside its summary."""
    start = next((i for i, message in enumerate(messages) if anchor and message.id == anchor), None)
    if start is None:
        start = max((i for i, message in enumerate(messages)
                 if isinstance(message, HumanMessage) and not memory.is_context(message)
                 and message.additional_kwargs.get("lc_source") != "summarization"
                 and not message.additional_kwargs.get("task_input")), default=len(messages))
    return (messages[start].id if start < len(messages) else None,
            {message.id for message in messages[start:] if isinstance(message, HumanMessage)})


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


def _summarizable(source_tokens, keep, budget):
    """A legacy checkpoint may exceed the per-call budget; skip its summary safely."""
    if source_tokens <= keep:
        return False
    limit = budget.limits.input_tokens - settings.agent_summary_output_tokens - 256
    if source_tokens - keep <= limit:
        return True
    record("memory", "compaction_skipped", reason="summary_input_too_large",
           source_tokens=source_tokens, keep_tokens=keep, summary_limit=limit)
    return False


def _capacity_shortage(error):
    """Capacity pressure is retryable; provider and validation failures are not."""
    return (isinstance(error, SummaryUnavailable)
            and error.reason in ("context_tokens", "input_tokens", "request_tokens"))


class ConversationCompaction(AgentMiddleware):
    """Reserve complete input/output space and compact only under capacity pressure."""

    def __init__(self, session):
        self.session = session
        self.failed = False

    def before_model(self, state, runtime):
        session = self.session
        # Approval processing needs its original pending exchange. The next
        # execution boundary can compact after the receipt is incorporated.
        if self.failed or state.get("resume") or state.get("proposal") or state.get("terminal"):
            return None
        check_cancelled(session.config)
        restore(session, state)
        messages = list(state["messages"])
        anchor, protected = _protected(messages, state.get("context_anchor"))
        anchored = {"context_anchor": anchor} if anchor != state.get("context_anchor") else None
        system = [session.system_message] if session.system_message else []
        budget, menu = session.budget, session.menu
        prompt = [*system, *hydrate(messages, session.transient)]
        before = budget.input_tokens(prompt, menu, model_key=session.model) + session.output_limit
        trigger = int(budget.limits.input_tokens * settings.agent_compaction_trigger)
        if before < trigger:
            return anchored
        source = [m for m in messages if not memory.is_context(m)]
        source_tokens = estimate_tokens(source)
        fixed = [*system, *(m for m in messages if memory.is_context(m) or m.id in protected)]
        target = int(budget.limits.input_tokens * settings.agent_compaction_target)
        keep = max(1, target - budget.input_tokens(fixed, menu)
                   - session.output_limit - settings.agent_summary_output_tokens)
        if len(source) < 2 or not _summarizable(source_tokens, keep, budget):
            return anchored
        try:
            from models import get_llm

            model = get_llm(streaming=False, max_tokens=settings.agent_summary_output_tokens,
                            enable_thinking=False, max_retries=0)
            middleware = SummarizationMiddleware(
                model=model, trigger=("tokens", 1), keep=("tokens", keep),
                token_counter=estimate_tokens, trim_tokens_to_summarize=None,
                summarizer=lambda old: summarize(old, budget=budget,
                                                config=session.config, model=model))
            update = middleware.before_model({"messages": source}, runtime)
        except (OpenAIError, TimeoutError, ValueError) as error:
            # Capacity pressure is expected on the first request after a legacy
            # oversized checkpoint; the history selector bounds that request, so
            # do not poison later rounds. Provider failures stay terminal.
            self.failed = self.failed or not _capacity_shortage(error)
            record("memory", "compaction_skipped", reason=(
                error.reason if isinstance(error, SummaryUnavailable) else type(error).__name__),
                   request_tokens=before, trigger_tokens=trigger)
            return anchored
        if update is None:
            return anchored
        summary, *preserved = update["messages"][1:]
        principal = session.principal
        actor = ({"uid": principal.uid, "role": principal.known_role}
                 if getattr(principal, "source", None) == SOURCE_ASSERTION else None)
        summary = summary.model_copy(update={"id": uuid.uuid4().hex, "additional_kwargs": {
            **summary.additional_kwargs, **({"memory_actor": actor} if actor else {})}})
        kept = protected | {message.id for message in preserved}
        covered = [message for message in source if message.id not in kept]
        if not covered:
            return anchored
        covered_ids = {message.id for message in covered}
        replacement = _replacement(messages, summary, covered_ids)
        after = budget.input_tokens([*system, *hydrate(replacement, session.transient)], menu)
        if after + session.output_limit >= before:
            self.failed = True
            record("memory", "compaction_skipped", reason="no_reduction")
            return anchored
        record("memory", "compacted", source_messages=len(covered),
               before_tokens=estimate_tokens(source),
               after_tokens=estimate_tokens([summary, *preserved]),
               request_before_tokens=before, request_after_tokens=after + session.output_limit,
               trigger_tokens=trigger, target_tokens=target,
               protected_messages=len(protected), phase="task" if any(
                   isinstance(m, AIMessage) and m.tool_calls for m in covered) else "history")
        return {"messages": [update["messages"][0], *replacement], "context_anchor": anchor,
                "context_update": _coverage(state.get("context_update"), covered, summary)}
