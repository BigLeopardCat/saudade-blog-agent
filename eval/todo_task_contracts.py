"""Distinguish user-owned todo records from the framework's private execution plan."""

from contextlib import contextmanager
from unittest.mock import patch

import httpx

from agent import confirm
from eval.own_fixtures import SECRET
from tools import base


@contextmanager
def backend():
    state = {"caller_role": "admin", "requests": []}

    def handle(request):
        state["requests"].append(request)
        if request.method == "GET" and request.url.path == "/api/protected/todos":
            return httpx.Response(200, json={"code": 200, "data": []})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client), \
            patch.object(confirm.settings, "jwt_secret", SECRET):
        yield state


def _personal_todo(result, state):
    from eval.native_contracts import _require

    specs = [spec for card in result["confirm_payloads"] for spec in card.get("specs", [])]
    _require(specs == [{"tool": "create_dashboard_todo",
                        "args": {"text": "亲亲泠月喵", "date": None}}],
             "personal todo was not prepared as a business record")
    _require(not result["exec_rows"] and all(r.method == "GET" for r in state["requests"]),
             "todo record changed without approval")


def after_chat(turn, state):
    turn("猫咪，今天有点睡不着")
    _personal_todo(turn("猫咪加条待办，亲亲泠月喵。"), state)


def after_private_plan(turn, state):
    from eval.native_contracts import _require

    first = turn("请在你自己的内部执行清单记录两项：整理建议、核对结果。"
                 "只记录内部执行计划，不创建或改动我的后台待办。")
    _require("write_todos" in first["tool_calls"] and not first["confirm_tokens"]
             and not state["requests"], "private planning created a business operation")
    _personal_todo(turn("现在给我的后台待办加一条：亲亲泠月喵。不设排期。"), state)


CASES = [("todo_after_chat", backend, after_chat),
         ("todo_after_private_plan", backend, after_private_plan)]
