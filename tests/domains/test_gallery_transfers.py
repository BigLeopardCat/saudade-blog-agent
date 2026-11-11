"""Transfers share confirmation, authorization and the durable execution ledger."""
import json
import secrets
import time
from contextlib import closing

import httpx
import pytest
from _own_support import config

from agent import confirm
from agent.domains.gallery import reads, transactions, transfers
from agent.principal import SOURCE_ASSERTION, Principal
from eval.fixtures.ledger import Ledger
from tools import base


def admin():
    return config(principal=Principal(7, "admin", SOURCE_ASSERTION))


def image(identifier=72):
    media_id = f"6b9e2bf1-56af-428c-8177-{identifier:012d}"
    return {"imageKey": identifier, "imageUrl": f"/api/public/media/{media_id}",
            "displayName": "封面", "description": "山景", "folderId": 0,
            "storage": "local", "storagePath": "cover.png", "isFavorite": False,
            "version": "b" * 64}


@pytest.mark.parametrize("mode", ["move", "copy"])
def test_approval_queues_one_job_without_claiming_completion_and_replay_never_resubmits(
        monkeypatch, tmp_path, mode):
    from agent.runtime import tool_evidence
    observations = []
    monkeypatch.setattr(tool_evidence, "record", lambda node, event, **kw: observations.append(kw))
    requests = []
    intent = {"mode": mode, "imageIds": [72], "folderId": 0, "storage": "local"}

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/prepare"):
            data = {"version": "a" * 64, "expiresAt": int(time.time()) + 900,
                    "intent": intent, "destinationName": "未分类", "images": [image()]}
        else:
            assert json.loads(request.content)["intent"] == intent
            data = {"id": "a" * 64, "status": "queued", "result": None, "error": None}
        return httpx.Response(200, json={"code": 200, "data": data})

    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        prepared = transfers.prepare_gallery_transfer.invoke({"intent": intent}, config=admin())
        assert len(requests) == 1
        assert all(row["complete"] for row in observations)
        assert observations[0]["facts"] == {"folderId": 0, "name": "未分类"}
        assert observations[1]["facts"]["images"] == [{
            "imageKey": 72, "displayName": "封面", "folderId": 0, "storage": "local"}]
        grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
        assert grant and grant["workflow"] == "gallery_transfer"
        assert confirm.verify(prepared.execution["pending_confirm"]["token"], 8, 42) is None
        with closing(Ledger(str(tmp_path / "ledger.db"))) as ledger:
            for _ in range(2):
                result = transactions.execute(grant, admin(), None, ledger=ledger)
                assert not result["blocked"] and len(result["receipts"]) == 1
                assert "尚未完成" in result["reply"] and "任务已完成" not in result["reply"]
        assert len(requests) == 2


def test_preview_rejects_scope_changes_and_forbidden_roles_do_not_call_backend(monkeypatch):
    intent = {"mode": "copy", "imageIds": [72], "folderId": 0}
    forged = {"version": "a" * 64, "expiresAt": int(time.time()) + 900,
              "intent": intent, "destinationName": "未分类", "images": [image(75)]}
    with pytest.raises(ValueError):
        transfers.prepared(forged, intent)
    monkeypatch.setattr(base, "_admin_read_post", lambda *_: pytest.fail("unauthorized read"))
    for role in ["user", "zako"]:
        cfg = config(principal=Principal(7, role, SOURCE_ASSERTION))
        result = transfers.prepare_gallery_transfer.invoke({"intent": intent}, config=cfg)
        assert result.kind == "unavailable"


def test_transfer_card_keeps_each_name_with_its_own_identifier(monkeypatch):
    from agent.contracts.events import confirmation_text
    from agent.products.blog.confirmation import public_card

    intent = {"mode": "copy", "imageIds": [72, 75], "folderId": 0}
    value = {"version": "a" * 64, "expiresAt": int(time.time()) + 900,
             "intent": intent, "destinationName": "未分类",
             "images": [{**image(72), "displayName": None}, image(75)]}
    monkeypatch.setattr(base, "_admin_read_post", lambda *_: value)
    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    prepared = transfers.prepare_gallery_transfer.invoke({"intent": intent}, config=admin())
    question = confirmation_text(public_card(prepared.execution))
    assert "图片 #72、图片 #75（封面）" in question
    assert "完整图片编号：72、75" in question
    grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
    assert grant["specs"][0]["args"]["intent"]["imageIds"] == [72, 75]


def test_completion_requires_exact_source_scope_and_independent_copy_ids():
    args = {"version": "a" * 64, "intent": {"mode": "copy", "imageIds": [72]}}
    value = {"id": "a" * 64, "status": "complete", "error": None,
             "result": {"mode": "copy", "sourceIds": [72], "imageIds": [75]}}
    assert transfers.outcome(value, args)["status"] == "complete"
    for key, replacement in [("sourceIds", [71]), ("imageIds", [72])]:
        changed = {**value, "result": {**value["result"], key: replacement}}
        with pytest.raises(ValueError):
            transfers.outcome(changed, args)
    with pytest.raises(ValueError):
        transfers.outcome({**value, "status": "queued"}, args)


def test_canonical_local_media_download_stays_on_the_configured_host(monkeypatch):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, content=b"image bytes")

    monkeypatch.setattr(base, "_admin_get", lambda *_: pytest.fail("R2 configuration is optional"))
    monkeypatch.setattr(base, "host_credential", lambda _: "fixture")
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        assert reads._download(image(), admin()) == b"image bytes"
    assert len(seen) == 1 and seen[0].url.path == "/api/protect/gallery/images/72/content"
    assert seen[0].url.host == httpx.URL(base.ADMIN_BASE).host
