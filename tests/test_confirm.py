"""Signed approvals require canonical tokens and current actor/conversation/executor bindings."""

import hashlib
import hmac
import json
import re
import secrets
import time

import pytest

from agent import confirm

SPECS = [{"tool": "create_tag", "args": {"title": "测试标签", "parent_id": None,
                                         "color": "#eb2f96"}}]


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))


def sign(**changes):
    values = {"uid": 7, "conv_id": 42, "workflow": "tag_create", "specs": SPECS,
              "executor": "tags", **changes}
    return confirm.sign(**values)


def signed_payload(payload):
    """Create negative controls with authentic signatures, independently of the issuing gate."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    signature = hmac.new(confirm.settings.confirmation_secret.encode(),
                         confirm._DOMAIN + body, hashlib.sha256).digest()
    return confirm._b64e(body) + "." + confirm._b64e(signature)


def test_signatures_bind_actor_conversation_executor_and_frozen_arguments():
    token = sign()
    payload = confirm.verify(token, 7, 42)
    assert payload is not None
    assert {key: payload[key] for key in ("uid", "conv", "workflow", "specs", "executor")} == {
        "uid": 7, "conv": 42, "workflow": "tag_create", "specs": SPECS, "executor": "tags"}
    assert token.count(".") == 1 and len(token) < 2000
    assert 300 <= confirm.TTL_SECONDS <= 900
    assert confirm.token_expiry(token) == payload["exp"]
    assert confirm.token_jti(token) == payload["jti"]
    assert re.fullmatch(r"[0-9a-f]{32}", payload["jti"])
    assert confirm.token_jti(sign()) != payload["jti"]
    assert confirm.verify(token, 8, 42) is None
    assert confirm.verify(token, 7, 43) is None
    changed_body = confirm._b64e(json.dumps({**payload, "specs": []}).encode())
    assert confirm.verify(changed_body + "." + token.split(".")[1], 7, 42) is None
    assert confirm.verify(token[:-2] + ("aa" if not token.endswith("aa") else "bb"), 7, 42) is None


@pytest.mark.parametrize("coordinate", [None, True, False, 0, -1, "7", 7.0, 2_147_483_648])
@pytest.mark.parametrize("field", ["uid", "conv_id"])
def test_invalid_request_coordinates_cannot_sign_or_verify(field, coordinate):
    assert sign(**{field: coordinate}) == ""
    values = {"uid": 7, "conv_id": 42, field: coordinate}
    assert confirm.verify(sign(), **values) is None


@pytest.mark.parametrize("field,value", [
    ("uid", True), ("uid", "7"), ("uid", 7.0), ("uid", None), ("uid", 0),
    ("conv", True), ("conv", "42"), ("conv", 42.0), ("conv", None), ("conv", 0),
    ("executor", None), ("executor", True), ("executor", "unknown"),
    ("v", 1), ("v", 2.0), ("v", "2"), ("exp", True), ("exp", int(time.time()) - 1),
    ("workflow", ""), ("workflow", True), ("specs", None), ("specs", []),
    ("jti", None), ("jti", True), ("jti", ""), ("jti", "a" * 31), ("jti", "A" * 32),
    ("specs", [{"tool": "create_tag", "args": {"title": "$list_tags[0].name"}}]),
])
def test_even_authentic_signatures_do_not_authorize_invalid_payloads(field, value):
    payload = confirm.verify(sign(), 7, 42)
    payload[field] = value
    assert confirm.verify(signed_payload(payload), 7, 42) is None


def test_missing_executor_is_rejected_even_when_signed():
    payload = confirm.verify(sign(), 7, 42)
    payload.pop("executor")
    assert confirm.verify(signed_payload(payload), 7, 42) is None
    with pytest.raises(TypeError, match="executor"):
        confirm.sign(7, 42, "tag_create", SPECS)


@pytest.mark.parametrize("changes", [
    {"executor": None}, {"executor": "unknown"}, {"workflow": ""}, {"specs": []},
    {"specs": [{"tool": "set_article_tags", "args": {"add": ["$list_tags[0].name"]}}]},
    {"specs": [{"tool": "set_article_tags", "args": {"nested": [["$x[0].y"]]}}]},
    {"specs": [{"tool": "create_tag", "args": {"title": object()}}]},
])
def test_invalid_or_unresolved_arguments_never_produce_a_token(changes):
    assert sign(**changes) == ""


@pytest.mark.parametrize("token", [None, "", "nodot", "x.y.z", "!!!.???"])
def test_malformed_tokens_are_refused_without_exception(token):
    assert confirm.verify(token, 7, 42) is None
    assert confirm.token_expiry(token) == 0
    assert confirm.token_jti(token) == ""


def test_absent_key_disables_both_issuing_and_verification(monkeypatch):
    token = sign()
    monkeypatch.setattr(confirm.settings, "confirmation_secret", "")
    assert sign() == "" and confirm.verify(token, 7, 42) is None
