"""Reuse provider input usage only for an unchanged model, tools and message prefix."""

import hashlib
import json
from dataclasses import dataclass

from agent.runtime.message_content import estimate_tokens


def _fingerprint(value):
    payload = json.dumps(value, ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class InputShape:
    model_key: object
    tools: str
    messages: tuple[str, ...]


def shape(messages, tools, model_key):
    if model_key is None:
        return None
    try:
        return InputShape(model_key, _fingerprint(tools), tuple(
            _fingerprint(message.model_dump()) for message in messages))
    except (TypeError, ValueError):
        return None


def estimate(anchor, current, messages):
    if anchor is None or current is None:
        return None
    previous, tokens = anchor
    count = len(previous.messages)
    if (current.model_key is not previous.model_key or current.tools != previous.tools
            or len(current.messages) < count or current.messages[:count] != previous.messages):
        return None
    # No tokenizer is recreated: only the appended messages use the framework estimate.
    return tokens + estimate_tokens(messages[count:])
