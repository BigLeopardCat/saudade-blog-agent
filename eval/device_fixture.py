"""Isolated OLED demo API; real tools run without contacting a device service."""

import json
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from agent import confirm
from agent.domains import device_actions, device_reads
from eval.own_fixtures import SECRET
from tools import base, device


@contextmanager
def backend():
    state = {"requests": [], "unsupported_reads": []}

    def handle(request):
        state["requests"].append(request)
        if request.method == "PUT" and request.url.path.endswith("/isolated-demo/cmd"):
            if json.loads(request.content).get("type") != "display":
                return httpx.Response(400)
            state["request_id"] = request.headers["X-Request-Id"]
            return httpx.Response(200, json={"published": True, "device_id": "isolated-demo",
                                            "req_id": state["request_id"]})
        if request.method == "GET" and request.url.path.endswith(
                "/isolated-demo/cmd/" + state.get("request_id", "missing")):
            return httpx.Response(200, json={"acked": False, "device_id": "isolated-demo",
                                            "req_id": state["request_id"]})
        state["unsupported_reads"].append(request.url.path)
        return httpx.Response(404)

    def client(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handle), **kwargs)

    with ExitStack() as stack:
        api = stack.enter_context(client())
        for owner, key, value in [
            (base, "_client", api),
            (confirm.settings, "jwt_secret", SECRET),
            (device_reads.settings, "iot_enabled", True),
            (device_reads, "_read", lambda uid: [
                {"id": "isolated-demo", "name": "isolated", "online": True}]),
            (device, "_jwt_secret", lambda: SECRET),
            (device_actions, "httpx", SimpleNamespace(Client=client, HTTPError=httpx.HTTPError)),
        ]:
            stack.enter_context(patch.object(owner, key, value))
        yield state
