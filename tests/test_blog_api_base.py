"""Configured host origin reaches HTTP requests and authorized browser commands."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_PROBE = r'''
import json
import httpx
import tools.base as base
from agent.domains.browser import navigate_page
from agent.principal import SOURCE_ASSERTION, Principal

seen = []

def respond(request):
    seen.append([request.method, str(request.url)])
    return httpx.Response(200, json={"code": 200, "data": []})

base._client = httpx.Client(transport=httpx.MockTransport(respond))
base._get("/notes")
base.search_notes.invoke({"keyword": "x"})
config = {"configurable": {"user_id": 7,
          "principal": Principal(uid=7, role="user", source=SOURCE_ASSERTION)}}
commands = []
for path in ("/talk", "/device-console/"):
    text, artifact = navigate_page.func(path, config, "navigation")
    commands.append({"result": json.loads(text), "command": artifact["receipts"][0]["cmd"]})
print(json.dumps({"api_base": base.API_BASE, "site_base": base.SITE_BASE,
                  "outbound": seen, "commands": commands}))
'''


@pytest.mark.parametrize("configured,expected", [
    (None, "http://127.0.0.1:3000/api/public"),
    ("https://blog.example.test/api/public", "https://blog.example.test/api/public"),
])
def test_host_configuration_reaches_transport_and_browser(configured, expected):
    env = dict(os.environ, SAUDADE_IGNORE_ENV_FILE="1", IOT_ENABLED="1")
    env.pop("BLOG_API_BASE", None)
    if configured:
        env["BLOG_API_BASE"] = configured
    result = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT,
                            env=env, capture_output=True, text=True, check=True, timeout=60)
    output = json.loads(result.stdout.strip().splitlines()[-1])
    origin = expected.removesuffix("/api/public")
    assert output["api_base"] == expected and output["site_base"] == origin
    assert output["outbound"] == [["GET", expected + "/notes"],
                                   ["POST", expected + "/notes/search"]]
    for item, path in zip(output["commands"], ("/talk", "/device-console/"), strict=True):
        assert item["command"] == {"kind": "navigate", "url": origin + path, "mode": "direct"}
        assert item["result"]["status"] == "sent"
        assert item["result"]["completion_confirmed"] is False
