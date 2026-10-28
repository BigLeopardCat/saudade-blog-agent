"""Spend the history window only after reserving current input and tool schemas."""

from langchain_core.messages import SystemMessage

from agent.runtime import memory
from agent.runtime.budget import RequestBudget


def fit_history(prompt, principal, menu, limits, output_limit, *, tool_access=None, budget=None):
    # This runs before the first model call; never trim an in-flight tool exchange.
    if not prompt or not isinstance(prompt[0], SystemMessage):
        return prompt
    current = prompt[-1:]
    body = prompt[1:-1]
    start = next((index for index, message in enumerate(body)
                  if not memory.is_context(message)), len(body))
    prefix = [*prompt[:1], *body[:start]]
    attachments = [message for message in body[start:] if memory.is_context(message)]
    fixed = [*prefix, *attachments, *current]
    room = (budget or RequestBudget(limits)).output_capacity(fixed, menu) - output_limit
    previous = [message for message in body[start:] if not memory.is_context(message)]
    visible = memory.history(previous, principal, max_tokens=room, tool_access=tool_access)
    return [*prefix, *memory.with_context([*visible, *current], attachments)]
