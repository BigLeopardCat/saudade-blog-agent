"""One framework estimate for history selection and request accounting."""

from functools import partial

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately

_estimate = partial(count_tokens_approximately, chars_per_token=1.5, tokens_per_image=2048)


def estimate_tokens(messages) -> int:
    """Include provider reasoning replay in the same conservative framework estimate."""
    messages = list(messages)
    replay = [SystemMessage(content=message.additional_kwargs["reasoning_content"])
              for message in messages if isinstance(message, AIMessage)
              and isinstance(message.additional_kwargs.get("reasoning_content"), str)
              and message.additional_kwargs["reasoning_content"]]
    return _estimate([*messages, *replay])


def text_content(content) -> str:
    """Read text blocks without treating image bytes or URLs as prompt text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block if isinstance(block, str) else block.get("text", "")
                     for block in content if isinstance(block, str)
                     or isinstance(block, dict) and block.get("type") == "text")
