"""Lifecycle facts obey the same identity and domain permissions as preparation."""

import pytest
from langchain_core.messages import HumanMessage

from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from agent.products.blog.history import tool_access
from agent.runtime import memory
from tools.result import ok


@pytest.mark.parametrize("role,source,name,allowed", [
    ("superadmin", SOURCE_ASSERTION, "prepare_tag_create", True),
    ("user", SOURCE_ASSERTION, "prepare_tag_create", False),
    ("user", SOURCE_ASSERTION, "prepare_favorite_change", True),
    ("user", SOURCE_BODY, "prepare_favorite_change", False),
    ("user", SOURCE_ASSERTION, "cancel_pending_operation", True),
    ("zako", SOURCE_ASSERTION, "cancel_pending_operation", False),
    ("superadmin", SOURCE_ASSERTION, "prepare_unknown_operation", False),
])
def test_lifecycle_history_access_comes_from_registered_domains(role, source, name, allowed):
    assert tool_access(Principal(7, role, source), name) is allowed


def test_preparation_and_cancellation_history_cannot_cross_actor_or_role_boundaries():
    owner = Principal(7, "superadmin", SOURCE_ASSERTION)
    for name in ("prepare_tag_create", "cancel_pending_operation"):
        saved = memory.turn_messages([HumanMessage(content="PRIVATE_CANARY")],
            memory.observation({"name": name, "args": {}, "id": "fact"}, ok("PRIVATE_CANARY")),
            "PRIVATE_CANARY", owner)
        assert memory.history(saved, owner, tool_access=tool_access)
        for other in (Principal(8, "superadmin", SOURCE_ASSERTION),
                      Principal(7, "user", SOURCE_ASSERTION),
                      Principal(7, "superadmin", SOURCE_BODY)):
            assert memory.history(saved, other, tool_access=tool_access) == []
