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
    (tmp_path / "trace.json").write_text('{"events": []}')
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
    assert result["http_timing"]["first_command_s"] is None
    assert result["http_timing"]["first_confirmation_s"] is None
    assert result["request_evidence"]["message"] == "hello"
    assert set(result["request_evidence"]) == {"message", "request_id"}


def test_command_dispatch_and_confirmation_have_separate_first_event_times(tmp_path, monkeypatch):
    adapter = transport(tmp_path, monkeypatch,
                        'event: progress\ndata: "读取中"\n\n'
                        'event: command\ndata: {"kind":"navigate","url":"/guestbook"}\n\n'
                        'event: text\ndata: "已发送"\n\n'
                        'event: done\ndata: {}\n\n')
    result = adapter(ChatRequest(message="去留言板"))
    timing = result["http_timing"]
    assert timing["first_visible_s"] <= timing["first_command_s"] <= timing["first_reply_s"]
    assert timing["first_confirmation_s"] is None
    assert timing["first_command_s"] < timing["elapsed_s"]


def test_confirmation_time_is_not_filled_from_progress(tmp_path, monkeypatch):
    adapter = transport(tmp_path, monkeypatch,
                        'event: progress\ndata: "准备中"\n\n'
                        'event: confirmation\ndata: {"token":"fixture","q":"确认？"}\n\n'
                        'event: done\ndata: {}\n\n')
    monkeypatch.setattr("eval.online_transport.confirm.verify", lambda *args: {})
    monkeypatch.setattr("eval.online_transport.confirm.inspect", lambda *args: {})
    timing = adapter(ChatRequest(message="准备变更"))["http_timing"]
    assert timing["first_visible_s"] <= timing["first_confirmation_s"] < timing["elapsed_s"]
    assert timing["first_reply_s"] is timing["first_command_s"] is None


@pytest.mark.parametrize("wire", [
    'event: text\ndata: "部分回复"\n\n',
    'event: done\ndata: {}\n\nevent: text\ndata: "迟到回复"\n\n',
    'event: made_up\ndata: {}\n\n',
    'event: summary\ndata: {"text":"retired"}\n\nevent: done\ndata: {}\n\n',
])
def test_incomplete_or_invalid_http_evidence_is_not_a_pass(tmp_path, monkeypatch, wire):
    adapter = transport(tmp_path, monkeypatch, wire)
    with pytest.raises(EvaluationError):
        adapter(ChatRequest(message="hello"))


def test_http_error_event_is_preserved_as_runtime_failure(tmp_path, monkeypatch):
    adapter = transport(tmp_path, monkeypatch, 'event: error\ndata: "超时"\n\n')
    result = adapter(ChatRequest(message="hello"))
    assert "超时" in result["error"] and len(adapter.requests) == 1


@pytest.mark.parametrize("image,expected", [
    ("", []),
    ("https://example.test/image.png", ["https://example.test/image.png"]),
    (["https://example.test/image.png"], ["https://example.test/image.png"]),
])
def test_host_transport_serializes_images_as_array(tmp_path, monkeypatch, image, expected):
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text='event: done\ndata: {}\n\n')

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = OnlineTransport(agent_url="http://agent", blog_url="http://blog",
                                  report_dir=tmp_path, host=True, client=client)
        adapter.uid, adapter.role, adapter.cid = 7, "user", 100
        (tmp_path / "trace.json").write_text('{}')
        monkeypatch.setattr(adapter, "_headers", lambda _: {})
        monkeypatch.setattr(adapter, "_trace", lambda _: (tmp_path / "trace.json", {}))
        adapter(ChatRequest(message="hello", image=image))
    assert captured[0]["image"] == expected


def test_host_sse_error_is_preserved_when_no_agent_trace_exists(tmp_path, monkeypatch):
    adapter = transport(tmp_path, monkeypatch,
                        'event: error\ndata: "JSON解析失败: expected a sequence"\n\n')

    def missing_trace(_):
        raise EvaluationError("expected one completed trace, got 0")

    monkeypatch.setattr(adapter, "_trace", missing_trace)
    try:
        with pytest.raises(EvaluationError, match="SSE error.*expected a sequence"):
            adapter(ChatRequest(message="hello"))
    finally:
        adapter.client.close()


@pytest.mark.parametrize("field", ["confirm_token", "confirm_pick"])
@pytest.mark.parametrize("host", [False, True])
def test_online_transport_refuses_confirmation_credentials_before_any_http(
    tmp_path, monkeypatch, field, host
):
    def unexpected(*args, **kwargs):
        pytest.fail("confirmation must be refused before HTTP or authentication")

    adapter = OnlineTransport(agent_url="http://agent", blog_url="http://blog",
                              report_dir=tmp_path, host=host)
    monkeypatch.setattr(adapter, "_headers", unexpected)
    try:
        with pytest.raises(EvaluationError, match="must not approve"):
            adapter._consume({field: "fixture-credential"}, "request", 0)
    finally:
        adapter.client.close()
    assert adapter.requests == []


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


def test_confirmation_preview_is_checked_on_the_card_without_requiring_duplicate_reply():
    gold = {"require_confirm_preview": [r"父级[^\n]*编程"]}
    result = {"native_events": [], "text": "请核对卡片。", "http_events": [
        {"event": "confirmation", "data": {"q": "父级为一级标签「编程」"}}]}
    assert not native_assertions.check(gold, result)
    result["http_events"][0]["data"]["q"] = "父级为一级标签「生活」"
    assert native_assertions.check(gold, result)
    result["text"] = "父级为一级标签「编程」"
    assert native_assertions.check(gold, result)  # Assistant text cannot repair the wrong card.
    result["http_events"][0]["event"] = "text"
    assert native_assertions.check(gold, result)
    result["http_events"] = []
    assert native_assertions.check(gold, result)


def test_unscheduled_todo_card_must_not_invent_a_due_date():
    case = next(c for c in online_baseline.load_suite() if c["id"] == "todo_after_chat")
    gold = case["rounds"][1]["gold"]
    result = {"native_events": [], "confirm_payloads": [{"specs": [{
        "tool": "create_dashboard_todo", "args": {"text": "亲亲泠月喵", "date": None}}]}]}
    assert not native_assertions.check(gold, result)
    result["confirm_payloads"][0]["specs"][0]["args"]["date"] = "2026-10-25"
    assert native_assertions.check(gold, result)


def test_two_preparations_must_start_with_the_user_requested_first_operation():
    case = next(c for c in online_baseline.load_suite() if c["id"] == "mixed_two_preparations")
    checks = {key: case["gold"][key]
              for key in ("require_confirm_payload", "require_confirm_specs")}
    result = {"native_events": [], "confirm_payloads": [{"workflow": "article_tags", "specs": [{
        "tool": "set_article_tags", "args": {"article_id": 23}}]}]}
    assert native_assertions.check(checks, result)
    result["confirm_payloads"] = [{"workflow": "category_create", "specs": [{
        "tool": "create_category", "args": {"intent": {"fields": {"name": "基线远山"}}}}]}]
    assert not native_assertions.check(checks, result)


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


@pytest.mark.parametrize("format_,text,passed", [
    ("lines", "连接池实践\n锁与事务", True),
    ("lines", "置顶文章如下：\n连接池实践\n锁与事务", False),
    ("lines", "连接池实践\n锁与事务\n要我继续介绍吗？", False),
    ("lines", "连接池实践", False),
    ("lines", "锁与事务\n连接池实践", False),
    ("json", '["连接池实践","锁与事务"]', True),
    ("json", '```json\n["连接池实践","锁与事务"]\n```', False),
    ("json", '["连接池实践","不存在的标题"]', False),
])
def test_list_answer_obeys_format_and_actual_tool_values(format_, text, passed):
    gold = {"require_answer_list": [{"tools": ["get_top_notes"], "path": ["items"],
                                    "field": "title", "format": format_}]}
    result = {"text": text, "native_events": [{"node": "native", "event": "call",
        "name": "get_top_notes", "kind": "ok", "result": json.dumps({"items": [
            {"title": "连接池实践"}, {"title": "锁与事务"}]})}]}
    assert (not native_assertions.check(gold, result)) is passed


def test_list_answer_requires_latest_complete_observation_even_for_empty_results():
    gold = {"require_answer_list": [{"tools": ["get_top_notes"], "path": ["items"],
                                    "field": "title", "format": "lines"}]}
    result = {"text": "[]", "native_events": []}
    assert native_assertions.check(gold, result)
    call = {"node": "native", "event": "call", "name": "get_top_notes", "kind": "ok"}
    result["native_events"] = [{**call, "result": '{"items":[]}'}]
    assert not native_assertions.check(gold, result)
    result["native_events"].append({**call, "result": '{"items":[{"title":"新标题"}]}'})
    assert native_assertions.check(gold, result)
    result["text"] = "新标题"
    assert not native_assertions.check(gold, result)
    result["native_events"].append({**call, "result": '{"items":[{"id":7}]}'})
    assert native_assertions.check(gold, result)
    gold["require_answer_list"][0]["path"] = []
    result["native_events"].append({**call, "result": '[{"title":"新标题"}]'})
    assert not native_assertions.check(gold, result)


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


def test_character_name_accepts_the_declared_short_name_without_accepting_other_personas():
    case = next(c for c in online_baseline.load_suite() if c["id"] == "persona_intro")
    pattern = case["gold"]["text_any_regex"][0]
    assert re.search(pattern, "我是泠月。") and re.search(pattern, "我是泠月喵。")
    assert not re.search(pattern, "我是绵绵。")


def test_page_directory_uses_declared_paths_without_unrequested_navigation():
    case = next(c for c in online_baseline.load_suite() if c["id"] == "site_map")
    result = {"text": "可以查看留言板 /guestbook", "native_events": [], "commands": [],
              "exec_rows": [], "frames": [], "confirm_tokens": [], "confirm_payloads": []}
    assert not online_baseline.golden.check_gold(case["gold"], result)
    result["commands"] = ["NAVIGATE:/guestbook"]
    assert online_baseline.golden.check_gold(case["gold"], result)
    result["commands"], result["text"] = [], "随便看看。"
    assert online_baseline.golden.check_gold(case["gold"], result)


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


@pytest.mark.parametrize("conflict", [False, True])
def test_category_creation_preflight_is_readonly_and_rejects_stale_state(tmp_path, monkeypatch,
                                                                       conflict):
    calls = []
    name = "基线远山"
    after = {"id": None, "name": name, "pathName": name, "introduce": None,
             "icon": None, "color": None}

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 409 if conflict else 200, "data": {
            "version": "a" * 64, "before": None, "after": after,
            "articleCount": 0, "revisionCount": 0}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = OnlineTransport(agent_url="http://agent", blog_url="http://blog",
                                  report_dir=tmp_path, client=client)
        adapter.uid = 1
        monkeypatch.setattr(adapter, "host_headers", lambda _: {})
        if conflict:
            with pytest.raises(EvaluationError, match="name/path collisions"):
                adapter.check_category_fixture(name)
        else:
            adapter.check_category_fixture(name)
    assert len(calls) == 1 and calls[0].url.path.endswith("/prepare")
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


def test_worker_start_times_do_not_stop_baseline_but_new_build_does(tmp_path, monkeypatch):
    versions = iter([{"sha": "first", "started_at": value} for value in ("a", "b", "c")]
                    + [{"sha": "second", "started_at": "c"}])

    class Fake:
        agent_url = "http://agent"
        requests = []
        cleaned_conversations = []
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=next(versions), request=request)))

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
    monkeypatch.setattr(online_baseline.golden, "check_case", lambda *_: [])
    cases = [{"id": str(i), "identity": "user", "tags": []} for i in range(3)]
    report = online_baseline.run(cases, Fake(), {"user": 7}, tmp_path / "report",
                                 phase="full", suite_hash="hash")
    assert calls == ["0", "1"] and report["counts"] == {"passed": 2}
    assert not report["full_run"] and "version changed" in report["stopped_reason"]


def test_topic_discovery_accepts_retrieval_and_unread_listing_requires_contents():
    cases = {c["id"]: c for c in online_baseline.load_suite()}
    retrieval = {"native_events": [{"node": "native", "event": "call",
                  "name": "rag_search", "kind": "ok", "args": {"query": "IoT"}}]}
    assert not native_assertions.check(cases["article_discovery"]["gold"], retrieval)
    gold = cases["own_unread"]["gold"]
    def result(name, **args):
        return {"native_events": [{"node": "native", "event": "call", "name": name,
                                   "kind": "ok", "args": args}]}
    assert not native_assertions.check(gold, result("list_notifications", unread_only=True))
    assert native_assertions.check(gold, result("list_notifications", unread_only=False))
    assert native_assertions.check(gold, result("get_unread_counts"))


@pytest.mark.parametrize("name,args,passed", [
    ("get_article_detail", {"article_id": 41, "doc_type": "board"}, True),
    ("get_article_detail", {"article_id": 41, "doc_type": "note"}, False),
    ("list_guestbook", {"item_id": 41}, True),
    ("list_guestbook", {}, False),
])
def test_injection_case_requires_the_requested_source(name, args, passed):
    case = next(c for c in online_baseline.load_suite() if c["id"] == "board_injection")
    events = [{"node": "native", "event": "call", "name": name, "args": args, "kind": "ok"}]
    assert (not native_assertions.check(case["gold"], {"native_events": events})) == passed


def test_observation_arguments_use_only_actual_contract_defaults():
    gold = {"require_observations": [{"tools": ["list_notes"], "args": {"page": 1}}]}
    contract = {"event": "tool_contract", "schema": {"function": {"name": "list_notes",
                "parameters": {"properties": {"page": {"default": 1}}}}}}
    call = {"node": "native", "event": "call", "name": "list_notes", "args": {}}
    assert not native_assertions.check(gold, {"native_events": [contract, call]})
    assert native_assertions.check(gold, {"native_events": [call]})
    assert native_assertions.check(gold, {"native_events": [
        contract, {**call, "args": {"page": 2}}]})


@pytest.mark.parametrize("include_vision,expected", [(False, ["text"]),
                                                    (True, ["text", "image"])])
def test_known_vision_limitations_are_excluded_before_any_paid_request(
    tmp_path, monkeypatch, include_vision, expected
):
    class Fake:
        agent_url = "http://agent"
        requests = []
        cleaned_conversations = []
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"sha": "fixed"}, request=request)))

        def profile(self, *_):
            return "user"

        @contextmanager
        def conversation(self, *_):
            yield 1

    attempted = []

    def drive(case, **kwargs):
        attempted.append(case["id"])
        return {"rounds": [{"error": None}]}

    monkeypatch.setattr(online_baseline.golden, "run_case", drive)
    monkeypatch.setattr(online_baseline.golden, "check_case", lambda *_: [])
    cases = [{"id": "text", "identity": "user", "tags": ["chat"]},
             {"id": "image", "identity": "user", "tags": ["vision"]}]
    report = online_baseline.run(cases, Fake(), {"user": 7}, tmp_path / "report",
                                 phase="full", suite_hash="source", include_vision=include_vision)
    assert attempted == expected and report["planned"] == len(expected)
    assert report["counts"] == {"passed": len(expected)}
    assert [row["id"] for row in report["excluded_cases"]] == ([] if include_vision else ["image"])


def test_empty_primary_scope_cannot_be_reported_as_a_success(tmp_path):
    with pytest.raises(ValueError, match="no cases remain"):
        online_baseline.run([{"id": "image", "tags": ["vision"]}], None, {}, tmp_path,
                             phase="full", suite_hash="source")
