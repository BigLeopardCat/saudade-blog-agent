"""Adapt standard summarization to request budgets and the conversation commit."""

import uuid

from langchain.agents.middleware import AgentMiddleware, SummarizationMiddleware
from langchain_core.messages import HumanMessage, trim_messages
from openai import OpenAIError

from agent.principal import SOURCE_ASSERTION
from agent.runtime import memory
from agent.runtime.message_content import estimate_tokens
from agent.runtime.summarization import summarize
from agent.runtime.turns import check_cancelled
from config.settings import settings
from utils.trace import record


class ConversationCompaction(AgentMiddleware):
    """Compact completed turns once, before this task starts invoking tools."""

    def __init__(self, session):
        self.session = session
        self.checked = False

    def before_model(self, state, runtime):
        session = self.session
        if self.checked or not session.fresh_task or state.get("resume"):
            return None
        self.checked = True
        check_cancelled(session.config)
        messages = list(state["messages"])
        current = max((i for i, message in enumerate(messages)
                       if isinstance(message, HumanMessage) and not memory.is_context(message)),
                      default=0)
        completed = [message for message in messages[:current]
                     if not memory.is_context(message)]
        if not completed:
            return None
        fixed = [*([session.system_message] if session.system_message else []),
                 *(message for message in messages[:current] if memory.is_context(message)),
                 *messages[current:]]
        room = session.budget.output_capacity(fixed, session.menu) - session.output_limit
        trigger = max(1, min(memory.COMPACTION_TOKENS, room))
        if estimate_tokens(completed) < trigger:
            return None
        recent = trim_messages(completed, max_tokens=max(1, trigger // 2),
            token_counter=estimate_tokens, strategy="last", start_on="human", allow_partial=False)
        latest = memory.dialogue_turns(completed)[-1]
        keep = max(len(recent), len(latest))
        if keep >= len(completed):
            return None
        try:
            from models import get_llm

            model = get_llm(streaming=False, max_tokens=settings.agent_summary_output_tokens,
                            enable_thinking=False, max_retries=0)
            middleware = SummarizationMiddleware(
                model=model, trigger=("tokens", trigger), keep=("messages", keep),
                token_counter=estimate_tokens, trim_tokens_to_summarize=None,
                summarizer=lambda source: summarize(source, budget=session.budget,
                                                    config=session.config, model=model))
            update = middleware.before_model({"messages": completed}, runtime)
        except (OpenAIError, TimeoutError, ValueError) as error:
            record("memory", "compaction_skipped", reason=type(error).__name__)
            return None
        if update is None:
            return None
        replacement = update["messages"][1:]
        summary = replacement[0]
        principal = session.principal
        actor = ({"uid": principal.uid, "role": principal.known_role}
                 if getattr(principal, "source", None) == SOURCE_ASSERTION else None)
        summary = summary.model_copy(update={"id": uuid.uuid4().hex, "additional_kwargs": {
            **summary.additional_kwargs, **({"memory_actor": actor} if actor else {})}})
        preserved = replacement[1:]
        ids = {message.id for message in preserved}
        covered = [message.id for message in completed if message.id not in ids]
        session.context_update = {"summary": summary, "covered_ids": covered}
        dialogue = [summary, *preserved, *messages[current:]]
        attachments = [message for message in messages[:current] if memory.is_context(message)]
        # Account context stays ahead of history. Transient data belongs to the
        # current request, preserving older messages and short-answer referents.
        leading = 0
        while leading < len(messages) and memory.is_context(messages[leading]):
            leading += 1
        prefix = messages[:leading]
        attachments = [message for message in attachments if message not in prefix]
        record("memory", "compacted", source_messages=len(covered),
               before_tokens=estimate_tokens(completed),
               after_tokens=estimate_tokens([summary, *preserved]))
        return {"messages": [update["messages"][0], *prefix,
                *memory.with_context(dialogue, attachments)],
                "context_update": session.context_update}
