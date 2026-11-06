"""Paid generation and gallery acceptance require separate human approvals."""

import copy
import json
import secrets

import httpx
import pytest
from _own_support import config

from agent import confirm
from agent.domains.image_generation import tools, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.pending import PendingOperation
from agent.runtime import writes
from eval.fixtures.ledger import Ledger
from tools import base

ID = "6b9e2bf1-56af-428c-8177-e3ad0641e036"


def admin():
    return config(principal=Principal(7, "admin", SOURCE_ASSERTION))


@pytest.fixture
def service(monkeypatch):
    state = {"paid_calls": 0, "accepts": 0, "requests": [], "job": None}

    def handle(request):
        state["requests"].append(request)
        path = request.url.path
        if path.endswith("/options"):
            data = {"configured": True, "defaultModel": "fixture", "models": ["fixture"]}
        elif path.endswith("/jobs") and request.method == "POST":
            payload = json.loads(request.content)
            refs = [
                {
                    "imageId": i,
                    "name": "参考图",
                    "digest": "c" * 64,
                    "previewUrl": f"/api/public/image-generation/{ID}/preview?token=fixture-{i}",
                }
                for i in payload.pop("referenceIds")
            ]
            state["job"] = {
                "id": ID,
                "request": {**payload, "references": refs},
                "requestHash": "a" * 64,
                "status": "pending",
                "outputHash": None,
                "previewUrl": None,
                "imageId": None,
            }
            data = state["job"]
        elif path.endswith("/approve"):
            assert json.loads(request.content) == {"version": "a" * 64}
            state["paid_calls"] += 1
            state["job"]["status"] = "generating"
            data = copy.deepcopy(state["job"])
            if state.get("mismatch"):
                data["requestHash"] = "d" * 64
        elif path.endswith("/accept"):
            assert json.loads(request.content) == {"version": "b" * 64}
            state["accepts"] += 1
            state["job"].update(status="accepted", imageId=31)
            data = state["job"]
        else:
            data = state["job"]
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
        yield state


def prepare(prompt="为文章画封面", **extra):
    return tools.prepare_image_generation.invoke({"prompt": prompt, **extra}, config=admin())


def grant(card):
    return confirm.verify(card.execution["pending_confirm"]["token"], 7, 42)


def test_full_prompt_and_references_visible_before_any_paid_call(service, tmp_path):
    prompt = "完整提示词\n```原样保留```\n" + "主题细节" * 1800
    card = prepare(prompt, reference_ids=[11, 12])
    assert prompt in card.reply and "参考图 1" in card.reply and "参考图 2" in card.reply
    assert service["paid_calls"] == 0 and service["accepts"] == 0
    signed = grant(card)
    assert signed["specs"][0]["args"] == {"job_id": ID, "version": "a" * 64}
    ledger = Ledger(str(tmp_path / "generation.sqlite3"))
    for _ in range(2):
        assert len(transactions.execute(signed, admin(), None, ledger=ledger)["receipts"]) == 1
    assert service["paid_calls"] == 1 and service["accepts"] == 0


def test_actual_pixels_need_a_distinct_result_approval(service, tmp_path):
    prepare()
    service["job"].update(
        status="review",
        outputHash="b" * 64,
        previewUrl=f"/api/public/image-generation/{ID}/preview?token=result",
    )
    card = tools.prepare_generated_image_acceptance.invoke({"job_id": ID}, config=admin())
    assert "![生成结果]" in card.reply and not service["accepts"]
    signed = grant(card)
    assert signed["workflow"] == "image_acceptance"
    ledger = Ledger(str(tmp_path / "acceptance.sqlite3"))
    for _ in range(2):
        outcome = transactions.execute(signed, admin(), None, ledger=ledger)
        assert len(outcome["receipts"]) == 1
    assert service["accepts"] == 1 and service["paid_calls"] == 0


def test_cancelled_approval_cannot_start_paid_call(service, tmp_path, monkeypatch):
    card = prepare()
    signed = grant(card)
    ledger = Ledger(str(tmp_path / "cancel.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda _: ledger)
    PendingOperation(signed, card.execution["pending_action"]).cancel(admin(), None)
    assert not transactions.execute(signed, admin(), None, ledger=ledger)["receipts"]
    assert service["paid_calls"] == 0


def test_inconsistent_provider_operation_is_unknown_not_success_or_retried(service, tmp_path):
    signed = grant(prepare())
    service["mismatch"] = True
    ledger = Ledger(str(tmp_path / "unknown.sqlite3"))
    for _ in range(2):
        assert not transactions.execute(signed, admin(), None, ledger=ledger)["receipts"]
    assert service["paid_calls"] == 1


def test_nonadmin_cannot_prepare_or_read_private_jobs(service):
    cfg = config(principal=Principal(9, "user", SOURCE_ASSERTION))
    assert (
        tools.prepare_image_generation.invoke({"prompt": "画封面"}, config=cfg).kind
        == "unavailable"
    )
    assert tools.get_image_generation.invoke({"job_id": ID}, config=cfg).kind == "unavailable"
    assert service["requests"] == []


def test_no_result_approval_before_generation_finishes(service):
    prepare()
    result = tools.prepare_generated_image_acceptance.invoke({"job_id": ID}, config=admin())
    assert result.kind == "unavailable" and not service["accepts"]


def test_background_generation_status_read_does_not_gain_paid_execution(service):
    service["job"] = {"id": ID, "status": "review", "imageId": None, "error": None}
    cfg = {
        "configurable": {
            "principal": Principal(7, "admin", SOURCE_ASSERTION),
            "user_id": 7,
            "thread_id": "schedule:isolated-checkpoint",
        }
    }
    result = tools.get_image_generation.invoke({"job_id": ID}, config=cfg)
    assert result.kind == "ok"
    assert service["paid_calls"] == 0 and service["accepts"] == 0
    count = len(service["requests"])
    assert (
        tools.prepare_generated_image_acceptance.invoke({"job_id": ID}, config=cfg).kind
        == "unavailable"
    )
    assert len(service["requests"]) == count
    cfg["configurable"]["user_id"] = 8
    assert tools.get_image_generation.invoke({"job_id": ID}, config=cfg).kind == "unavailable"
    assert len(service["requests"]) == count
