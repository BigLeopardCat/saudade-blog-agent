"""Capabilities must come from the host and stay bound to one verified actor."""
import asyncio
import json
import time
from pathlib import Path

import jwt
import pytest
from starlette.requests import Request

from agent import delegation
from agent.api.authentication import resolve_principal, verify_assertion_claims
from config.settings import Settings, settings
from tools import base, device

KEY = 'host-only-delegation-key-at-least-32-bytes'
ASSERTION = 'independent-assertion-key-at-least-32-bytes'


def capability(uid=7, scope='tools', **overrides):
    return jwt.encode(dict(sub=uid, exp=int(time.time()) + 300, ver=4, role='user',
                           aud='blog-tools', scope=scope, **overrides), KEY, algorithm='HS256')


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    context = delegation.clear()
    monkeypatch.setattr(settings, 'delegation_dir', str(tmp_path / 'delegations'))
    monkeypatch.setattr(settings, 'scheduling_enabled', True)
    monkeypatch.setattr(settings, 'agent_assertion_secret', ASSERTION)
    yield
    delegation.reset(context)


def test_no_login_key_or_self_signing_fallback(monkeypatch):
    monkeypatch.setenv('JWT_SECRET', KEY)
    configured = Settings(_env_file=None)
    assert 'jwt_secret' not in Settings.model_fields
    assert configured.confirmation_secret != KEY
    with pytest.raises(PermissionError):
        base._sign_local_jwt(7, 'admin')
    with pytest.raises(PermissionError):
        device._sign_user_jwt(7)


def test_forwarding_preserves_host_signature_and_actor_across_threads():
    token = capability()
    delegation.accept(7, token)
    assert base._sign_local_jwt(7, 'admin') == token
    assert device._sign_user_jwt(7) == token
    assert asyncio.run(asyncio.to_thread(delegation.credential, 7)) == token
    with pytest.raises(ValueError):
        delegation.credential(8)
    context = delegation.clear()
    with pytest.raises(PermissionError):
        delegation.credential(7)
    delegation.reset(context)


@pytest.mark.parametrize('changes', [{'sub': 8}, {'aud': 'blog-session'}, {'scope': 'background'},
                                    {'exp': 1}, {'ver': None}])
def test_invalid_nested_credentials_rejected(changes):
    claims = jwt.decode(capability(), options={'verify_signature': False})
    claims.update(changes)
    invalid = jwt.encode(claims, KEY, algorithm='HS256')
    with pytest.raises(ValueError):
        delegation.accept(7, invalid)


def test_background_is_private_owner_bound_and_expires(monkeypatch):
    token = capability(scope='background')
    delegation.accept(7, '', token)
    path = Path(settings.delegation_dir) / '7.json'
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert delegation.credential(7) == token
    with pytest.raises(PermissionError):
        delegation.credential(8)
    claims = jwt.decode(token, options={'verify_signature': False})
    claims['exp'] = 1
    path.write_text(json.dumps({'token': jwt.encode(claims, KEY, algorithm='HS256')}))
    with pytest.raises(PermissionError):
        delegation.credential(7)


def test_assertion_key_does_not_accept_confirmation_or_login_key(monkeypatch):
    monkeypatch.setattr(settings, 'confirmation_secret', KEY)
    claims = {'sub': 7, 'aud': 'agent', 'exp': int(time.time()) + 60, 'role': 'user',
              'delegation': capability()}
    wrong = jwt.encode(claims, KEY, algorithm='HS256')
    assert verify_assertion_claims(wrong) is None
    valid = jwt.encode(claims, ASSERTION, algorithm='HS256')
    request = Request({'type': 'http', 'headers': [(b'x-agent-assertion', valid.encode())]})
    principal = resolve_principal(request, 8)
    assert principal.uid == 7
    assert delegation.credential(7) == claims['delegation']
    assert 'delegation' not in repr(principal)


def test_operator_file_requires_private_permissions_and_matching_host_capability(monkeypatch, tmp_path):
    source = tmp_path / 'operator.json'
    token = capability()
    source.write_text(json.dumps({'7': token}))
    source.chmod(0o600)
    monkeypatch.setenv('SAUDADE_OPERATOR_TOOLS_FILE', str(source))
    assert delegation.credential(7) == token
    source.chmod(0o644)
    with pytest.raises(PermissionError):
        delegation.credential(7)
    source.chmod(0o600)
    source.write_text(json.dumps({'7': capability(uid=8)}))
    with pytest.raises(ValueError):
        delegation.credential(7)
