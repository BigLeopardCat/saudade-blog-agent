"""Repeatable hybrid task outcomes: real model, synthetic business API and storage.

Run with .venv/bin/python -m eval.native_contracts. No business database is used.
This checks model routing and confirmation outcomes, not PostgreSQL durability.
"""

import json
import sys
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import jwt
from langgraph.checkpoint.memory import InMemorySaver
from starlette.requests import Request

from agent import confirm, idem
from agent.agent import create_agent
from agent.runtime import writes
from agent.runtime.hybrid import HybridArm
from eval import request_metrics
from eval.own_fixtures import SECRET, favorite_backend, notification_backend
from utils.trace_io import load_trace

# run_golden's existing helpers still use imports relative to its script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402
from eval.run_golden import ChatRequest, golden_trace, run_one  # noqa: E402


class _Store:
    def __init__(self, path):
        self.path = path

    def claim(self, *args, **kwargs):
        with closing(idem.Ledger(self.path)) as store:
            return store.claim(*args, **kwargs)

    def finish(self, *args):
        with closing(idem.Ledger(self.path)) as store:
            return store.finish(*args)


def _principal():
    token = jwt.encode({"sub": 7, "role": "user", "aud": "agent",
                        "exp": int(time.time()) + 600}, SECRET, algorithm="HS256")
    request = Request({"type": "http", "headers": [
        (b"x-agent-assertion", token.encode())]})
    principal = server._resolve_principal(request, 7)
    if principal.source != "assertion":
        raise RuntimeError("synthetic identity did not pass production verification")
    return principal


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _card(result, executor):
    tokens = result["confirm_tokens"]
    _require(len(tokens) == 1, "expected exactly one confirmation")
    grant = confirm.verify(tokens[0], 7, 42)
    _require(grant and grant.get("executor") == executor, "incorrect signed executor")
    return tokens[0], grant


def _posts(state):
    return [r for r in state["requests"] if r.method in {"POST", "DELETE", "PUT", "PATCH"}]


def _chat(turn, state):
    result = turn("你好，随便聊聊，不需要查资料。")
    _require(result["text"].strip() and not result["tool_calls"], "chat used tools or had no reply")
    trace = load_trace(result["trace"])
    _require(trace and request_metrics.summary(trace)["calls"] == 1,
             "chat did not complete in one model call")
    _require(not any(e.get("event") in {"load_domain", "call"}
                     for e in trace["events"]), "chat loaded or called tools")
    _require(not _posts(state), "chat wrote business data")


def _favorite_cancel(turn, state):
    prepared = turn("请收藏文章19。")
    token, grant = _card(prepared, "favorites")
    _require(grant["specs"] == [{"tool": "add_favorite", "args": {"article_id": 19}}],
             "favorite target changed")
    _require(not _posts(state), "preparation wrote business data")
    cancelled = turn("算了，取消刚才的收藏，不要执行。")
    _require(not cancelled["confirm_tokens"], "cancellation created another card")
    stale = turn("", token)
    _require(not stale["exec_rows"] and not _posts(state), "cancelled card executed")
    _require(server._agent.get_state({"configurable": {"thread_id": "conv_42"}})
             .values.get("pending_action") is None, "pending card was not cleared")


def _notifications(turn, state):
    prepared = turn("请把我的全部未读通知标记为已读。")
    token, grant = _card(prepared, "notifications")
    args = {"ids": [101, 102], "all": False}
    _require(grant["specs"] == [{"tool": "read_notifications", "args": args}],
             "notification scope was not fixed")
    _require(not _posts(state), "preparation wrote business data")
    state["items"].append({"id": 104, "title": "新到通知", "isRead": False})
    executed = turn("", token)
    replay = turn("", token)
    _require(executed["exec_rows"] and replay["exec_rows"], "missing factual receipt")
    _require(len(_posts(state)) == 1 and json.loads(_posts(state)[0].content) == args,
             "confirmation or replay wrote unexpected data")
    _require(all(row["isRead"] == (row["id"] != 104) for row in state["items"]),
             "read state or exclusion of new notifications is incorrect")


def run():
    _require(golden_trace.enabled(), "native contracts require evaluation tracing")
    run_id = golden_trace.resolve_run_id()
    paths, results = [], []
    cases = [("chat", favorite_backend, _chat),
             ("favorite_cancel", favorite_backend, _favorite_cancel),
             ("notification_replay", notification_backend, _notifications)]
    for name, backend, contract in cases:
        with backend() as state, TemporaryDirectory(prefix="agent-native-eval-") as temp:
            graph = create_agent(checkpointer=InMemorySaver())
            arm = HybridArm(graph)
            store = _Store(str(Path(temp) / "ledger.sqlite3"))
            principal = _principal()
            rounds = []

            def turn(message, token=None):
                result = run_one(ChatRequest(message=message, user_id=7, conversation_id=42),
                                 principal, {"run": run_id, "case": f"native_{name}_{len(rounds)}"},
                                 thread="conv_42", confirm_token=token or "")
                rounds.append(result)
                if result["trace"]:
                    paths.append(result["trace"])
                _require(not result["error"], "runtime error; inspect the case trace")
                return result

            with patch.object(server, "_agent", arm), \
                    patch.object(writes, "ledger_for", lambda _: store), \
                    patch.object(graph, "stream",
                                 side_effect=RuntimeError("unexpected legacy handoff")):
                try:
                    contract(turn, state)
                    results.append({"case": name, "passed": True, "requests": len(rounds)})
                except (ValueError, RuntimeError) as exc:
                    results.append({"case": name, "passed": False, "reason": str(exc),
                                    "requests": len(rounds)})
    return {"trace_run": run_id, "business_api": "synthetic", "checkpoint": "memory",
            "ledger": "temporary_sqlite", "cases": results,
            "metrics": request_metrics.collect(paths)}


def main():
    report = run()
    path = Path("eval/report") / f"_smoke_native_contracts_{report['trace_run']}" / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"report": str(path), "cases": report["cases"]}, ensure_ascii=False))
    return 0 if all(case["passed"] for case in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
