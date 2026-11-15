"""Diagnostic copies remove recognizable credentials while live task data stays intact."""

import copy
import json
import secrets
from pathlib import Path

import pytest

from utils import trace
from utils.secret_redaction import REDACTED, redact_credentials


@pytest.mark.parametrize("value,secret", [
    ('api_key="synthetic-provider-key"', "synthetic-provider-key"),
    ('{"api-key":"synthetic-provider-key"}', "synthetic-provider-key"),
    ("Authorization: Bearer synthetic-session-token", "synthetic-session-token"),
    ("api_key=sk-synthetic0123456789abcdef", "sk-synthetic0123456789abcdef"),
    ("登录态 eyJhbGciOiJIUzI1NiJ9.eyJ1aWQiOjd9.synthetic_signature", "eyJhbGciOiJIUzI1NiJ9"),
    ("-----BEGIN PRIVATE KEY-----\nsynthetic-private-key\n-----END PRIVATE KEY-----",
     "synthetic-private-key"),
])
def test_credential_forms_are_filtered_without_changing_the_caller_value(value, secret):
    filtered = redact_credentials(value)
    assert secret in value and secret not in filtered
    assert REDACTED in filtered and redact_credentials(filtered) == filtered


def test_filter_keeps_usage_ids_source_coordinates_and_nonsecret_text():
    token_count = 4
    original = {"input_tokens": 321, "output_tokens": 20, "cache_read": 256,
                "key": secrets.token_hex(32), "user_id": 7, "token_count": token_count,
                "source": {"revision": "r1", "offset": 100, "end": 121},
                "items": [{"id": 45, "content": "密码字段是必填，当前尚未执行。"}],
                "text": '只输出 JSON: {"count":2}', "query": "token budget",
                "link": "https://blog.test/article/7?token=public-reference"}
    assert redact_credentials(original) == original
    assert redact_credentials(original) is not original


def test_nested_structured_credentials_are_removed_from_diagnostic_copies():
    values = [secrets.token_urlsafe(24) for _ in range(5)]
    original = {"headers": {"Authorization": "Bearer " + values[0]},
                "details": [{"api_key": values[1], "Password": values[2]}],
                "confirmation": {"confirm_token": values[3]},
                "code": json.dumps({"refresh_token": values[4]})}
    before = copy.deepcopy(original)
    safe = redact_credentials(original)
    assert original == before
    serialized = json.dumps(safe)
    assert all(value not in serialized for value in values)
    assert redact_credentials(safe) == safe



def test_live_events_and_disk_trace_share_filtered_evidence_without_mutating_business_data(
        tmp_path):
    identifier = "synthetic-credential-boundary"
    secret = "sk-" + secrets.token_urlsafe(24)
    reply_value = secrets.token_urlsafe(24)
    original = {"message": "api_key=" + secret, "source": "production"}
    event = {"kind": "ok", "result": json.dumps({"api_key": secret, "count": 2}),
             "args": {"api_key": secret, "article_id": 19}, "input_tokens": 123}
    expected = copy.deepcopy(event)
    recorder = trace.start_trace(identifier, 7, "fixture", input_meta=original,
                                 dir=str(tmp_path), name=identifier, by_day=False)
    try:
        trace.record("native", "call", **event)
        recorder.set_reply("Authorization: Bearer " + reply_value)
        assert secret not in json.dumps(recorder.events)
        assert event == expected and secret in original["message"]
        path = trace.finish_trace(identifier, "producer_done", 0.1, 1)
    finally:
        trace.finish_trace(identifier, "cleanup", 0.1, 1)
    doc = json.loads(Path(path).read_text())
    assert secret not in json.dumps(doc) and reply_value not in json.dumps(doc)
    assert doc["events"][0]["args"]["article_id"] == 19
    assert doc["events"][0]["input_tokens"] == 123
    assert json.loads(doc["events"][0]["result"])["count"] == 2
    assert doc["observability"]["credential_redaction"] == 1
    assert Path(path).stat().st_mode & 0o777 == 0o600


def test_json_tool_result_remains_valid_when_numeric_or_array_credentials_are_filtered():
    numeric = 123456
    source = json.dumps({"password": numeric, "access_token": [secrets.token_urlsafe(24)],
                         "count": 2})
    filtered = json.loads(redact_credentials(source))
    assert filtered == {"password": REDACTED, "access_token": REDACTED, "count": 2}


def test_tool_parameter_schemas_are_contracts_not_supplied_credentials(tmp_path):
    numeric = 123
    recorder = trace.start_trace("synthetic-schema", 7, "fixture", dir=str(tmp_path))
    schema = {"function": {"name": "test-tool", "parameters": {"type": "object",
        "properties": {"password": {"type": "string", "description": "Input password"},
                       "token": {"type": "integer"}}}}}
    try:
        trace.record("native", "tool_contract", schema=schema)
        assert recorder.events[-1]["schema"] == schema
        trace.record("native", "call", args={"password": secrets.token_urlsafe(24),
                                             "token": numeric})
        assert recorder.events[-1]["args"] == {"password": REDACTED, "token": REDACTED}
    finally:
        trace.finish_trace("synthetic-schema", "cleanup", 0.1, 0)


def test_deeply_nested_json_text_cannot_break_request_recording():
    value = '[' * 1200 + '{"api_key":"synthetic-key"}' + ']' * 1200
    assert redact_credentials(value) == REDACTED
