"""Spend the history window only after reserving current input and tool schemas."""

from langchain_core.messages import HumanMessage, SystemMessage

from agent.runtime import memory
from agent.runtime.budget import TaskBudget
from utils.trace import record


def fit_history(prompt, principal, menu, limits, output_limit, *, tool_access=None,
                budget=None, anchor=None, model_key=None):
    """Emergency model view after compression; preserve the entire active task."""
    if not prompt or not isinstance(prompt[0], SystemMessage):
        return prompt
    budget = budget or TaskBudget(limits)
    boundary = next((i for i, message in enumerate(prompt)
                     if anchor and message.id == anchor), len(prompt) - 1)
    current, body = prompt[boundary:], prompt[1:boundary]
    start = next((index for index, message in enumerate(body)
                  if not memory.is_context(message)), len(body))
    prefix = [*prompt[:1], *body[:start]]
    attachments = [message for message in body[start:] if memory.is_context(message)]
    fixed = [*prefix, *attachments, *current]
    previous = memory.history(
        [message for message in body[start:] if not memory.is_context(message)],
        principal, tool_access=tool_access)
    complete = [*prefix, *memory.with_context([*previous, *current], attachments)]
    if budget.input_tokens(complete, menu, model_key=model_key) <= min(
            limits.input_tokens, limits.context_tokens - output_limit):
        return complete
    room = budget.input_capacity(fixed, menu, output_limit=output_limit, model_key=model_key)
    visible = memory.history(previous, principal, max_tokens=room, tool_access=tool_access)
    if len(visible) < len(previous) or memory._tokens(visible) < memory._tokens(previous):
        gap = HumanMessage(content="较早的会话上下文未全部载入；缺失原文需读取会话历史核实，"
                          "不要猜测旧约定或对象。", additional_kwargs={"memory_context": True})
        room = budget.input_capacity([*fixed, gap], menu, output_limit=output_limit,
                                     model_key=model_key)
        visible = memory.history(previous, principal, max_tokens=room, tool_access=tool_access)
        prefix.append(gap)
        record("memory", "emergency_trim", before_tokens=memory._tokens(previous),
               after_tokens=memory._tokens(visible), available_tokens=max(0, room))
    return [*prefix, *memory.with_context([*visible, *current], attachments)]
