"""Real HTTP adapter for the existing golden multi-round driver.

Conversations belong to the selected real account. Only conversations created
by this adapter are deleted. Confirmation credentials never leave memory.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx
import jwt
from httpx_sse import connect_sse

from agent import confirm
from agent.transport import command_wire
from config.settings import settings
from tools.base import _sign_local_jwt


class EvaluationError(RuntimeError):
    """The evaluation cannot establish the evidence needed for a judgment."""


def safe_result(value):
    """Keep evidence but remove bearer credentials from nested wire objects."""
    if isinstance(value, dict):
        return {k: safe_result(v) for k, v in value.items()
                if k not in {"token", "confirm_token", "confirm_tokens"}}
    if isinstance(value, list):
        return [safe_result(v) for v in value]
    if isinstance(value, str) and value.startswith(("__CONFIRM__:", "__PENDING__:")):
        prefix, _, body = value.partition(":")
        return prefix + ":" + json.dumps(safe_result(json.loads(body)), ensure_ascii=False)
    return value


class OnlineTransport:
    def __init__(self, *, agent_url, blog_url, report_dir, client=None, host=False):
        self.agent_url = agent_url.rstrip("/")
        self.blog_url = blog_url.rstrip("/")
        self.report_dir = Path(report_dir)
        self.client = client or httpx.Client(timeout=180, trust_env=False)
        self.host = host
        self.uid = None
        self.role = None
        self.cid = None
        self.requests = []
        self.cleaned_conversations = []

    def host_headers(self, uid):
        return {"Authorization": "Bearer " + _sign_local_jwt(uid, "user")}

    def profile(self, uid, expected_roles):
        response = self.client.get(self.blog_url + "/api/protected/profile",
                                   headers=self.host_headers(uid))
        response.raise_for_status()
        role = response.json()["data"]["role"]
        if role not in expected_roles:
            raise EvaluationError(f"account {uid}: role {role!r}, expected {expected_roles}")
        return role

    def check_quota_fixture(self, account_id):
        from agent.domains.quota_actions.targets import state

        if type(account_id) is not int or account_id <= 0:
            raise EvaluationError("invalid quota fixture account ID")
        response = self.client.get(
            self.blog_url + f"/api/protected/quota/actions/accounts/{account_id}",
            headers=self.host_headers(self.uid))
        response.raise_for_status()
        value = state(response.json()["data"])
        if value["userId"] != account_id or value["unlimited"] or value["used"] == 0:
            raise EvaluationError("quota fixture needs the specified limited account with used > 0")

    @contextmanager
    def conversation(self, uid, expected_roles):
        if self.cid is not None:
            raise EvaluationError("a conversation is already active")
        self.uid, self.role = uid, self.profile(uid, expected_roles)
        response = self.client.post(self.blog_url + "/api/chat/conversations",
                                    headers=self.host_headers(uid))
        response.raise_for_status()
        self.cid = int(response.json()["id"])
        try:
            yield self.cid
        finally:
            cid = self.cid
            self.cid = None
            response = self.client.delete(self.blog_url + f"/api/chat/conversations/{cid}",
                                          headers=self.host_headers(uid))
            response.raise_for_status()
            self.cleaned_conversations.append(cid)

    def check_category_fixture(self, name):
        """Verify creation preconditions through the same read-only service contract."""
        from agent.domains.categories import targets

        request = targets.intent({"action": "create", "fields": {"name": name}})
        response = self.client.post(self.blog_url + targets.PATH + "/prepare",
                                    headers=self.host_headers(self.uid), json=request)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise EvaluationError(
                "category fixture cannot prepare a new category; check name/path collisions")
        try:
            targets.plan(payload["data"], request)
        except (ValueError, TypeError, KeyError) as exc:
            raise EvaluationError("category fixture has no valid new-category plan") from exc

    def _headers(self, request_id):
        if self.host:
            return {**self.host_headers(self.uid), "X-Request-ID": request_id,
                    "X-Agent-Stream-Version": "2"}
        if not settings.jwt_secret:
            raise EvaluationError("JWT secret is unavailable")
        token = jwt.encode({"sub": self.uid, "role": self.role, "aud": "agent",
                            "exp": int(time.time()) + 180}, settings.jwt_secret,
                           algorithm="HS256")
        return {"X-Agent-Assertion": token, "X-Request-ID": request_id}

    def _trace(self, request_id):
        deadline = time.monotonic() + 5
        while True:
            paths = list(Path(settings.trace_dir).glob(f"*/*_{request_id[:8]}.json"))
            if len(paths) == 1:
                doc = json.loads(paths[0].read_text())
                if doc.get("trace_id") != request_id:
                    raise EvaluationError("trace ID mismatch")
                meta = doc.get("input") or {}
                if (meta.get("principal_source") != "assertion"
                        or meta.get("principal_role") != self.role
                        or doc.get("user_id") != self.uid
                        or meta.get("conversation_id") != self.cid):
                    raise EvaluationError("verified HTTP principal/session differs from fixture")
                folder = self.report_dir / "traces"
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / (request_id + ".json")
                path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
                return path, doc
            if len(paths) > 1 or time.monotonic() >= deadline:
                raise EvaluationError(f"expected one completed trace, got {len(paths)}")
            time.sleep(0.05)

    def _consume(self, payload, request_id, started):
        if payload.get("confirm_token") or payload.get("confirm_pick"):
            raise EvaluationError("online regression must not approve business mutations")
        path = "/api/chat/stream" if self.host else "/chat/stream/v2"
        root = self.blog_url if self.host else self.agent_url
        events, done = [], False
        first = {key: None for key in ("first_visible_s", "first_reply_s",
                                      "first_command_s", "first_confirmation_s")}
        with connect_sse(self.client, "POST", root + path,
                         headers=self._headers(request_id), json=payload) as stream:
            stream.response.raise_for_status()
            for event in stream.iter_sse():
                if done:
                    raise EvaluationError("event after done")
                kind, data = event.event, json.loads(event.data)
                if kind not in {"text", "command", "receipts", "confirmation", "pending",
                                "progress", "summary", "done", "error"}:
                    raise EvaluationError(f"unknown SSE event {kind!r}")
                if kind in {"text", "progress", "command", "confirmation"}:
                    if first["first_visible_s"] is None:
                        first["first_visible_s"] = time.monotonic() - started
                if kind == "text":
                    if not isinstance(data, str):
                        raise EvaluationError("text event is not a string")
                field = {"text": "first_reply_s", "command": "first_command_s",
                         "confirmation": "first_confirmation_s"}.get(kind)
                if field and first[field] is None:
                    first[field] = time.monotonic() - started
                events.append((kind, data))
                done = kind == "done"
        stream_errors = [data for kind, data in events if kind == "error"]
        if not done and not stream_errors:
            raise EvaluationError("HTTP stream ended without done")
        return events, stream_errors, first

    def __call__(self, req, principal=None, **_kwargs):
        if self.cid is None:
            raise EvaluationError("no owned conversation")
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        payload = req.model_dump()
        if self.host and isinstance(payload["image"], str):
            payload["image"] = [payload["image"]] if payload["image"] else []
        payload.update(user_id=self.uid, conversation_id=self.cid)
        events, errors, first = self._consume(payload, request_id, started)
        try:
            trace_path, doc = self._trace(request_id)
        except EvaluationError as exc:
            if errors:
                raise EvaluationError(
                    f"HTTP SSE error without completed Agent trace: {errors}") from exc
            raise
        timing = {"request_id": request_id, "elapsed_s": time.monotonic() - started,
                  **first,
                  "trace": str(trace_path), "transport": "blog_http" if self.host else "agent_http"}
        self.requests.append(timing)
        result = self._result(events, errors, trace_path, doc, timing)
        result["request_evidence"] = {"message": payload["message"], "request_id": request_id}
        return result

    def _result(self, events, errors, trace_path, doc, timing):
        actual = [e for e in doc.get("events", [])
                  if e.get("node") == "native" and e.get("event") == "call"]
        tokens = [d["token"] for k, d in events if k == "confirmation"]
        for token in tokens:
            if confirm.verify(token, self.uid, self.cid) is None:
                raise EvaluationError("invalid confirmation returned by service")
        prefixes = {"command": "__CMD__:", "receipts": "__EXEC__:",
                    "confirmation": "__CONFIRM__:", "pending": "__PENDING__:"}
        frames = [prefixes[k] + json.dumps(d, ensure_ascii=False)
                  for k, d in events if k in prefixes]
        rows = [row for k, data in events if k == "receipts" for row in data]
        return {"text": "".join(d for k, d in events if k == "text"),
                "commands": [command_wire(d) for k, d in events if k == "command"],
                "tool_calls": [e["name"] for e in actual],
                "tool_rounds": len({e.get("round") for e in actual}), "frames": frames,
                "exec_rows": rows, "exec_tools": [r.get("tool") for r in rows],
                "trace": str(trace_path), "confirm_tokens": tokens,
                "confirm_payloads": [confirm.inspect(t) for t in tokens],
                "task_frames": [], "ledger_frames": [], "resets": 0,
                "resets_reasons": [], "reset_scopes": [], "fallback_reasons": [],
                "error": str(errors) if errors else None,
                "native_events": doc.get("events", []),
                "http_events": safe_result([{"event": k, "data": d} for k, d in events]),
                "http_timing": timing}
