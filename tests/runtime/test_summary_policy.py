"""Only framework compaction owns scheduling; source scopes still protect imports."""

from agent.api.schemas import ChatRequest, ChatResponse
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.memory_scope import safe_request

SCOPE = {"user_id": 7, "role": "user", "conversation_id": 42}
ACTOR = Principal(7, "user", SOURCE_ASSERTION)


def test_public_schema_has_no_independent_summary_scheduler_or_writeback():
    assert "summary_batch" not in ChatRequest.model_fields
    assert not hasattr(ChatRequest, "needs_summary")
    assert not {"new_summary", "summary_coverage"}.intersection(ChatResponse.model_fields)


def test_bootstrap_source_is_not_relabelled_after_role_or_user_change():
    request = ChatRequest(message="continue", conversation_id=42, summary="PRIVATE_CANARY",
                          summary_scope=SCOPE, history_scope=SCOPE,
                          history=[{"role": "user", "content": "PRIVATE_CANARY"}])
    assert safe_request(request, ACTOR).summary == "PRIVATE_CANARY"
    for actor in [Principal(8, "user", SOURCE_ASSERTION),
                  Principal(7, "admin", SOURCE_ASSERTION)]:
        cleaned = safe_request(request, actor)
        assert not cleaned.summary and not cleaned.history
