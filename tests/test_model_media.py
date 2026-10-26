"""Model fingerprints preserve wire order and never retain bearer media references."""

import base64
import copy
import hashlib
import json
import uuid

import httpx
import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage

from models import media
from models.llm import get_llm


def inline(value):
    return "data:image/png;base64," + base64.b64encode(value).decode()


def image(value):
    return {"type": "image_url", "image_url": {"url": value}}


def test_fingerprints_are_ordered_bounded_and_do_not_mutate_messages(monkeypatch):
    secret_url = "https://example.invalid/image.png?signature=" + uuid.uuid4().hex
    values = [inline(b"first fixture"), secret_url, inline(b"second fixture")]
    messages = [HumanMessage(content=[{"type": "text", "text": "Compare"},
                                     *(image(value) for value in values)])]
    original = copy.deepcopy(messages)
    result = media.image_fingerprints(messages)
    assert messages == original and result["count"] == 3
    assert [row["block_index"] for row in result["images"]] == [1, 2, 3]
    assert [row["sha256"] for row in result["images"]] == [
        hashlib.sha256(raw).hexdigest() for raw in
        (b"first fixture", secret_url.encode(), b"second fixture")]
    assert [row["digest_scope"] for row in result["images"]] == [
        "content", "reference", "content"]
    serialized = json.dumps(result)
    assert all(value not in serialized for value in values)
    monkeypatch.setattr(media, "MAX_IMAGES", 1)
    capped = media.image_fingerprints(messages)
    assert capped["count"] == 3 and len(capped["images"]) == 1 and capped["omitted"] == 2


def test_invalid_and_oversized_media_remain_visible_without_payloads(monkeypatch):
    messages = [HumanMessage(content=[image("data:image/png;base64,%%%"),
        image("data:image/png,not-base64"), image(None), image(inline(b"large"))])]
    monkeypatch.setattr(media, "MAX_HASH_CHARS", 60)
    result = media.image_fingerprints(messages)
    assert result["count"] == 4 and all("sha256" not in row for row in result["images"])
    assert [row.get("digest_omitted") for row in result["images"]] == [
        "invalid_encoding", "unsupported_encoding", None, "size_limit"]


def test_plain_text_does_not_create_media_events(monkeypatch):
    events = []
    monkeypatch.setattr(media, "record", lambda *args, **kwargs: events.append((args, kwargs)))
    media.ModelMediaHandler().on_chat_model_start({}, [[HumanMessage(content="hello")]],
                                                run_id=uuid.uuid4())
    assert not events


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("standard_blocks", [False, True])
def test_factory_callback_matches_actual_serialized_images_without_rewriting(
        monkeypatch, streaming, standard_blocks):
    events, wire, caller_events = [], [], []
    monkeypatch.setattr(media, "record", lambda *args, **kwargs: events.append((args, kwargs)))
    values = [inline(b"red fixture"), inline(b"blue fixture")]
    blocks = ([{"type": "image", "base64": value.partition(",")[2], "mime_type": "image/png"}
               for value in values] if standard_blocks else [image(value) for value in values])
    messages = [HumanMessage(content=[{"type": "text", "text": "Compare"}, *blocks])]
    original = copy.deepcopy(messages)

    def respond(request):
        wire.append(json.loads(request.content))
        if streaming:
            chunk = {"id": "fixture", "object": "chat.completion.chunk", "model": "fixture",
                     "choices": [{"index": 0, "finish_reason": "stop", "delta": {
                         "role": "assistant", "content": "fixture answer"}}]}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion",
            "model": "fixture", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "fixture answer"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}})

    class CallerCallback(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, **kwargs):
            caller_events.append(kwargs["run_id"])

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        llm = get_llm(model="fixture", base_url="https://example.invalid/v1",
                      api_key=uuid.uuid4().hex,
                      http_client=client, enable_thinking=False, streaming=streaming,
                      callbacks=[CallerCallback()])
        llm.invoke(messages)
    assert messages == original and len(wire) == len(events) == len(caller_events) == 1
    sent = [b["image_url"]["url"] for m in wire[0]["messages"]
            for b in m["content"] if b["type"] == "image_url"]
    assert sent == values
    args, observed = events[0]
    assert args == ("model", "media_input") and observed["stage"] == "before_sdk"
    assert [item["sha256"] for item in observed["images"]] == [
        hashlib.sha256(base64.b64decode(value.partition(",")[2])).hexdigest() for value in sent]
