"""Gallery discovery stays bounded and writes use signed, versioned approvals."""

import base64
import io
import json
import secrets

import httpx
import pytest
from _own_support import config
from langchain_core.messages import ToolMessage
from PIL import Image

from agent import confirm, idem
from agent.domains.gallery import preparation, reads, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import memory_media
from tools import base


def admin():
    return config(principal=Principal(7, "admin", SOURCE_ASSERTION))


@pytest.fixture
def gallery(monkeypatch):
    row = dict(
        imageKey=11,
        imageUrl="https://images.example/cover.png",
        displayName="山景",
        description="山景" * 500,
        folderId=0,
        isFavorite=False,
        version="a" * 64,
    )
    state = dict(row=row, writes=0, requests=[])

    def handle(request):
        state["requests"].append(request)
        if request.method == "GET":
            if request.url.path.endswith("/images"):
                return httpx.Response(
                    200, json={"code": 200, "data": {"items": [state["row"]], "nextCursor": None}}
                )
            if "/articles/" in request.url.path:
                data = dict(articleId=3, title="文章", coverUrl=None, fit=None, version="b" * 64)
            else:
                data = state["row"]
            return httpx.Response(200, json={"code": 200, "data": data})
        state["writes"] += 1
        patch = json.loads(request.content)
        before = state["row"]["version"]
        assert patch.pop("version") == before
        if not state.get("ignore"):
            state["row"].update(patch, version="c" * 64)
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {"beforeVersion": before, "changed": True, "state": state["row"]},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
        yield state


def test_index_is_compact_and_has_no_pixels_or_storage_tokens(gallery):
    result = reads.get_gallery_index.invoke({}, config=admin())
    page = json.loads(str(result))
    assert len(page["items"][0]["description"]) == 160
    assert page["items"][0]["description_truncated"]
    assert "imageUrl" not in str(result) and "version" not in str(result)
    assert gallery["requests"][0].url.params["limit"] == "20"
    assert not gallery["writes"]


def test_preparation_and_replay_are_readonly_then_exactly_one_write(gallery, tmp_path):
    prepared = preparation.prepare_gallery_image_change.invoke(
        {"image_id": 11, "display_name": "新名称"}, config=admin()
    )
    grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
    assert gallery["writes"] == 0
    assert grant["specs"][0]["args"]["changes"] == {"displayName": "新名称"}
    ledger = idem.Ledger(str(tmp_path / "ledger.sqlite3"))
    for _ in range(2):
        result = transactions.execute(grant, admin(), None, ledger=ledger)
        assert len(result["receipts"]) == 1
    assert gallery["writes"] == 1


def test_mismatched_backend_outcome_never_produces_success(gallery, tmp_path):
    prepared = preparation.prepare_gallery_image_change.invoke(
        {"image_id": 11, "description": "新描述"}, config=admin()
    )
    grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
    gallery["ignore"] = True
    ledger = idem.Ledger(str(tmp_path / "ledger.sqlite3"))
    for _ in range(2):
        assert not transactions.execute(grant, admin(), None, ledger=ledger)["receipts"]
    assert gallery["writes"] == 1


def test_cover_preview_binds_exact_article_and_image_versions(gallery):
    prepared = preparation.prepare_article_cover.invoke(
        {"article_id": 3, "image_id": 11}, config=admin()
    )
    grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
    args = grant["specs"][0]["args"]
    assert args["version"] == "b" * 64 and args["image_version"] == "a" * 64
    assert args["fit"] == "contain" and "![候选封面]" in prepared.reply
    assert gallery["writes"] == 0


def test_untrusted_or_nonadmin_identity_never_reads_private_images(gallery):
    for role, source in [("user", SOURCE_ASSERTION), ("admin", "body")]:
        cfg = config(principal=Principal(7, role, source))
        assert (
            preparation.prepare_article_cover.invoke(
                {"article_id": 3, "image_id": 11}, config=cfg
            ).kind
            == "unavailable"
        )
        assert isinstance(reads.get_gallery_index.invoke({}, config=cfg), base.ToolResult)
    assert gallery["requests"] == []


def test_preview_is_bounded_and_reader_survives_history_compaction():
    source = Image.new("RGBA", (2400, 1200), (255, 0, 0, 50))
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")
    url = reads.thumbnail(buffer.getvalue())
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as preview:
        assert preview.size == (1280, 640) and preview.mode == "RGB"
    message = ToolMessage(
        content=[{"type": "image_url", "image_url": {"url": url}}],
        tool_call_id="gallery",
        artifact={"image_reader": {"tool": "get_gallery_image", "args": {"image_id": 11}}},
    )
    compact = memory_media.compact(message)
    assert "get_gallery_image" in str(compact.content) and "base64" not in str(compact.content)
    assert "get_conversation_image" not in str(compact.content)


def test_storage_allowlist_rejects_arbitrary_urls_before_download(monkeypatch):
    monkeypatch.setattr(base, "_admin_get", lambda *a: {"publicBase": "https://images.example"})
    for url in [
        "http://127.0.0.1/secret",
        "https://images.example.evil/test.png",
        "https://evil/images.example/a.png",
    ]:
        with pytest.raises(ValueError):
            reads._download({"imageUrl": url}, admin())
