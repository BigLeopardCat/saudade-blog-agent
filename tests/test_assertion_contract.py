"""Host assertions require valid signatures, audience, expiry and positive subjects."""

import secrets
import time
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException

from agent.api import authentication
from config.settings import Settings, settings


@pytest.fixture
def key(monkeypatch):
    value = secrets.token_urlsafe(32)
    monkeypatch.setattr(settings, "agent_assertion_secret", value)
    return value


def sign(key, **changes):
    claims = {"sub": 7, "aud": "agent", "exp": int(time.time()) + 60, "role": "user"}
    claims.update(changes)
    return jwt.encode(claims, key, algorithm="HS256")


@pytest.mark.parametrize("subject", [7, "7"])
def test_numeric_rust_and_string_subjects_are_verified(key, subject):
    claims = authentication.verify_assertion_claims(sign(key, sub=subject))
    assert claims["uid"] == 7 and claims["role"] == "user"


@pytest.mark.parametrize("subject", [True, False, 7.5, 0, -1, "", "-1", " 7", "７", None])
def test_invalid_subject_never_becomes_a_user_id(key, subject):
    assert authentication.verify_assertion_claims(sign(key, sub=subject)) is None


@pytest.mark.parametrize("field,value", [("aud", "blog-session"), ("aud", None),
                                          ("exp", 1), ("exp", None), ("sub", None)])
def test_invalid_claims_are_rejected(key, field, value):
    assert authentication.verify_assertion_claims(sign(key, **{field: value})) is None


@pytest.mark.parametrize("field", ["sub", "aud", "exp"])
def test_required_claims_cannot_be_omitted(key, field):
    claims = {"sub": 7, "aud": "agent", "exp": int(time.time()) + 60}
    claims.pop(field)
    token = jwt.encode(claims, key, algorithm="HS256")
    assert authentication.verify_assertion_claims(token) is None


def test_wrong_signature_empty_key_and_malformed_tokens_are_rejected(key, monkeypatch):
    valid = sign(key)
    assert authentication.verify_assertion_claims(sign(secrets.token_urlsafe(32))) is None
    assert authentication.verify_assertion_claims("malformed") is None
    assert authentication.verify_assertion_claims(valid + "x") is None
    monkeypatch.setattr(settings, "agent_assertion_secret", "")
    assert authentication.verify_assertion_claims(valid) is None


def test_unsigned_algorithm_is_not_accepted(key):
    token = jwt.encode({"sub": 7, "aud": "agent", "exp": int(time.time()) + 60},
                       key="", algorithm="none")
    assert authentication.verify_assertion_claims(token) is None


def test_verified_identity_overrides_body_and_uses_only_verified_delegations(key, monkeypatch):
    seen = []
    monkeypatch.setattr(authentication.delegation, "accept", lambda *args: seen.append(args))
    request = SimpleNamespace(headers={"X-Agent-Assertion": sign(
        key, delegation="verified-capability", background_delegation="verified-background")})
    principal = authentication.resolve_principal(request, 999)
    assert principal.uid == 7 and principal.source == "assertion"
    assert seen == [(7, "verified-capability", "verified-background")]
    assert "verified-capability" not in repr(principal)
    with pytest.raises(HTTPException) as rejected:
        authentication.resolve_principal(SimpleNamespace(headers={}), 7)
    assert rejected.value.status_code == 401 and len(seen) == 1


@pytest.mark.parametrize("source", ["constructor", "environment", "dotenv"])
def test_retired_auth_opt_out_is_rejected(source, monkeypatch, tmp_path):
    values = {"_env_file": None}
    if source == "constructor":
        values["agent_require_assertion"] = False
    elif source == "environment":
        monkeypatch.setenv("AGENT_REQUIRE_ASSERTION", "0")
    else:
        path = tmp_path / "local.env"
        path.write_text("AGENT_REQUIRE_ASSERTION=0\n")
        values["_env_file"] = path
    with pytest.raises(ValueError, match="retired"):
        Settings(**values)
