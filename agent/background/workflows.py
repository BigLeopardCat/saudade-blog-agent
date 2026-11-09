"""Durable subscription stream around the existing authenticated host chat pipeline."""

import asyncio
import json

import httpx
from dbos import DBOS, WorkflowSerializationFormat
from httpx_sse import aconnect_sse

from agent.background.service import STREAM, WORKFLOW
from config.settings import settings

_VISIBLE = {"text", "text_reset", "progress", "confirmation", "command", "task_input",
            "done", "error"}


async def publish(event, data):
    await DBOS.write_stream_async(STREAM, {"event": event, "data": data},
        serialization_type=WorkflowSerializationFormat.PORTABLE)


@DBOS.step(retries_allowed=False, preemptible=True)
async def perform(payload):
    # A killed transport may have performed business effects. Recovery reports the
    # interruption instead of replaying the request or consuming an approval twice.
    if await DBOS.get_event_async(DBOS.workflow_id, "attempted", timeout_seconds=0):
        return outcome("interrupted", "error", "后台执行曾被中断，请核对已有结果后继续原任务。")
    await DBOS.set_event_async("attempted", True)
    try:
        async with asyncio.timeout(settings.stream_total_timeout + 30):
            async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(135, connect=5),
                                         follow_redirects=False) as client:
                async with aconnect_sse(client, "POST",
                        settings.agent_admin_base.rstrip("/") + "/api/chat/runs/execute",
                        headers={"X-Chat-Run-Ticket": payload["ticket"],
                                 "Content-Type": "application/json", "X-Agent-Stream-Version": "2"},
                        content=payload["body"]) as source:
                    source.response.raise_for_status()
                    async for event in source.aiter_sse():
                        if event.event not in _VISIBLE:
                            raise ValueError("unexpected host chat event")
                        data = json.loads(event.data)
                        if event.event in {"done", "error"}:
                            return outcome("completed" if event.event == "done" else "failed",
                                           event.event, data)
                        await publish(event.event, data)
    except (httpx.HTTPError, ValueError, TimeoutError):
        pass
    return outcome("interrupted", "error", "后台任务连接中断，结果可能不完整；请核对后继续原任务。")


def outcome(status, event, data):
    return {"status": status, "terminal": {"event": event, "data": data}}


@DBOS.workflow(name=WORKFLOW, serialization_type=WorkflowSerializationFormat.PORTABLE)
async def execute(payload):
    result = await perform(payload)
    await publish(result["terminal"]["event"], result["terminal"]["data"])
    await DBOS.close_stream_async(STREAM)
    return {"status": result["status"]}
