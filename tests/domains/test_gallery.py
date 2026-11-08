"""Gallery discovery stays bounded and writes use signed, versioned approvals."""

import base64
import io
import json
import secrets
import time

import httpx
import pytest
from _own_support import config
from langchain_core.messages import ToolMessage
from PIL import Image

from agent import confirm
from agent.domains.gallery import preparation, reads, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import memory_media
from eval.fixtures.ledger import Ledger
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
        payload = json.loads(request.content)
        if request.url.path.endswith("/image-actions/prepare"):
            ids = payload.get("imageIds", [item["imageId"] for item in payload.get("items", [])])
            data = {"version": state["row"]["version"], "expiresAt": int(time.time()) + 900,
                    "intent": payload, "matchedIds": ids,
                    "preview": [{"imageId": 11, "displayName": state["row"]["displayName"],
                        "folderId": 0, "isFavorite": False}],
                    "previewTruncated": False, "destination": None}
            return httpx.Response(200, json={"code": 200, "data": data})
        state["writes"] += 1
        assert payload["version"] == state["row"]["version"]
        intent = payload["intent"]
        state["row"].update(intent["items"][0]["changes"], version="c" * 64)
        data = {"action": intent["action"], "beforeVersion": payload["version"], "changed": True,
                "matchedIds": [12] if state.get("ignore") else [11], "changedIds": [11],
                "cleanupPendingIds": []}
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
        yield state


def test_index_is_compact_and_has_no_pixels_or_storage_tokens(gallery):
    result = reads.get_gallery_index.invoke({}, config=admin())
    page = json.loads(str(result))
    assert result.data == page
    assert len(page["items"][0]["description"]) == 160
    assert page["items"][0]["description_truncated"]
    assert "imageUrl" not in str(result) and "version" not in str(result)
    assert gallery["requests"][0].url.params["limit"] == "20"
    assert not gallery["writes"]


def test_full_metadata_reads_one_authoritative_image_without_pixels_or_writes(gallery):
    result = reads.get_gallery_image_metadata.invoke({"image_id": 11}, config=admin())
    assert result.data == json.loads(str(result))
    assert result.data["description"] == gallery["row"]["description"]
    assert result.data["url"] == gallery["row"]["imageUrl"]
    assert result.data["display_markdown"] == "![山景](https://images.example/cover.png)"
    assert len(gallery["requests"]) == 1
    assert gallery["requests"][0].url.path.endswith("/gallery/images/11")
    assert gallery["writes"] == 0


def test_display_metadata_preserves_the_url_and_escapes_markdown_labels(gallery):
    gallery["row"].update(displayName="[猫]\n![伪造](bad)",
                          imageUrl="https://images.example/封面 (1).png")
    result = reads.get_gallery_image_metadata.invoke({"image_id": 11}, config=admin())
    assert result.data["display_markdown"] == (
        "![\\[猫\\] !\\[伪造\\](bad)](https://images.example/%E5%B0%81%E9%9D%A2%20%281%29.png)")
    assert len(gallery["requests"]) == 1 and not gallery["writes"]


def test_explicit_complete_cover_remains_supported_and_public_preview_matches_grant(gallery):
    from agent.products.blog.confirmation import public_card

    prepared = preparation.prepare_article_cover.invoke(
        {"article_id": 3, "image_id": 11, "fit": "contain"}, config=admin())
    card = public_card(prepared.execution)
    grant = confirm.verify(card["token"], 7, 42)
    assert card["cover_review"]["fit"] == grant["specs"][0]["args"]["fit"] == "contain"
    assert card["cover_review"]["url"] == grant["specs"][0]["args"]["image_url"]
    assert "specs" not in card and gallery["writes"] == 0


def test_full_metadata_does_not_disguise_another_image_as_the_requested_one(gallery):
    gallery["row"]["imageKey"] = 12
    result = reads.get_gallery_image_metadata.invoke({"image_id": 11}, config=admin())
    assert result.kind == "unavailable" and result.data is None


@pytest.mark.parametrize("rows", [
    [{"id": 2, "parentId": 0, "name": "a"}, {"id": 1, "parentId": 0, "name": "b"}],
    [{"id": 1, "parentId": 0, "name": "a"}] * 2,
    [{"id": 1, "parentId": 2, "name": "wrong parent"}],
    [{"id": True, "parentId": 0, "name": "invalid identifier"}],
    [None],
])
def test_invalid_folder_pages_never_look_like_an_empty_or_valid_collection(monkeypatch, rows):
    monkeypatch.setattr(base, "_admin_get", lambda *args: rows)
    result = reads.list_gallery_folders.invoke({}, config=admin())
    assert result.kind == "unavailable" and result.data is None


def test_folder_page_exposes_the_same_structured_continuation_as_its_display(monkeypatch):
    rows = [{"id": i, "parentId": 0, "name": str(i)} for i in range(1, 4)]
    monkeypatch.setattr(base, "_admin_get", lambda *args: rows)
    result = reads.list_gallery_folders.invoke({"limit": 2}, config=admin())
    assert result.data == json.loads(str(result)) == {"items": rows[:2], "next_cursor": 2}


def test_preparation_and_replay_are_readonly_then_exactly_one_write(gallery, tmp_path):
    prepared = preparation.prepare_gallery_images.invoke(
        {"intent": {"action": "metadata", "items": [{"imageId": 11,
            "changes": {"displayName": "新名称"}}]}}, config=admin()
    )
    grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
    assert gallery["writes"] == 0
    assert grant["specs"][0]["args"]["intent"]["items"][0]["changes"] == {"displayName": "新名称"}
    ledger = Ledger(str(tmp_path / "ledger.sqlite3"))
    for _ in range(2):
        result = transactions.execute(grant, admin(), None, ledger=ledger)
        assert len(result["receipts"]) == 1
    assert gallery["writes"] == 1


def test_mismatched_backend_outcome_never_produces_success(gallery, tmp_path):
    prepared = preparation.prepare_gallery_images.invoke(
        {"intent": {"action": "metadata", "items": [{"imageId": 11,
            "changes": {"description": "新描述"}}]}}, config=admin()
    )
    grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
    gallery["ignore"] = True
    ledger = Ledger(str(tmp_path / "ledger.sqlite3"))
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
    assert args["fit"] == "cover"
    assert prepared.execution["pending_confirm"]["cover_review"] == {
        "image_id": 11, "name": "山景", "url": "https://images.example/cover.png", "fit": "cover"}
    assert "![" not in prepared.reply
    assert gallery["writes"] == 0
    assert [(request.method, request.url.path) for request in gallery["requests"]] == [
        ("GET", "/api/protect/gallery/images/11"),
        ("GET", "/api/protect/gallery/articles/3/cover"),
    ]


def test_cover_preparation_supplies_verified_targets_to_narration_without_more_queries(gallery):
    from agent.runtime.approval_messages import prepared as approval_message

    card = preparation.prepare_article_cover.invoke(
        {"article_id": 3, "image_id": 11}, config=admin())
    message = approval_message({"name": "prepare_article_cover", "id": "cover"}, {
        "execution": card.execution, "reply": card.reply}, [])
    facts = json.loads(message.content)
    assert facts["status"] == "awaiting_confirmation" and facts["executed"] is False
    assert facts["preparation_facts"] == {
        "article_id": 3, "article_title": "文章", "image_id": 11, "image_name": "山景",
        "fit": "cover", "preview_layouts": ["article_card", "home_carousel", "article_banner"],
        "preview_delivery": "confirmation_card", "preview_before_approval": True,
        "preparation_inspects_pixels": False,
    }
    assert card.execution["pending_confirm"]["token"] not in message.content
    assert gallery["row"]["description"] not in message.content
    assert "version" not in message.content and len(gallery["requests"]) == 2
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
        assert reads.get_gallery_image_metadata.invoke(
            {"image_id": 11}, config=cfg).kind == "unavailable"
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


def test_background_metadata_reads_do_not_require_a_writable_conversation(gallery):
    cfg = {
        "configurable": {
            "principal": Principal(7, "admin", SOURCE_ASSERTION),
            "user_id": 7,
            "thread_id": "schedule:isolated-checkpoint",
        }
    }
    result = reads.get_gallery_index.invoke({}, config=cfg)
    assert result.kind == "ok" and gallery["writes"] == 0
    count = len(gallery["requests"])
    assert (
        preparation.prepare_gallery_images.invoke(
            {"intent": {"action": "metadata", "items": [{"imageId": 11,
                "changes": {"displayName": "new name"}}]}}, config=cfg
        ).kind
        == "unavailable"
    )
    assert len(gallery["requests"]) == count
    cfg["configurable"]["user_id"] = 8
    assert reads.get_gallery_index.invoke({}, config=cfg).kind == "unavailable"
    assert len(gallery["requests"]) == count


def test_metadata_includes_authoritative_folder_labels_without_extra_reads(gallery):
    gallery["row"].update(folderId=3, folderName="素材", folderParentId=2)
    result = reads.get_gallery_image_metadata.invoke({"image_id": 11}, config=admin())
    assert result.data["folder_id"] == 3
    assert result.data["folder_name"] == "素材"
    assert result.data["folder_parent_id"] == 2
    assert len(gallery["requests"]) == 1 and not gallery["writes"]


def test_index_includes_folder_name_without_expanding_the_folder_tree(gallery):
    gallery["row"].update(folderId=3, folderName="素材", folderParentId=2)
    result = reads.get_gallery_index.invoke({}, config=admin())
    assert result.data["items"][0]["folder_name"] == "素材"
    assert len(gallery["requests"]) == 1 and not gallery["writes"]


def test_an_explicit_unchanged_favorite_preview_is_read_only_and_can_be_reviewed(gallery):
    assert gallery["row"]["isFavorite"] is False
    prepared = preparation.prepare_gallery_images.invoke(
        {"intent": {"action": "favorite", "imageIds": [11], "isFavorite": False}},
        config=admin(),
    )
    from agent.products.blog.confirmation import public_card

    grant = confirm.verify(public_card(prepared.execution)["token"], 7, 42)
    assert grant["specs"][0]["args"]["intent"] == {
        "action": "favorite", "imageIds": [11], "isFavorite": False,
    }
    assert gallery["writes"] == 0
    assert len(gallery["requests"]) == 1
    assert gallery["requests"][0].url.path.endswith("/image-actions/prepare")
