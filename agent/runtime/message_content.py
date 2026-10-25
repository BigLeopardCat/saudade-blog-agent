"""One framework estimate for history selection and request accounting."""

from functools import partial

from langchain_core.messages.utils import count_tokens_approximately

estimate_tokens = partial(count_tokens_approximately, chars_per_token=1.5, tokens_per_image=2048)


def text_content(content) -> str:
    """Read text blocks without treating image bytes or URLs as prompt text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block if isinstance(block, str) else block.get("text", "")
                     for block in content if isinstance(block, str)
                     or isinstance(block, dict) and block.get("type") == "text")
