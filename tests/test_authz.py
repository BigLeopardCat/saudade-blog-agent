"""Declarative permissions stay closed independently of model intent or wording."""

import pytest

from agent import authz
from agent.domains.registry import operation_names
from agent.principal import (
    KNOWN_ROLES,
    ROLE_ADMIN,
    ROLE_SUPERADMIN,
    ROLE_USER,
    ROLE_ZAKO,
    Principal,
)
from tools.base import get_all_tools


def principal(role):
    return Principal(uid=7, role=role)


def test_manifest_is_complete_closed_and_uses_declared_scopes():
    names = [tool.name for tool in get_all_tools()] + sorted(operation_names())
    assert not authz.manifest_gaps(names)
    assert not authz.manifest_stale(names)
    assert set(authz.TOOL_SCOPE.values()) <= authz.ALL_SCOPES
    assert all(isinstance(scope, str) for scope in authz.TOOL_SCOPE.values())
    assert authz.WRITE_SCOPES <= authz.ALL_SCOPES
    assert authz.manifest_gaps(["unknown_tool"]) == ["unknown_tool"]
    assert authz.manifest_stale(["list_notes"]) == sorted(set(names) - {"list_notes"})


def test_role_grants_preserve_the_product_permissions():
    assert authz.scopes_for(ROLE_USER) == frozenset(
        {
            authz.SCOPE_READ_PUBLIC,
            authz.SCOPE_READ_OWN,
            authz.SCOPE_WRITE_PAGE,
            authz.SCOPE_WRITE_DEVICE,
            authz.SCOPE_WRITE_OWN,
        }
    )
    assert authz.scopes_for(ROLE_ADMIN) == authz.ALL_SCOPES
    assert authz.scopes_for(ROLE_SUPERADMIN) == authz.ALL_SCOPES
    assert authz.scopes_for(ROLE_ZAKO) == frozenset()


@pytest.mark.parametrize("role", [None, "", "root", "visitor"])
def test_unknown_roles_have_no_grants_and_never_default_to_an_existing_role(role):
    assert not authz.scopes_for(role)
    for name in authz.TOOL_SCOPE:
        result = authz.check(principal(role), name)
        assert not result.allowed
        assert result.reason == authz.REASON_UNKNOWN_ROLE
        assert not authz.holds(principal(role), authz.required_scope(name))


@pytest.mark.parametrize("role", sorted(KNOWN_ROLES))
def test_every_registered_tool_obeys_exactly_its_scope_and_role_grant(role):
    actor = principal(role)
    for name, scope in authz.TOOL_SCOPE.items():
        expected = scope in authz.scopes_for(role)
        result = authz.check(actor, name)
        assert result.allowed is expected
        assert result.scope == scope and result.tool == name
        assert result.reason == (authz.REASON_OK if expected else authz.REASON_DENIED)
        assert authz.holds(actor, scope) is expected
        assert authz.is_write(name) is (scope in authz.WRITE_SCOPES)


def test_missing_identity_and_unknown_tool_fail_closed():
    for name in authz.TOOL_SCOPE:
        assert not authz.check(None, name).allowed
    for actor in [None, *(principal(role) for role in KNOWN_ROLES)]:
        result = authz.check(actor, "unregistered_tool")
        assert not result.allowed and result.reason == authz.REASON_NO_MANIFEST
        assert result.scope is None
        assert not authz.holds(actor, "unregistered_scope")
    assert authz.required_scope("unregistered_tool") is None
    assert not authz.is_write("unregistered_tool")
