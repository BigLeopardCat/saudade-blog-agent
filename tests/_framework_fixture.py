"""OpenAI wire fixtures and existing business operations for kernel evaluation."""

import json
from contextlib import closing, contextmanager

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agent import confirm, idem
from agent.domains.notices import preparation, transactions
from agent.runtime.results import TurnResult
from models.llm import get_llm


def call(name, ident="call-1", **args):
    return {"id": ident, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def _wire_reply(message, streaming):
    reason = "tool_calls" if message.get("tool_calls") else "stop"
    envelope = {"id": "fixture", "model": "gpt-4o-mini", "created": 0}
    usage = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
    if not streaming:
        return httpx.Response(200, json={**envelope, "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", **message},
                         "finish_reason": reason}], "usage": usage})
    delta = {"role": "assistant", **message}
    if delta.get("tool_calls"):
        delta["tool_calls"] = [{"index": index, **item}
                               for index, item in enumerate(delta["tool_calls"])]
    frames = [{**envelope, "object": "chat.completion.chunk", "choices": [
        {"index": 0, "delta": delta, "finish_reason": None}]},
        {**envelope, "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": {}, "finish_reason": reason}]},
        {**envelope, "object": "chat.completion.chunk", "choices": [], "usage": usage}]
    body = "".join("data: " + json.dumps(row, ensure_ascii=False) + "\n\n" for row in frames)
    return httpx.Response(200, text=body + "data: [DONE]\n\n",
                          headers={"content-type": "text/event-stream"})


@contextmanager
def model_replies(*messages):
    """Use the real model adapter while all provider HTTP stays in MockTransport."""
    responses, requests = iter(messages), []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        return _wire_reply(next(responses), bool(body.get("stream")))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        model = get_llm(model="gpt-4o-mini", api_key="test-only",
                        base_url="https://model.fixture.test/v1", http_client=client,
                        streaming=True, stream_usage=True, max_tokens=128, enable_thinking=False)
        yield model, requests


def prepared_notice(config, ledger_path):
    """Reuse signed preparation and transaction code, not a synthetic write shortcut.

    The candidate starts after preparation. Connecting proposals to the host card
    protocol is a separate acceptance item, not established by these tests.
    """
    proposal = preparation.prepare_user_notice.invoke(
        {"content": "请查看新的站内通知。", "username": "青叶"}, config=config)
    assert isinstance(proposal, TurnResult) and proposal.execution["pending_confirm"]
    token = proposal.execution["pending_confirm"]["token"]

    @tool
    def execute_prepared_notice(config: RunnableConfig) -> dict:
        """Execute the specific previously prepared and signed notice after approval."""
        actor = config["configurable"]["principal"]
        cid = config["configurable"]["conversation_id"]
        grant = confirm.verify(token, actor.uid, cid)
        if grant is None:
            raise ValueError("Confirmation does not match the current actor and conversation")
        # The framework may dispatch tools on worker threads; own the connection here.
        with closing(idem.Ledger(ledger_path)) as ledger:
            return transactions.execute(grant, config, None, ledger=ledger)

    return execute_prepared_notice, proposal.execution["confirm_text"], token
