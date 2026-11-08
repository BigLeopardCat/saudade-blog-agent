"""SDK-to-business-HTTP contract checks with scripted, offline model responses.

Passing these tests proves the efficiency checks reject the injected regressions;
it does not measure live-model task success, provider latency or cache hits.
"""

import json

import httpx
import pytest

from eval import efficiency_contracts as contracts
from eval import native_contracts
from models.llm import get_llm
from models.transport import ModelIO


def call(name, **args):
    return {"name": name, "args": args}


@pytest.fixture
def sdk(tmp_path):
    clients = []
    patcher = pytest.MonkeyPatch()

    def install(responses):
        answers = iter(responses)
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            response = next(answers)
            calls = response if isinstance(response, list) else []
            delta = {"role": "assistant", "content": "" if calls else response}
            if calls:
                delta["tool_calls"] = [
                    {"index": index, "id": f"call_{len(requests)}_{index}", "type": "function",
                     "function": {"name": action["name"], "arguments": json.dumps(action["args"])}}
                    for index, action in enumerate(calls)]
            chunk = {"id": "offline", "model": "test", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": delta,
                                  "finish_reason": "tool_calls" if calls else "stop"}],
                     "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

        client = httpx.Client(transport=httpx.MockTransport(handle))
        clients.append(client)
        patcher.setattr(native_contracts, "ModelIO",
                        lambda: ModelIO(transport=httpx.MockTransport(handle)))
        patcher.setattr("agent.runtime.service.get_llm", lambda **kwargs: get_llm(
            model="test", api_key="offline-test-only", base_url="https://model.invalid/v1",
            http_client=client, http_socket_options=(), **kwargs))
        patcher.setattr(native_contracts.golden_trace, "trace_root",
                        lambda: str(tmp_path / "traces"))
        return requests

    yield install
    for client in clients:
        client.close()
    patcher.undo()


def duplicate_business_http(monkeypatch, suffix):
    """A transport regression hidden behind one successful model tool invocation."""
    original = httpx.MockTransport.handle_request

    def send(transport, request):
        if request.url.path.endswith(suffix):
            original(transport, request).close()
        return original(transport, request)

    monkeypatch.setattr(httpx.MockTransport, "handle_request", send)


def result(case, requests):
    row = native_contracts.run([case])["cases"][0]
    assert "evidence" in row, row
    evidence = row["evidence"]
    assert evidence["model_attempts"] == len(requests), row
    assert evidence["model_http_requests"] == len(requests), row
    assert evidence["business_api"] == "synthetic"
    assert row["evaluated"] is True
    return row


@pytest.mark.parametrize("mode", ["scoped", "duplicate_http", "whole_body", "oversized_section",
                                  "followup_reread"])
def test_rag_correct_answer_still_fails_for_duplicate_or_excess_body_reads(sdk, monkeypatch, mode):
    with contracts.article_backend() as state:
        hit = state["index"]._lexical_ranked(contracts.QUERY, 1, article_id=contracts.ARTICLE_ID)[0]
    match = next(row for row in hit["matches"] if row["section_id"] == "s2")
    args = {"article_id": contracts.ARTICLE_ID, "section": "s2", "offset": match["offset"],
            "max_chars": match["end"] - match["offset"],
            "revision": hit["document_revision"], "section_schema": hit["section_schema"]}
    if mode == "duplicate_http":
        duplicate_business_http(monkeypatch, f"/documents/note/{contracts.ARTICLE_ID}/read")
    elif mode == "whole_body":
        args.update(section="", offset=0, max_chars=12000)
    elif mode == "oversized_section":
        args.update(offset=0, max_chars=12000)
    responses = [
        [call("rag_search", query=contracts.QUERY, article_id=contracts.ARTICLE_ID, top_k=1)],
        [call("get_article_detail", **args)], contracts.FACT,
    ]
    if mode in {"scoped", "followup_reread"}:
        if mode == "followup_reread":
            responses.append([call("get_article_detail", **args)])
        responses.append(contracts.FACT)
    requests = sdk(responses)
    row = result("efficiency_scoped_article", requests)
    assert row["passed"] is (mode == "scoped"), row
    assert contracts.FACT in row["rounds"][0]["text"]
    assert len(requests) == {"scoped": 4, "followup_reread": 5}.get(mode, 3)
    assert row["evidence"]["tool_requests"] == (3 if mode == "followup_reread" else 2)
    assert row["business_writes"] == 0
    if mode == "scoped":
        assert contracts.FACT in row["rounds"][1]["text"]
        assert row["evidence"]["business_http_requests"] == 2
    if mode == "duplicate_http":
        assert row["evidence"]["business_http_requests"] == 3
        assert "fetched repeatedly" in row["reason"]
    elif mode == "whole_body":
        assert "whole-body read" in row["reason"]
    elif mode == "oversized_section":
        assert "HTTP body exceeded" in row["reason"]
    elif mode == "followup_reread":
        assert contracts.FACT in row["rounds"][1]["text"]
        assert "followup repeated business reads" in row["reason"]


@pytest.mark.parametrize("mode", ["batch", "serial", "parallel", "duplicate_http"])
def test_keyword_expansion_checks_backend_requests_not_only_models_or_tools(sdk, monkeypatch, mode):
    batch = [call("search_public_activity", keywords=contracts.WORDS)]
    individual = [call("search_public_activity", keywords=[word]) for word in contracts.WORDS]
    actions = [[action] for action in individual] if mode == "serial" else [
        individual if mode == "parallel" else batch]
    if mode == "duplicate_http":
        duplicate_business_http(monkeypatch, "/activity/search")
    requests = sdk([*actions, "猫咪和小猫都在喵喵叫。"])
    row = result("efficiency_expanded_search", requests)
    assert row["passed"] is (mode == "batch"), row
    assert row["rounds"][0]["text"] == "猫咪和小猫都在喵喵叫。"
    assert row["business_writes"] == 0
    if mode == "duplicate_http":
        assert len(requests) == 2 and row["evidence"]["tool_requests"] == 1
        assert row["evidence"]["business_http_requests"] == 2
    if mode == "parallel":
        assert len(requests) == 2 and row["evidence"]["tool_requests"] == 3
    if mode != "batch":
        assert "repeated business HTTP" in row["reason"]


@pytest.mark.parametrize("mode", ["reuse", "reread", "duplicate_http"])
def test_gallery_followup_reuses_observed_image_without_business_io(sdk, monkeypatch, mode):
    if mode == "duplicate_http":
        duplicate_business_http(monkeypatch, "/gallery/images/72")
    responses = [[call("load_admin_gallery_info_tools")],
                 [call("get_gallery_image_metadata", image_id=72)], "文件名是学院风。"]
    if mode == "reread":
        responses.append([call("get_gallery_image_metadata", image_id=72)])
    responses.append(f"![学院风]({contracts.IMAGE_URL})")
    requests = sdk(responses)
    row = result("efficiency_gallery_reuse", requests)
    assert row["passed"] is (mode == "reuse"), row
    assert row["rounds"][1]["text"] == f"![学院风]({contracts.IMAGE_URL})"
    assert contracts.IMAGE_URL in json.dumps(requests[-1]["messages"])
    assert row["business_writes"] == 0
    if mode != "reuse":
        assert "already-read metadata" in row["reason"]


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("prelist", [False, True])
def test_partial_write_replay_has_one_write_and_cannot_claim_all_completed(sdk, completed, prelist):
    reply = json.dumps({"101": True, "102": completed})
    responses = [[call("load_notifications_info_tools")]]
    if prelist:
        responses.append([call("list_notifications", unread_only=True)])
    responses.extend([
        [call("prepare_notification_read", scope="all_unread")],
        "请核对确认卡，批准后标记。",
        [call("list_notifications")], reply,
    ])
    requests = sdk(responses)
    row = result("efficiency_partial_write_replay", requests)
    assert row["passed"] is (not completed and not prelist), row
    assert row["requests"] == len(row["rounds"]) == 3
    assert row["rounds"][0]["model_calls"] == 3 + prelist
    assert row["business_writes"] == 1
    assert row["evidence"]["business_http_counts"] == {
        "GET /api/protected/notifications/state": 1,
        "GET /api/protected/notifications": 1 + prelist,
        "POST /api/protected/notifications/read": 1,
    }
    assert row["rounds"][1]["text"] == reply
    assert row["rounds"][2]["model_calls"] == 0
    assert not row["rounds"][1]["exec_rows"] and not row["rounds"][2]["exec_rows"]
    if completed:
        assert "contradicts" in row["reason"]
    elif prelist:
        assert "4 > 3" in row["reason"]
