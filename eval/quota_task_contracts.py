"""A direct account reset is independent from the pending application queue."""

import hashlib
import json
from contextlib import contextmanager
from unittest.mock import patch

import httpx

from agent import confirm
from eval.own_fixtures import SECRET
from tools import base


@contextmanager
def backend():
    state = {"caller_role": "admin", "requests": [], "unsupported_reads": []}
    account = {"userId": 722, "name": "synthetic_quota_user", "role": "user",
               "used": 19, "limit": 500, "unlimited": False, "unit": "conversation_turns"}
    account["version"] = hashlib.sha256(json.dumps(account, sort_keys=True).encode()).hexdigest()

    def handle(request):
        state["requests"].append(request)
        if request.method != "GET":
            raise AssertionError("quota preparation cannot perform a business mutation")
        if request.url.path == "/api/protected/quota/actions/accounts/722":
            data = account
        elif request.url.path == "/api/protected/quota/requests":
            data = []
        elif request.url.path == "/api/protected/accounts":
            data = [{"id": 722, "username": account["name"], "role": "user", "frozen": False,
                     "muted": False, "mutedUntil": None, "quotaUsed": 19, "quotaLimit": 500}]
        else:
            state["unsupported_reads"].append(request.url.path)
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 200, "data": data,
                                        "total": len(data) if isinstance(data, list) else 1})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client), \
            patch.object(confirm.settings, "jwt_secret", SECRET):
        yield state


def direct_reset(turn, state):
    result = turn("重置账号722的对话额度。")
    tokens = result["confirm_tokens"]
    grant = confirm.verify(tokens[0], 7, 42) if len(tokens) == 1 else None
    if not grant or grant.get("executor") != "quota_actions":
        raise ValueError("direct reset did not prepare one quota confirmation")
    spec = grant["specs"][0]
    if (spec["tool"] != "reset_user_quota" or spec["args"]["user_id"] != 722
            or {"request_id", "reason"}.intersection(spec["args"])):
        raise ValueError("direct reset changed target or became application approval")
    if result["exec_rows"] or any(r.method != "GET" for r in state["requests"]):
        raise ValueError("preparation performed a business mutation")


CASES = [("quota_direct_reset", backend, direct_reset)]
