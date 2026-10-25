"""Spend the history window only after reserving current input and tool schemas."""

from langchain_core.messages import SystemMessage
from langchain_core.messages.utils import count_tokens_approximately

from agent.runtime import memory
from agent.runtime.message_content import estimate_tokens


def fit_history(prompt, principal, menu, limits, output_limit):
    # This runs before the first model call; never trim an in-flight tool exchange.
    if not prompt or not isinstance(prompt[0], SystemMessage):
        return prompt
    current = prompt[-1:]
    attachments = [message for message in prompt[1:-1] if memory.is_context(message)]
    fixed = [*prompt[:1], *attachments, *current]
    schema = count_tokens_approximately([], tools=list(menu))
    room = limits.context_tokens - estimate_tokens(fixed) - schema - output_limit
    previous = [message for message in prompt[1:-1] if not memory.is_context(message)]
    return [*prompt[:1], *memory.history(previous, principal, max_tokens=room),
            *attachments, *current]
