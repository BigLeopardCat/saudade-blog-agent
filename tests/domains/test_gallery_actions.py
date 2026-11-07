"""Fixed gallery selections use one approval and one authoritative mutation receipt."""

import copy
import json
import secrets
import threading
import time
from contextlib import closing

import httpx
import pytest
from _own_support import config
from pydantic import ValidationError

from agent import confirm
from agent.domains.gallery import image_actions, preparation, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.turns import AgentCancelled
from eval.fixtures.ledger import Ledger
from tools import base


def cfg(**kwargs):
    return config(principal=Principal(7, "admin", SOURCE_ASSERTION), **kwargs)


@pytest.fixture
def backend(monkeypatch):
    state = {"requests": [], "effects": 0, "version": "a" * 64}

    def handle(request):
        state["requests"].append(request)
        assert request.method == "POST"
        body = json.loads(request.content)
        if request.url.path == image_actions.PATH + "/prepare":
            ids = body.get("imageIds", [item["imageId"] for item in body.get("items", [])])
            destination = ({"id": body["folderId"], "name": "封面", "parentId": 0}
                           if body["action"] == "move" else None)
            result = {"version": state["version"], "expiresAt": int(time.time()) + 900,
                      "intent": body, "matchedIds": ids,
                      "preview": [{"imageId": i, "displayName": "[图片](https://evil.invalid)",
                          "folderId": 0, "isFavorite": False} for i in ids[:10]],
                      "previewTruncated": len(ids) > 10, "destination": destination}
            if state.get("bad_plan"):
                state["bad_plan"](result)
        else:
            assert request.url.path == image_actions.PATH
            if body["version"] != state["version"]:
                return httpx.Response(409, json={"code": 409, "message": "state changed"})
            intent = body["intent"]
            ids = intent.get("imageIds", [item["imageId"] for item in intent.get("items", [])])
            changed = [] if state.get("noop") else ids
            result = {"action": intent["action"], "beforeVersion": body["version"],
                      "matchedIds": ids, "changedIds": changed, "changed": bool(changed),
                      "cleanupPendingIds": ids if intent["action"] == "delete" else []}
            state["effects"] += bool(changed)
            if state.get("lost"):
                raise httpx.ReadTimeout("response lost after effect", request=request)
            if state.get("bad_result"):
                state["bad_result"](result)
        return httpx.Response(200, json={"code": 200, "data": result})

    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


def approve(intent):
    prepared = preparation.prepare_gallery_images.invoke({"intent": intent}, config=cfg())
    token = prepared.execution["pending_confirm"]["token"]
    grant = confirm.verify(token, 7, 42)
    assert grant and len(grant["specs"]) == 1
    assert confirm.verify(token, 8, 42) is None
    assert confirm.verify(token, 7, 43) is None
    assert confirm.verify(token[:-5] + "wrong", 7, 42) is None
    return prepared, grant


@pytest.mark.parametrize("intent", [
    {"action": "move", "imageIds": list(range(100, 0, -1)), "folderId": 2},
    {"action": "move", "imageIds": [11], "folderId": 0},
    {"action": "favorite", "imageIds": [12, 11], "isFavorite": False},
    {"action": "delete", "imageIds": [12, 11]},
    {"action": "metadata", "items": [
        {"imageId": 12, "changes": {"description": "", "isFavorite": False}},
        {"imageId": 11, "changes": {"displayName": "[新名称]", "folderId": 2}}]},
])
def test_one_readonly_preparation_then_one_atomic_request_and_durable_replay(
    backend, tmp_path, intent
):
    prepared, grant = approve(intent)
    canonical = image_actions.intent(intent)
    assert grant["specs"][0]["args"]["intent"] == canonical
    assert backend["effects"] == 0 and len(backend["requests"]) == 1
    assert "https://evil.invalid)" not in prepared.reply
    if len(image_actions.parse(intent).identifiers()) > 10:
        assert "仅预览前 10 张" in prepared.reply and "100" in prepared.reply
    with closing(Ledger(str(tmp_path / "ledger.db"))) as ledger:
        for _ in range(2):
            result = transactions.execute(grant, cfg(), None, ledger=ledger)
            assert len(result["receipts"]) == 1 and not result["blocked"]
            assert result["receipts"][0]["args"]["intent"] == canonical
    assert len(backend["requests"]) == 2 and backend["effects"] == 1
    assert json.loads(backend["requests"][1].content) == grant["specs"][0]["args"]
    if intent["action"] == "delete":
        assert "清理已排队" in result["reply"] and "尚未核实物理文件清除" in result["reply"]


def test_observed_noop_is_not_an_unverified_missing_result(backend, tmp_path):
    _, grant = approve({"action": "favorite", "imageIds": [11], "isFavorite": True})
    backend["noop"] = True
    with closing(Ledger(str(tmp_path / "ledger.db"))) as ledger:
        result = transactions.execute(grant, cfg(), None, ledger=ledger)
    assert result["receipts"] and "更新 0 张" in result["reply"]
    assert backend["effects"] == 0 and len(backend["requests"]) == 2


@pytest.mark.parametrize("change", [
    {"matchedIds": [12]}, {"changedIds": [12]}, {"changedIds": [11, 11]},
    {"beforeVersion": "b" * 64}, {"action": "delete"}, {"changed": False},
    {"cleanupPendingIds": [11]}, {"extra": True},
])
def test_invalid_receipt_never_becomes_success_or_repeats_a_sent_write(
    backend, tmp_path, change
):
    _, grant = approve({"action": "favorite", "imageIds": [11], "isFavorite": True})
    backend["bad_result"] = lambda result: result.update(change)
    with closing(Ledger(str(tmp_path / "ledger.db"))) as ledger:
        for _ in range(2):
            result = transactions.execute(grant, cfg(), None, ledger=ledger)
            assert not result["receipts"] and result["operation_outcome"]["status"] == "unverified"
    assert len(backend["requests"]) == 2 and backend["effects"] == 1


@pytest.mark.parametrize("change", [
    {"cleanupPendingIds": []}, {"changedIds": []}, {"cleanupPendingIds": [11]},
])
def test_delete_must_prove_all_approved_ids_and_durable_cleanup(backend, tmp_path, change):
    _, grant = approve({"action": "delete", "imageIds": [11, 12]})
    backend["bad_result"] = lambda result: result.update(change)
    with closing(Ledger(str(tmp_path / "ledger.db"))) as ledger:
        result = transactions.execute(grant, cfg(), None, ledger=ledger)
    assert result["blocked"] and not result["receipts"]


def test_lost_response_preserves_unknown_without_an_automatic_second_write(backend, tmp_path):
    _, grant = approve({"action": "delete", "imageIds": [11, 12]})
    backend["lost"] = True
    with closing(Ledger(str(tmp_path / "ledger.db"))) as ledger:
        for _ in range(2):
            result = transactions.execute(grant, cfg(), None, ledger=ledger)
            assert result["operation_outcome"]["status"] == "unverified"
            assert not result["receipts"]
    assert backend["effects"] == 1 and len(backend["requests"]) == 2


def test_version_conflict_does_not_reprepare_or_partially_retry(backend, tmp_path):
    _, grant = approve({"action": "move", "imageIds": [11, 12], "folderId": 2})
    backend["version"] = "b" * 64
    with closing(Ledger(str(tmp_path / "ledger.db"))) as ledger:
        result = transactions.execute(grant, cfg(), None, ledger=ledger)
    assert not result["receipts"] and backend["effects"] == 0
    assert len(backend["requests"]) == 2


@pytest.mark.parametrize("change", [
    {"matchedIds": [11]}, {"matchedIds": [12, 11]}, {"expiresAt": 1},
    {"previewTruncated": True}, {"preview": []},
    {"preview": [], "previewTruncated": True},
    {"destination": {"id": 3, "name": "wrong", "parentId": 0}},
    {"intent": {"action": "delete", "imageIds": [11, 12]}},
])
def test_bad_preparation_never_issues_an_approval(backend, change):
    backend["bad_plan"] = lambda result: result.update(copy.deepcopy(change))
    result = preparation.prepare_gallery_images.invoke(
        {"intent": {"action": "move", "imageIds": [11, 12], "folderId": 2}}, config=cfg())
    assert result.kind == "unavailable" and backend["effects"] == 0
    assert len(backend["requests"]) == 1


@pytest.mark.parametrize("intent", [
    {"action": "delete", "imageIds": []}, {"action": "delete", "imageIds": [True]},
    {"action": "delete", "imageIds": [11, 11]},
    {"action": "delete", "imageIds": list(range(1, 102))},
    {"action": "delete", "imageIds": [11], "all": True},
    {"action": "delete", "imageIds": [11], "folderId": None},
    {"action": "favorite", "imageIds": [11], "isFavorite": 1},
    {"action": "move", "imageIds": [11], "folderId": -1},
    {"action": "metadata", "items": [{"imageId": 11, "changes": {}}]},
    {"action": "metadata", "items": [{"imageId": 11, "changes": {"description": None}}]},
    {"action": "metadata", "items": [{"imageId": i, "changes": {"description": "猫" * 2000}}
                                        for i in range(1, 3)]},
])
def test_invalid_or_oversized_selection_is_rejected_before_any_http(backend, intent):
    with pytest.raises(ValidationError):
        preparation.prepare_gallery_images.invoke({"intent": intent}, config=cfg())
    assert backend["requests"] == []


@pytest.mark.parametrize("principal", [
    Principal(7, "user", SOURCE_ASSERTION), Principal(7, "admin", "body"),
    Principal(8, "admin", SOURCE_ASSERTION),
])
def test_wrong_identity_cannot_prepare_or_execute(backend, tmp_path, principal):
    _, grant = approve({"action": "delete", "imageIds": [11]})
    wrong = config(principal=principal)
    result = preparation.prepare_gallery_images.invoke(
        {"intent": {"action": "delete", "imageIds": [11]}}, config=wrong)
    assert result.kind == "unavailable"
    with closing(Ledger(str(tmp_path / "ledger.db"))) as ledger:
        result = transactions.execute(grant, wrong, None, ledger=ledger)
    assert result["blocked"] and not result["receipts"]
    assert len(backend["requests"]) == 1 and backend["effects"] == 0


def test_cancellation_and_replaced_conversation_do_not_execute(backend, tmp_path):
    _, grant = approve({"action": "delete", "imageIds": [11]})
    event = threading.Event()
    event.set()
    with closing(Ledger(str(tmp_path / "ledger.db"))) as ledger:
        with pytest.raises(AgentCancelled):
            transactions.execute(grant, cfg(stop_event=event), None, ledger=ledger)
        result = transactions.execute(grant, cfg(conversation_id=43, thread_id="conv_43"),
                                      None, ledger=ledger)
        assert not result["receipts"] and result["blocked"]
    assert len(backend["requests"]) == 1 and backend["effects"] == 0


def test_utf8_intent_budget_still_fits_the_signed_confirmation(backend):
    intent = {"action": "metadata", "items": [
        {"imageId": 11, "changes": {"description": "猫" * 2000}},
        {"imageId": 12, "changes": {"description": "猫" * 600}}]}
    prepared, grant = approve(intent)
    assert len(prepared.execution["pending_confirm"]["token"]) < confirm.MAX_TOKEN_CHARS
    assert grant["specs"][0]["args"]["intent"] == intent
    assert "猫" * 2000 in prepared.reply and backend["effects"] == 0


@pytest.mark.parametrize("tamper", [False, True])
def test_cover_keeps_its_separate_article_and_image_version_contract(
    monkeypatch, tmp_path, tamper
):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/images/11"):
            data = {"imageKey": 11, "imageUrl": "https://images.example/cover.png",
                    "displayName": "封面", "description": "", "folderId": 0,
                    "isFavorite": False, "version": "a" * 64}
        elif request.method == "GET":
            data = {"articleId": 3, "title": "文章", "version": "b" * 64}
        else:
            assert request.method == "PUT"
            body = json.loads(request.content)
            assert body == {"version": "b" * 64, "imageId": 11,
                            "imageVersion": "a" * 64, "fit": "contain"}
            data = {"beforeVersion": "b" * 64, "changed": True,
                    "imageId": 12 if tamper else 11,
                    "state": {"articleId": 3, "title": "文章", "version": "c" * 64,
                              "coverUrl": "https://images.example/cover.png", "fit": "contain"}}
        return httpx.Response(200, json={"code": 200, "data": data})

    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        prepared = preparation.prepare_article_cover.invoke(
            {"article_id": 3, "image_id": 11}, config=cfg())
        grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
        with closing(Ledger(str(tmp_path / "ledger.db"))) as ledger:
            for _ in range(2):
                result = transactions.execute(grant, cfg(), None, ledger=ledger)
                assert bool(result["receipts"]) is not tamper
    assert len(requests) == 3


@pytest.mark.parametrize("case_id", ["gallery_bulk_move_prepare", "gallery_bulk_favorite_prepare"])
def test_current_golden_requires_the_exact_batch_and_rejects_a_narrowed_card(backend, case_id):
    from eval import native_assertions, online_baseline

    case = next(row for row in online_baseline.load_suite() if row["id"] == case_id)
    assert online_baseline.fixture_images(case) == [72, 75]
    assert case["gold"]["max_model_calls"] == 4
    assert case["gold"]["max_tool_calls"] == {"prepare_gallery_images": 1}
    limits = {key: case["gold"][key] for key in ("max_model_calls", "max_tool_calls")}
    calls = [{"event": "llm_start"}] * 4 + [
        {"event": "tool_requested", "name": "prepare_gallery_images"}]
    assert not native_assertions.check(limits, {"native_events": calls})
    assert native_assertions.check(limits, {"native_events": [*calls, {"event": "llm_start"}]})
    assert native_assertions.check(limits, {"native_events": [*calls, calls[-1]]})
    assert case["gold"]["forbid_business_write"] and case["gold"]["require_zero_exec"]
    expected = case["gold"]["require_confirm_specs"][0]
    _, grant = approve(expected["args"]["intent"])
    assert native_assertions.contains(grant["specs"][0], expected)
    narrowed = copy.deepcopy(grant)
    narrowed["specs"][0]["args"]["intent"]["imageIds"] = [72]
    assert not native_assertions.contains(narrowed["specs"][0], expected)
    assert backend["effects"] == 0
