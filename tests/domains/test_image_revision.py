"""Human draft edits use checkpoints, revoke old grants, and spend no model/API calls."""

import copy
import json
import secrets
from contextlib import closing

import httpx
import pytest
from _framework_ledger import Ledger
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_confirmation_continuation import Model, call

from agent import confirm
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog import image_revision as revisions
from agent.products.blog.application import BlogApplication
from agent.runtime import writes
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from tools import base

ID = "6b9e2bf1-56af-428c-8177-e3ad0641e036"
NEW = "7b9e2bf1-56af-428c-8177-e3ad0641e036"


@pytest.fixture
def prepared(monkeypatch, tmp_path):
    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    jobs, paid, requests = {}, [], []
    revision_count = []
    model = Model(
        AIMessage(content="", tool_calls=[call("load_admin_image_generation_info_tools", "load")]),
        AIMessage(
            content="", tool_calls=[call("prepare_image_generation", "prepare", prompt="original")]
        ),
    )
    model.approval_reply = AIMessage(content="请确认。")
    runtime = AgentRuntime(
        CheckpointConversationStore(InMemorySaver()),
        application=BlogApplication(),
        model_factory=lambda **_: model,
    )

    def handle(request):
        requests.append(request.url.path)
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path.endswith("/options"):
            result = {
                "configured": True,
                "models": ["fixture", "alternate"],
                "defaultModel": "fixture",
            }
        elif path.endswith("/jobs"):
            jobs[ID] = {
                "id": ID,
                "conversationId": 42,
                "requestHash": "a" * 64,
                "status": "pending",
                "imageRemoved": False,
                "request": {**body, "provider": "https://images.test/v1", "references": []},
            }
            result = jobs[ID]
        elif path.endswith("/revision"):
            assert body["conversationId"] == 42
            old_id = path.split("/")[-2]
            revision_count.append(old_id)
            new_id = NEW if len(revision_count) == 1 else "8b9e2bf1-56af-428c-8177-e3ad0641e036"
            jobs[new_id] = copy.deepcopy(jobs[old_id])
            jobs[new_id].update(
                id=new_id, requestHash=("b" if len(revision_count) == 1 else "c") * 64
            )
            jobs[new_id]["request"].update(
                {key: body[key] for key in ("prompt", "model", "size", "quality")}
            )
            jobs[old_id]["status"] = "rejected"
            result = jobs[new_id]
        elif path.endswith("/approve"):
            job_id = path.split("/")[-2]
            assert body == {"version": jobs[job_id]["requestHash"]}
            if jobs[job_id]["status"] == "pending":
                paid.append(job_id)
                jobs[job_id]["status"] = "generating"
            result = jobs[job_id]
        else:
            result = jobs[path.rsplit("/", 1)[-1]]
        return httpx.Response(200, json={"code": 200, "data": result})

    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    with (
        closing(Ledger(str(tmp_path / "ledger.sqlite"))) as ledger,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(writes, "ledger_for", lambda _: ledger)
        result = list(
            runtime.stream_events(
                {"messages": [HumanMessage(content="生成图片")], "task_id": "image-edit"}, cfg
            )
        )[-1]
        grant = confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)
        assert grant is not None and not paid
        yield runtime, cfg, grant, model, jobs, paid, requests


def draft(**overrides):
    return revisions.ImageDraft(
        prompt="human exact edited prompt",
        model="alternate",
        size="1024x1536",
        quality="low",
        **overrides,
    )


def test_revision_spends_zero_model_calls_and_resumes_actual_new_grant(prepared):
    runtime, cfg, old, model, jobs, paid, requests = prepared
    before = len(model.prompts)
    updated = revisions.revise(runtime, old, draft(), cfg)
    card = updated["confirmation"]
    assert card["image_review"]["prompt"] == draft().prompt
    assert card["image_review"]["editor"]["model"] == "alternate"
    new = confirm.verify(card["token"], 7, 42)
    assert new["jti"] != old["jti"] and new["specs"][0]["args"]["job_id"] == NEW
    assert jobs[ID]["status"] == "rejected" and not paid and len(model.prompts) == before
    repeated = revisions.revise(runtime, old, draft(), cfg)
    assert repeated["confirmation"]["token"] == card["token"]
    assert sum(path.endswith("/revision") for path in requests) == 1
    old_result = list(runtime.stream_events({"confirm_grant": old}, cfg))[-1]
    assert not old_result.execution.get("receipts") and not paid
    model.responses = iter([AIMessage(content="已开始按修改后的设置生成图片。")])
    result = list(runtime.stream_events({"confirm_grant": new}, cfg))[-1]
    assert paid == [NEW] and not runtime.store.load(cfg).pending_action
    assert "开始" in result.reply
    assert len(model.prompts) == before + 1


@pytest.mark.parametrize("change", ["role", "owner", "conversation", "draft"])
def test_revision_rejects_changed_scope_or_stale_draft(prepared, change):
    runtime, cfg, old, model, jobs, paid, requests = prepared
    if change == "draft":
        revisions.revise(runtime, old, draft(), cfg)
        value = revisions.ImageDraft.model_validate({**draft().model_dump(), "prompt": "different"})
    else:
        value = draft()
        cfg = copy.deepcopy(cfg)
        if change == "role":
            cfg["configurable"]["principal"] = Principal(7, "user", SOURCE_ASSERTION)
        if change == "owner":
            cfg["configurable"]["principal"] = Principal(8, "admin", SOURCE_ASSERTION)
        if change == "conversation":
            cfg["configurable"]["conversation_id"] = 43
    count = len(requests)
    with pytest.raises((ValueError, PermissionError)):
        revisions.revise(runtime, old, value, cfg)
    assert len(requests) == count and not paid


def test_current_image_handle_is_visible_without_reloading_pixels():
    from agent.runtime.memory_media import hydrate, image_id, stage

    url = "data:image/png;base64,aGVsbG8="
    original = HumanMessage(
        content=[
            {"type": "text", "text": "用它作为参考图"},
            {"type": "image_url", "image_url": {"url": url}},
        ]
    )
    transient = {}
    staged = stage([original], transient)
    visible = hydrate(staged, transient)[0]
    assert image_id(url) in str(visible.content)
    assert url not in str(staged[0].content)
    assert original.content[0]["text"] == "用它作为参考图"


def test_multiple_revisions_resume_the_original_native_tool_exchange(prepared):
    runtime, cfg, old, model, jobs, paid, requests = prepared
    first = revisions.revise(runtime, old, draft(), cfg)["confirmation"]
    first_grant = confirm.verify(first["token"], 7, 42)
    latest_draft = revisions.ImageDraft.model_validate(
        {**draft().model_dump(), "prompt": "final human prompt"}
    )
    second = revisions.revise(runtime, first_grant, latest_draft, cfg)["confirmation"]
    latest = confirm.verify(second["token"], 7, 42)
    assert latest["jti"] != first_grant["jti"] and not paid
    before = len(model.prompts)
    model.responses = iter([AIMessage(content="已经按最终确认的内容开始生成。")])
    list(runtime.stream_events({"confirm_grant": latest}, cfg))
    assert paid == [latest["specs"][0]["args"]["job_id"]]
    assert len(model.prompts) == before + 1
    assert "final human prompt" in str(model.prompts[-1])


@pytest.mark.parametrize("retry_update", [True, False])
def test_saved_replacement_recovers_when_the_http_reply_or_interrupt_setup_is_lost(
    prepared, monkeypatch, retry_update
):
    runtime, cfg, old, model, jobs, paid, requests = prepared
    from agent.runtime import approval_revision

    native_graph = approval_revision.approval.graph

    class Unavailable:
        def invoke(self, *_):
            raise RuntimeError("checkpoint interrupt setup unavailable")

    monkeypatch.setattr(approval_revision.approval, "graph", lambda *_: Unavailable())
    with pytest.raises(RuntimeError):
        revisions.revise(runtime, old, draft(), cfg)
    assert runtime.store.load(cfg).pending_action["jti"] != old["jti"] and not paid
    monkeypatch.setattr(approval_revision.approval, "graph", native_graph)
    restored = (
        revisions.revise(runtime, old, draft(), cfg)["confirmation"]
        if retry_update
        else runtime.store.load(cfg).pending_confirm
    )
    new = confirm.verify(restored["token"], 7, 42)
    model.responses = iter([AIMessage(content="修改后的生图请求已开始执行。")])
    list(runtime.stream_events({"confirm_grant": new}, cfg))
    assert paid == [NEW]


def test_replacement_saved_before_checkpoint_failure_keeps_old_approval_unpaid(
    prepared, monkeypatch
):
    from agent.products.blog.pending import PendingOperation

    runtime, cfg, old, model, jobs, paid, requests = prepared
    original = PendingOperation.feedback

    def fail_before_revocation(*_args, **_kwargs):
        raise RuntimeError("interrupted before checkpoint update")

    monkeypatch.setattr(PendingOperation, "feedback", fail_before_revocation)
    with pytest.raises(RuntimeError):
        revisions.revise(runtime, old, draft(), cfg)
    assert jobs[ID]["status"] == "rejected" and jobs[NEW]["status"] == "pending"
    assert runtime.store.load(cfg).pending_action["jti"] == old["jti"]
    monkeypatch.setattr(PendingOperation, "feedback", original)
    model.responses = iter([AIMessage(content="原生成请求尚未执行，修改后的请求需要重新确认。")])
    result = list(runtime.stream_events({"confirm_grant": old}, cfg))[-1]
    assert result.execution["operation_outcome"]["status"] == "unverified"
    assert not paid and not runtime.store.load(cfg).pending_action
    count = len(model.prompts)
    list(runtime.stream_events({"confirm_grant": old}, cfg))
    assert not paid and len(model.prompts) == count
