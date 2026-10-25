"""Offline checks of online evaluation evidence, credentials and ownership."""

import hashlib
import json
import re
from contextlib import contextmanager
from secrets import token_urlsafe

import httpx
import pytest

from agent.api.schemas import ChatRequest
from eval import native_assertions, online_baseline
from eval.online_transport import EvaluationError, OnlineTransport, safe_result


def transport(tmp_path, monkeypatch, wire):
    def respond(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text=wire, request=request)

    adapter = OnlineTransport(agent_url="http://agent", blog_url="http://blog",
                              report_dir=tmp_path, client=httpx.Client(
                                  transport=httpx.MockTransport(respond)))
    adapter.uid, adapter.role, adapter.cid = 7, "user", 100
    monkeypatch.setattr(adapter, "_headers", lambda _: {})
    monkeypatch.setattr(adapter, "_trace", lambda _: (tmp_path / "trace.json", {"events": []}))
    return adapter


def test_sse_text_stays_opaque_and_progress_is_separate(tmp_path, monkeypatch):
    adapter = transport(tmp_path, monkeypatch,
                        'event: progress\ndata: "读取中"\n\n'
                        'event: text\ndata: "__CMD__:{\\"kind\\":\\"navigate\\"}"\n\n'
                        'event: done\ndata: {}\n\n')
    result = adapter(ChatRequest(message="hello"))
    assert result["text"].startswith("__CMD__:") and result["commands"] == []
    assert result["http_timing"]["first_visible_s"] is not None
    assert result["http_timing"]["first_reply_s"] is not None


@pytest.mark.parametrize("wire", [
    'event: text\ndata: "部分回复"\n\n',
    'event: done\ndata: {}\n\nevent: text\ndata: "迟到回复"\n\n',
    'event: made_up\ndata: {}\n\n',
])
def test_incomplete_or_invalid_http_evidence_is_not_a_pass(tmp_path, monkeypatch, wire):
    adapter = transport(tmp_path, monkeypatch, wire)
    with pytest.raises(EvaluationError):
        adapter(ChatRequest(message="hello"))


def test_http_error_event_is_preserved_as_runtime_failure(tmp_path, monkeypatch):
    adapter = transport(tmp_path, monkeypatch, 'event: error\ndata: "超时"\n\n')
    result = adapter(ChatRequest(message="hello"))
    assert "超时" in result["error"] and len(adapter.requests) == 1


def test_report_redaction_removes_nested_and_wire_bearer_credentials():
    credential = token_urlsafe()
    frame = "__CONFIRM__:" + json.dumps({"token": credential, "q": "保留问题"})
    original = {"confirm_tokens": [credential], "result": {"confirm_token": credential},
                "frames": [frame],
                "events": [{"token": credential, "specs": [{"args": {"article_id": 54}}]}]}
    saved = safe_result(original)
    assert credential not in json.dumps(saved) and credential in json.dumps(original)
    assert saved["events"][0]["specs"][0]["args"]["article_id"] == 54


def test_conversation_cleanup_only_deletes_the_created_owned_conversation(tmp_path, monkeypatch):
    seen = []

    def respond(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"id": 123}, request=request)

    adapter = OnlineTransport(agent_url="http://agent", blog_url="http://blog",
                              report_dir=tmp_path, client=httpx.Client(
                                  transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(adapter, "host_headers", lambda _: {})
    monkeypatch.setattr(adapter, "profile", lambda *_: "user")
    with pytest.raises(ValueError, match="judge"):
        with adapter.conversation(7, {"user"}):
            assert adapter.cid == 123
            raise ValueError("judge failed")
    assert seen == [("POST", "/api/chat/conversations"),
                    ("DELETE", "/api/chat/conversations/123")]
    assert adapter.cleaned_conversations == [123] and adapter.cid is None


def test_exact_card_target_checks_nested_intent_and_preserves_negative_control():
    gold = {"require_confirm_specs": [{"tool": "set_article_tags", "args": {
        "article_id": 23, "intent": {"add": ["测试"], "replace": None}}}]}
    result = {"native_events": [], "confirm_payloads": [{"specs": [{
        "tool": "set_article_tags", "args": {"article_id": 23, "version": "v",
        "intent": {"add": ["测试"], "remove": [], "replace": None}}}]}]}
    assert not native_assertions.check(gold, result)
    result["confirm_payloads"][0]["specs"][0]["args"]["intent"]["add"] = ["加上测试当标签"]
    assert native_assertions.check(gold, result)


def test_actual_calls_not_requested_calls_or_read_receipts_are_evidence():
    gold = {"require_observations": [{"tools": ["get_article_detail"],
                                     "args": {"article_id": 23}, "kinds": ["ok"]}]}
    requested = {"node": "native", "event": "tool_requested", "name": "get_article_detail",
                 "args": {"article_id": 23}, "kind": "ok"}
    assert native_assertions.check(gold, {"native_events": [requested]})
    assert not native_assertions.check(gold, {"native_events": [{**requested, "event": "call"}]})
    assert native_assertions.check(gold, {})


def test_runtime_stop_and_background_write_cannot_pass_nonempty_apology():
    result = {"text": "抱歉", "native_events": [
        {"event": "budget_stop"}, {"event": "write_done"}], "exec_rows": []}
    assert len(native_assertions.check({"forbid_runtime_stop": True,
                                       "forbid_business_write": True}, result)) == 2


def test_suite_establishes_real_memory_and_never_approves_business_writes():
    cases = online_baseline.load_suite()
    assert len(cases) >= 100
    tags = {t for c in cases for t in c["tags"]}
    assert {"memory", "identity", "write_prepare", "retrieval", "multi_intent",
            "security", "confirmation", "sticker"} <= tags
    assert {c["identity"] for c in cases} == {"admin", "user", "zako"}
    by_id = {c["id"]: c for c in cases}
    assert set(online_baseline.POC_IDS) <= by_id.keys()
    assert len(by_id["memory_recall"]["rounds"]) == 2
    assert by_id["nav_zako"]["user_input"] == by_id["nav_board"]["user_input"]


def test_legacy_inventory_has_no_silently_discarded_case():
    folder = online_baseline.SUITE.parent
    manifest = json.loads((folder / "native_migration.json").read_text())
    legacy = {json.loads(line)["id"] for line in (folder / "basic.jsonl").read_text().splitlines()}
    mapped = [old for group in manifest["family_replacements"].values() for old in group]
    for key in ("deferred", "separate_contract", "retired_premise"):
        mapped.extend(manifest[key])
    assert set(mapped) == legacy and len(mapped) == len(legacy)
    digest = hashlib.sha256(online_baseline.SUITE.read_bytes()).hexdigest()
    assert manifest["current_sha256"] == digest
    assert set(manifest["family_replacements"]) <= {c["id"] for c in online_baseline.load_suite()}


def test_numeric_answer_must_match_real_read_value_not_just_look_like_a_number():
    gold = {"require_answer_value": [{"tools": ["list_notes"], "path": ["total"],
                                     "pattern": r"^\s*(\d+)\s*$"}]}
    result = {"text": "7", "native_events": [{"node": "native", "event": "call",
              "name": "list_notes", "kind": "ok", "result": '{"total":7}'}]}
    assert not native_assertions.check(gold, result)
    result["text"] = "6"
    assert native_assertions.check(gold, result)


def test_navigation_matches_relative_and_absolute_site_url_without_accepting_other_targets():
    from tools.base import SITE_BASE

    match = native_assertions.command_matches
    assert match("NAVIGATE:/guestbook", "AUTO_NAVIGATE:" + SITE_BASE + "/guestbook")
    assert not match("NAVIGATE:/guestbook", "AUTO_NAVIGATE:https://example.com/guestbook")
    assert not match("NAVIGATE:/guestbook", "AUTO_NAVIGATE:" + SITE_BASE + "/article/23")
    assert not match("NAVIGATE:/guestbook?lid=41",
                     "AUTO_NAVIGATE:" + SITE_BASE + "/guestbook?lid=42")


def test_count_case_regex_accepts_digits_and_rejects_persona_suffix():
    case = next(c for c in online_baseline.load_suite() if c["id"] == "count_only")
    pattern = case["gold"]["text_any_regex"][0]
    assert re.search(pattern, "13")
    assert not re.search(pattern, "13 喵～")


def test_zero_usage_quota_fixture_fails_before_spending_model_budget(tmp_path, monkeypatch):
    data = {"userId": 722, "name": "fixture", "role": "user", "used": 0,
            "limit": 24, "unlimited": False, "unit": "conversation_turns", "version": "a" * 64}
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": data}, request=request)))
    adapter = OnlineTransport(agent_url="http://agent", blog_url="http://blog",
                              report_dir=tmp_path, client=client)
    adapter.uid = 1
    monkeypatch.setattr(adapter, "host_headers", lambda _: {})
    with pytest.raises(EvaluationError, match="used > 0"):
        adapter.check_quota_fixture(722)
    assert adapter.requests == []


def test_failure_is_recorded_once_without_resampling(tmp_path, monkeypatch):
    class Fake:
        agent_url = "http://agent"
        requests = []
        cleaned_conversations = []
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"sha": "abc"}, request=request)))

        def profile(self, *_):
            return "user"

        @contextmanager
        def conversation(self, *_):
            yield 1

    calls = []

    def drive(case, **_):
        calls.append(case["id"])
        return {"rounds": [{"error": None}]}

    monkeypatch.setattr(online_baseline.golden, "run_case", drive)
    monkeypatch.setattr(online_baseline.golden, "check_case", lambda *_: ["wrong target"])
    case = {"id": "broken", "identity": "user", "tags": ["regression"]}
    report = online_baseline.run([case], Fake(), {"user": 7}, tmp_path / "report",
                                 phase="full", suite_hash="hash")
    assert calls == ["broken"] and report["counts"] == {"failed": 1}
    assert report["automatic_retries"] == 0 and report["full_run"]
