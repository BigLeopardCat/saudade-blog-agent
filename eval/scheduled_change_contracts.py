"""Real-model scheduling contracts with a synthetic category service and no business writes."""

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

from agent import confirm
from agent.domains.categories.targets import intent as category_intent
from agent.principal import Principal
from agent.products.blog.application import BlogApplication
from agent.scheduling.service import ScheduleService
from eval.own_fixtures import favorite_backend
from tools import base


@contextmanager
def backend():
    with favorite_backend() as state:
        state["caller_role"] = "admin"
        state["application"] = BlogApplication(scheduling=ScheduleService(
            None, lambda _: Principal(7, "admin", "assertion")))
        original = base._client

        def handle(request):
            path = request.url.path
            if path == "/api/protected/category-actions/prepare":
                state["requests"].append(request)
                intent = category_intent(json.loads(request.content))
                if intent["action"] != "create":
                    return httpx.Response(409)
                after = {"id": None, **intent["fields"]}
                after["pathName"] = after["pathName"] or after["name"]
                data = {"version": hashlib.sha256(request.content).hexdigest(),
                        "before": None, "after": after, "articleCount": 0, "revisionCount": 0}
                return httpx.Response(200, json={"code": 200, "data": data})
            if path in {"/api/category", "/api/public/catalog/categories"}:
                state["requests"].append(request)
                data = [] if path == "/api/category" else {"items": [], "total": 0, "page": 1}
                return httpx.Response(200, json={"code": 200, "data": data})
            return original.send(request)

        with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
                patch.object(base, "_client", client):
            yield state


def prepared(result, state):
    tokens = result["confirm_tokens"]
    grant = confirm.verify(tokens[0], 7, 42) if len(tokens) == 1 else None
    if not grant or grant.get("executor") != "automations":
        raise ValueError("requested future write did not produce scheduling approval")
    definition = grant["specs"][0]["args"]["definition"]
    if (definition["action"] != "admin_write"
            or definition["write"]["kind"] != "categories"
            or definition["write"]["change"]["intent"]["action"] != "create"
            or definition["write"]["change"]["intent"]["fields"]["name"] != "密码学"):
        raise ValueError("future category write was replaced with another action or reminder")
    if result["exec_rows"] or any(r.method != "GET" and not r.url.path.endswith("/prepare")
                                  for r in state["requests"]):
        raise ValueError("business write occurred before scheduling approval")
    state["contract_evidence"] = {"exact_scheduled_write": True, "business_writes": 0}
    return definition


def absolute_category(turn, state):
    tomorrow = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
    definition = prepared(turn("明天下午3点自动创建一个名为“密码学”的文章分类。"), state)
    stamp = datetime.fromisoformat(definition["run_at"]).astimezone(ZoneInfo("Asia/Shanghai"))
    if (stamp.date() != tomorrow or stamp.strftime("%H:%M:%S") != "15:00:00"
            or definition["delay_seconds"] is not None):
        raise ValueError("absolute clock time was changed or converted to relative seconds")


def relative_category(turn, state):
    definition = prepared(turn("五分钟后自动创建一个名为“密码学”的文章分类。"), state)
    if definition["delay_seconds"] != 300 or definition["run_at"] is not None:
        raise ValueError("relative delay was not anchored to approval")


CASES = [("scheduled_category_absolute", backend, absolute_category),
         ("scheduled_category_relative", backend, relative_category)]
