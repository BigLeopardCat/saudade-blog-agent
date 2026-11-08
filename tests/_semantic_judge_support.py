"""Real LangChain/OpenEvals models over isolated HTTP responses."""

import json

import httpx
import pytest
from langchain_openai import ChatOpenAI
from pydantic import Field

_CLIENTS = []


@pytest.fixture(autouse=True)
def judge_clients():
    yield
    for client in _CLIENTS:
        client.close()
    _CLIENTS.clear()


class Model(ChatOpenAI):
    messages: list = Field(default_factory=list, exclude=True)

    def __init__(self, verdict=None, *, error=None, finish="stop"):
        verdict = verdict or {
            "unsupported": [], "answered": True, "verdict": "ok", "reason": "有依据"}

        def provider(request):
            self.messages.append(json.loads(request.content)["messages"])
            if error:
                raise error
            return httpx.Response(200, json={"id": "fixture", "model": "fixture-judge",
                "choices": [{"index": 0, "finish_reason": finish, "message": {
                    "role": "assistant", "content": json.dumps(verdict, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}})

        client = httpx.Client(transport=httpx.MockTransport(provider))
        _CLIENTS.append(client)
        super().__init__(model="fixture-judge", api_key="fixture", base_url="https://model.test/v1",
                         http_client=client, max_retries=0, streaming=False)

