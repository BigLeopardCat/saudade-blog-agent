"""Real SDK and checkpoints carry one paid start through result and storage approval."""

import json
import secrets
import threading
import time
from contextlib import closing

import httpx
import pytest
from _framework_ledger import Ledger
from _own_support import config
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import confirm
from agent.domains.image_generation import transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.runtime import writes
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.control import AgentCancelled, RequestControl, request_scope
from agent.runtime.service import AgentRuntime
from models.llm import get_llm
from models.transport import ModelIO
from tools import base

ID = "6b9e2bf1-56af-428c-8177-e3ad0641e036"


class Body(httpx.SyncByteStream):
    def __init__(self, values, stop=None):
        self.values, self.stop = values, stop
        self.closed = False

    def __iter__(self):
        for index, value in enumerate(self.values):
            if index == 1 and self.stop is not None:
                self.stop.set()
            yield ("event: image_job\ndata: " + json.dumps(value) + "\n\n").encode()

    def close(self):
        self.closed = True


@pytest.mark.parametrize("interrupted", [False, True])
def test_sdk_two_approvals_wait_once_each_and_never_send_preview_tokens_to_model(
        monkeypatch, tmp_path, interrupted):
    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    calls, model_requests, streams = [], [], []
    stopped = threading.Event()
    job = {"id": ID, "requestHash": "a" * 64, "status": "pending",
           "request": {"provider": "https://images.test/v1", "model": "fixture",
                       "prompt": "approved exact prompt", "size": "1536x1024",
                       "quality": "high", "references": []}}

    def host(request):
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/options"):
            data = {"configured": True, "defaultModel": "fixture", "models": ["fixture"]}
        elif path.endswith("/jobs") and request.method == "POST":
            assert json.loads(request.content)["prompt"] == job["request"]["prompt"]
            data = job
        elif path.endswith("/approve"):
            job["status"] = "generating"
            data = job
        elif path.endswith("/accept"):
            job["status"] = "saving"
            data = job
        elif path.endswith("/events"):
            first = dict(job)
            if job["status"] == "generating":
                job.update(status="review", outputHash="b" * 64,
                           previewUrl=f"/api/public/image-generation/{ID}/preview?token=private")
            elif job["status"] == "saving":
                job.update(status="accepted", imageId=31)
            else:
                assert job["status"] == "review"
            body = Body([first, dict(job)], stopped if interrupted and not streams else None)
            streams.append(body)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=body)
        else:
            assert path.endswith(ID) and request.method == "GET"
            data = job
        return httpx.Response(200, json={"code": 200, "data": data})

    actions = iter([
        ("load_admin_image_generation_info_tools", {}),
        ("prepare_image_generation", {"prompt": job["request"]["prompt"]}),
        ("get_image_generation", {"job_id": ID, "wait_seconds": 240}),
        ("prepare_generated_image_acceptance", {"job_id": ID}),
        ("get_image_generation", {"job_id": ID, "wait_seconds": 240}),
        None,
    ])

    async def provider(request):
        payload = json.loads(request.content)
        model_requests.append(payload)
        if payload.get("tool_choice") == "none":
            delta = {"content": "请核对图片与确认卡。"}
        else:
            action = next(actions)
            delta = ({"tool_calls": [{"index": 0, "id": "call-" + str(len(model_requests)),
                "type": "function", "function": {"name": action[0],
                "arguments": json.dumps(action[1])}}]} if action else
                {"content": "图片已保留到图库，编号31。"})
        wire = {"id": "fixture", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
            content=("data: " + json.dumps(wire) + "\n\ndata: [DONE]\n\n").encode())

    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    monkeypatch.setattr("config.settings.llm_provider", "opencode")
    with closing(Ledger(str(tmp_path / "execution.sqlite3"))) as ledger, \
            httpx.Client(transport=httpx.MockTransport(host)) as client, \
            ModelIO(transport=httpx.MockTransport(provider)) as io:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(writes, "ledger_for", lambda _: ledger)
        saver = InMemorySaver()

        def run(inputs):
            runtime = AgentRuntime(CheckpointConversationStore(saver),
                application=BlogApplication(), model_io=io,
                model_factory=lambda **kwargs: get_llm(api_key="synthetic-only",
                    base_url="https://model.test/v1", model="deepseek-v4.1-flash", **kwargs))
            with request_scope(RequestControl(stopped, time.monotonic() + 300)):
                return list(runtime.stream_events(inputs, cfg))[-1]

        first = run({"messages": [HumanMessage(content="根据完整提示词生成图片，完成后给我看。")],
                     "task_id": "image-followthrough"})
        assert not any(path.endswith("/approve") for _, path in calls)
        first_grant = confirm.verify(first.execution["pending_confirm"]["token"], 7, 42)
        if interrupted:
            with pytest.raises(AgentCancelled):
                run({"confirm_grant": first_grant})
            assert streams[0].closed
            stopped.clear()
            second = run({"resume_task": "image-followthrough"})
        else:
            second = run({"confirm_grant": first_grant})
        card = second.execution["pending_confirm"]
        assert card["image_review"]["result_url"].endswith("token=private")
        assert job["status"] == "review" and not any(path.endswith("/accept") for _, path in calls)
        second_grant = confirm.verify(card["token"], 7, 42)
        final = run({"confirm_grant": second_grant})
        assert final.reply == "图片已保留到图库，编号31。"
        assert job["status"] == "accepted" and all(body.closed for body in streams)
        assert sum(path.endswith("/approve") for _, path in calls) == 1
        assert sum(path.endswith("/accept") for _, path in calls) == 1
        assert sum(path.endswith("/events") for _, path in calls) == 2 + int(interrupted)
        before = len(calls)
        assert transactions.execute(first_grant, cfg, None, ledger=ledger)["receipts"]
        assert transactions.execute(second_grant, cfg, None, ledger=ledger)["receipts"]
        assert len(calls) == before
    wire = json.dumps(model_requests, ensure_ascii=False)
    assert "token=private" not in wire and "result_url" not in wire
    assert "正在写入图库，尚未核实入库完成" in wire
    assert "hasPreview" in wire and "accepted" in wire
