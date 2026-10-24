"""Native authorization contract without planner or graph imports."""

from agent import authz
from agent.principal import (
    ADMIN_ROLES,
    KNOWN_ROLES,
    ROLE_ADMIN,
    ROLE_SUPERADMIN,
    ROLE_USER,
    ROLE_ZAKO,
    SOURCE_ASSERTION,
    UNKNOWN,
    Principal,
)
from tools.base import _TOOL_REGISTRY


def principal(role, uid=7):
    return Principal(uid=uid, role=role, source=SOURCE_ASSERTION)


def test_native_manifest_and_role_scopes_are_closed():
    names = [tool.name for tool in _TOOL_REGISTRY]
    assert not authz.manifest_gaps(names)
    assert not authz.manifest_stale(names)
    assert set(authz.TOOL_SCOPE.values()) <= authz.ALL_SCOPES
    assert all(scopes <= authz.ALL_SCOPES
               for scopes in (authz.scopes_for(role) for role in KNOWN_ROLES))
    assert ADMIN_ROLES == {ROLE_ADMIN, ROLE_SUPERADMIN}
    assert authz.scopes_for(ROLE_ZAKO) < authz.scopes_for(ROLE_USER)
    assert authz.scopes_for(ROLE_USER) < authz.scopes_for(ROLE_ADMIN)
    assert authz.scopes_for(ROLE_ADMIN) <= authz.scopes_for(ROLE_SUPERADMIN)


def test_native_authorization_fails_closed_and_checks_scope():
    assert not authz.check(UNKNOWN, "list_notes").allowed
    assert authz.check(UNKNOWN, "list_notes").reason == authz.REASON_UNKNOWN_ROLE
    assert not authz.check(None, "list_notes").allowed
    assert not authz.check(principal("invented"), "list_notes").allowed
    assert not authz.check(principal(ROLE_ADMIN), "unknown_tool").allowed
    assert authz.check(principal(ROLE_USER), "list_notes").allowed
    assert not authz.check(principal(ROLE_USER), "list_accounts").allowed
    assert authz.check(principal(ROLE_ADMIN), "list_accounts").allowed
    assert authz.check(principal(ROLE_ADMIN), "freeze_account").allowed
    assert not authz.check(principal(ROLE_USER), "freeze_account").allowed


def test_native_write_classification_is_explicit():
    assert not authz.is_write("get_chat_history")
    assert authz.is_write("navigate_to")
    assert authz.is_write("freeze_account")
    assert authz.required_scope("account_mute") == authz.SCOPE_WRITE_CONSOLE
    assert authz.required_scope("account_unmute") == authz.SCOPE_WRITE_CONSOLE
    assert authz.required_scope("set_account_role") == authz.SCOPE_WRITE_CONSOLE
