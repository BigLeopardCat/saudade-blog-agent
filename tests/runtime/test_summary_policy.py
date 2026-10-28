"""Summary frequency belongs to the Agent, independent of host scheduling hints."""

import secrets

from agent.api.schemas import ChatRequest
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.memory_scope import safe_request

SCOPE = {"user_id": 7, "role": "user", "conversation_id": 42}
ACTOR = Principal(7, "user", SOURCE_ASSERTION)


def request(count=10, content="brief", hint=True):
    return ChatRequest(
        message="current",
        conversation_id=42,
        history_scope=SCOPE,
        needs_summary=hint,
        summary_batch={
            "base_id": 0,
            "messages": [{"id": i + 1, "role": "user", "content": content} for i in range(count)],
        },
    )


def test_host_hint_cannot_force_small_batches_or_suppress_a_full_window():
    assert not safe_request(request(19), ACTOR).needs_summary
    assert safe_request(request(20, hint=False), ACTOR).needs_summary
    assert safe_request(request(4, "文" * 1500, hint=False), ACTOR).needs_summary


def test_policy_never_bypasses_source_or_confirmation_boundaries():
    value = request(20)
    assert not safe_request(value.model_copy(update={"history_scope": None}), ACTOR).needs_summary
    assert not safe_request(
        value.model_copy(update={"confirm_token": secrets.token_urlsafe(24)}), ACTOR
    ).needs_summary
