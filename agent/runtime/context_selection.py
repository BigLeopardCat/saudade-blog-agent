"""Choose bounded summary prefixes without splitting native tool exchanges."""

from langchain_core.messages import AIMessage, ToolMessage, trim_messages

from agent.runtime.message_content import estimate_tokens
from agent.runtime.summarization import input_prompt
from config.settings import settings


def _cut_points(messages, maximum):
    pending, points = set(), []
    for index, message in enumerate(messages[:maximum]):
        if isinstance(message, AIMessage):
            pending.update(call["id"] for call in message.tool_calls)
        elif isinstance(message, ToolMessage):
            pending.discard(message.tool_call_id)
        if not pending:
            points.append(index + 1)
    return points


def summary_cutoff(messages, keep, budget):
    """Bound the rendered summary request, including instructions and XML framing."""
    tail = trim_messages(messages, max_tokens=keep, token_counter=estimate_tokens,
                         strategy="last", start_on=("human", "ai"), allow_partial=False)
    # The current task is protected separately; all old source messages may be covered.
    points = _cut_points(messages, len(messages) - len(tail))
    ceiling = min(budget.limits.input_tokens,
                  budget.limits.context_tokens - settings.agent_summary_output_tokens)
    low, high, selected = 0, len(points), 0
    while low < high:
        middle = (low + high) // 2
        if budget.input_tokens(input_prompt(messages[:points[middle]])) <= ceiling:
            selected = points[middle]
            low = middle + 1
        else:
            high = middle
    return selected
