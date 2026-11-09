"""Repeatable native task outcomes: real model, synthetic business API and storage.

Run with .venv/bin/python -m eval.native_contracts. No business database is used.
This checks model routing and confirmation outcomes, not PostgreSQL durability.
"""

import argparse
import json
import sys
import time
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

import jwt
from langgraph.checkpoint.memory import InMemorySaver
from starlette.requests import Request

from agent import checkpoint, confirm, delegation
from agent.contracts.events import confirmation_text
from agent.products.blog.application import BlogApplication
from agent.runtime import writes
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.control import RequestControl, request_scope
from agent.runtime.service import AgentRuntime
from config.settings import settings
from eval import (
    account_candidate_contracts,
    admin_task_contracts,
    article_tag_contracts,
    compaction_contracts,
    composite_task_contracts,
    confirmation_failure_contracts,
    content_contracts,
    context_contracts,
    efficiency_contracts,
    experience_contracts,
    gallery_contracts,
    image_generation_contracts,
    navigation_contracts,
    notification_scope_contracts,
    ordered_preparation_contracts,
    quota_task_contracts,
    reading_task_contracts,
    request_metrics,
    scheduled_change_contracts,
    task_lifecycle_contracts,
    todo_task_contracts,
)
from eval.fixtures.ledger import Ledger
from eval.own_fixtures import SECRET, favorite_backend, message_backend, notification_backend
from models.transport import ModelIO
from utils.trace_io import load_trace

# golden_evaluation's existing helpers still use imports relative to its script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent.api.authentication import resolve_principal  # noqa: E402
from eval import golden_evaluation  # noqa: E402
from eval.golden_evaluation import ChatRequest, golden_trace, run_one  # noqa: E402


class _Store:
    def __init__(self, path):
        self.path = path

    def claim(self, *args, **kwargs):
        with closing(Ledger(self.path)) as store:
            return store.claim(*args, **kwargs)

    def finish(self, *args):
        with closing(Ledger(self.path)) as store:
            return store.finish(*args)

    def lookup(self, *args):
        with closing(Ledger(self.path)) as store:
            return store.lookup(*args)


def _principal(role="user"):
    expires = int(time.time()) + 600
    capability = jwt.encode({"sub": 7, "role": role, "aud": "blog-tools", "ver": 0,
                             "scope": "tools", "exp": expires}, SECRET, algorithm="HS256")
    token = jwt.encode({"sub": 7, "role": role, "aud": "agent", "exp": expires,
                        "delegation": capability}, SECRET, algorithm="HS256")
    request = Request({"type": "http", "headers": [
        (b"x-agent-assertion", token.encode())]})
    with patch.object(settings, "agent_assertion_secret", SECRET):
        principal = resolve_principal(request, 7)
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
    read_posts = {"/api/public/notes/search", "/api/public/documents/validate",
                  "/api/public/activity/search"}
    return [r for r in state["requests"] if r.method in {"POST", "DELETE", "PUT", "PATCH"}
            and not (r.method == "POST" and (r.url.path in read_posts
                                             or r.url.path.endswith("/prepare")))]


def _chat(turn, state):
    result = turn("你好，随便聊聊，不需要查资料。")
    _require(result["text"].strip() and not result["tool_calls"], "chat used tools or had no reply")
    _require("主人" not in result["text"] and "杂鱼" not in result["text"],
             "ordinary-user chat used another role's default address")
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
    _require(golden_evaluation.ensure_agent().get_state(
        checkpoint.conversation_config(_principal(), 42))
             .values.get("pending_action") is None, "pending card was not cleared")


def _favorite_confirm(turn, state):
    prepared = turn("请收藏文章19。")
    token, grant = _card(prepared, "favorites")
    _require(grant["specs"] == [{"tool": "add_favorite", "args": {"article_id": 19}}],
             "favorite target changed")
    _require(not _posts(state), "preparation wrote business data")
    for replay in (False, True):
        result = turn("", token)
        _require(result["exec_rows"] and result["text"].strip(), "missing execution result")
        _require(not result["confirm_tokens"], "confirmation introduced another operation")
        trace = load_trace(result["trace"])
        model_called = bool(trace and any(event.get("node") == "native"
                            and event.get("event") == "llm_done" for event in trace["events"]))
        _require(model_called != replay, "first completion needs Agent; replay must reuse facts")
    _require(len(_posts(state)) == 1 and state["favorites"][0]["noteId"] == 19,
             "confirmation or replay wrote an unexpected favorite")


def _read_replay(turn, state, message, executor, tool, ids, arrival):
    prepared = turn(message)
    token, grant = _card(prepared, executor)
    args = {"ids": ids, "all": False}
    _require(grant["specs"] == [{"tool": tool, "args": args}], "read scope was not fixed")
    _require(not _posts(state), "preparation wrote business data")
    state["items"].append(arrival)
    executed = turn("", token)
    replay = turn("", token)
    _require(executed["exec_rows"] and replay["exec_rows"], "missing factual receipt")
    _require(len(_posts(state)) == 1 and json.loads(_posts(state)[0].content) == args,
             "confirmation or replay wrote unexpected data")
    _require(all(row["isRead"] == (row["id"] != arrival["id"]) for row in state["items"]),
             "read state or exclusion of new arrivals is incorrect")


def _notifications(turn, state):
    _read_replay(turn, state, "请把我的全部未读通知标记为已读。",
                 "notifications", "read_notifications", [101, 102],
                 {"id": 104, "title": "新到通知", "isRead": False})


def _inbox_counts(turn, state):
    result = turn("请告诉我未读通知数、站内信数与合计。只查计数，不查内容。")
    _require(result["text"].strip() and not result["confirm_tokens"], "no count reply")
    _require(len(state["requests"]) == 1 and
             state["requests"][0].url.path.endswith("/notifications/summary"),
             "count query fetched message or notification contents")
    trace = load_trace(result["trace"])
    _require(trace and request_metrics.summary(trace)["calls"] <= 2,
             "count query required an extra discovery round")


def _messages(turn, state):
    viewed = turn("我的收件箱里有哪些站内信？只看看，不要标记已读。")
    _require("欢迎来信" in viewed["text"] and not viewed["confirm_tokens"],
             "mailbox view did not return actual received-message facts")
    _require(not _posts(state), "viewing the mailbox changed read state")
    detail = turn("请读取收件箱里编号201那封信的正文，不要标记已读。")
    _require("仅单封正文" in detail["text"], "single-message content was not retrieved")
    _require(not _posts(state), "reading a message changed read state")
    _read_replay(turn, state, "请把我收到的全部未读站内信标记为已读。",
                 "messages", "read_messages", [201, 202],
                 {"id": 204, "toUserId": 7, "title": "新到来信", "isRead": False})
    _require(not any(row["isRead"] for row in state["outbox"]), "outbox state changed")


CASES = [("chat", favorite_backend, _chat),
         ("favorite_cancel", favorite_backend, _favorite_cancel),
         ("favorite_confirm", favorite_backend, _favorite_confirm),
         ("notification_replay", notification_backend, _notifications),
         ("inbox_counts", message_backend, _inbox_counts),
         ("message_read_replay", message_backend, _messages),
         *image_generation_contracts.CASES, *navigation_contracts.CASES,
         *experience_contracts.CASES, *admin_task_contracts.CASES,
         *notification_scope_contracts.CASES,
         *reading_task_contracts.CASES, *quota_task_contracts.CASES,
         *context_contracts.CASES, *composite_task_contracts.CASES,
         *account_candidate_contracts.CASES, *compaction_contracts.CASES,
         *article_tag_contracts.CASES, *confirmation_failure_contracts.CASES,
         *todo_task_contracts.CASES, *ordered_preparation_contracts.CASES,
         *task_lifecycle_contracts.CASES,
         *scheduled_change_contracts.CASES,
         *content_contracts.CASES, *gallery_contracts.CASES, *efficiency_contracts.CASES]


def _sample(name, backend, contract, run_id, sample):
    paths, rounds = [], []
    with (ExitStack() as resources, backend() as state,
          TemporaryDirectory(prefix="agent-native-eval-") as temp):
        credential_scope = delegation.clear()
        resources.callback(delegation.reset, credential_scope)
        arm = AgentRuntime(
            CheckpointConversationStore(InMemorySaver()),
            application=state.get("application") or BlogApplication(),
            model_io=resources.enter_context(ModelIO()),
        )
        store = _Store(str(Path(temp) / "ledger.sqlite3"))
        principal = _principal(state.get("caller_role", "user"))

        def turn(message, token=None):
            control = RequestControl(Event(), time.monotonic() + 90)
            started = time.monotonic()
            with request_scope(control):
                result = run_one(ChatRequest(message=message, user_id=7, conversation_id=42),
                                 principal, {"run": run_id,
                                             "case": f"native_{name}_{sample}_{len(rounds)}"},
                                 thread="conv_42", confirm_token=token or "")
            evidence = load_trace(result["trace"]) if result.get("trace") else None
            result["native_events"] = evidence["events"] if evidence else None
            # Persist observable outcomes, never signed confirmation credentials.
            rounds.append({"user_input": message, "text": result["text"],
                           "commands": result["commands"],
                           "confirm_payloads": result["confirm_payloads"],
                           "confirmation_previews": [
                               confirmation_text(json.loads(frame.partition(":")[2]))
                               for frame in result["frames"]
                               if frame.startswith("__CONFIRM__:")],
                           "exec_rows": result["exec_rows"],
                           "confirmation_count": len(result["confirm_tokens"]),
                           "error": result["error"], "trace": result["trace"],
                           "elapsed_s": round(time.monotonic() - started, 3),
                           "model_calls": control.budget.request_spent()["model_calls"],
                           "tool_attempts": control.budget.request_spent()["tool_calls"],
                           "charged_tokens": control.budget.request_spent()["tokens"],
                           "task_spent": control.budget.spent()})
            if result["trace"]:
                paths.append(result["trace"])
            _require(not result["error"], "runtime error; inspect the case trace")
            return result

        outcome = {"case": name, "sample": sample, "evaluated": True,
                   "passed": False, "rounds": rounds}
        with patch.object(golden_evaluation, "ensure_agent", lambda: arm), \
                patch.object(writes, "ledger_for", lambda _: store):
            try:
                contract(turn, state)
                if name.startswith("navigation_"):
                    _require(not _posts(state), "navigation wrote business data")
                outcome["passed"] = True
            except (ValueError, RuntimeError) as exc:
                outcome["reason"] = str(exc)
        outcome["requests"] = len(rounds)
        outcome["business_writes"] = len(_posts(state))
        if "contract_evidence" in state:
            outcome["evidence"] = state["contract_evidence"]
        if state.get("unsupported_reads"):
            outcome.update(evaluated=False, passed=False, reason="unsupported fixture read",
                           unsupported_reads=state["unsupported_reads"])
    return outcome, paths


def run(case_names=None, repeat=1, *, fail_fast=False, on_sample=None):
    _require(golden_trace.enabled(), "native contracts require evaluation tracing")
    _require(type(repeat) is int and 1 <= repeat <= 50, "repeat must be between 1 and 50")
    selected = set(case_names) if case_names is not None else {case[0] for case in CASES}
    _require(selected and selected <= {case[0] for case in CASES},
             "unknown or empty case selection")
    run_id = golden_trace.resolve_run_id()
    paths, results = [], []
    for sample in range(1, repeat + 1):
        for name, backend, contract in CASES:
            if name not in selected:
                continue
            result, traces = _sample(name, backend, contract, run_id, sample)
            results.append(result)
            paths.extend(traces)
            if on_sample is not None:
                on_sample(result)
            if fail_fast and not result["passed"]:
                break
        if fail_fast and results and not results[-1]["passed"]:
            break
    scores = [{"case": name,
               "samples": sum(row["case"] == name for row in results),
               "evaluated": sum(row["evaluated"] for row in results if row["case"] == name),
               "passed": sum(row["passed"] for row in results if row["case"] == name)}
              for name, _, _ in CASES if name in selected]
    return {"trace_run": run_id, "business_api": "synthetic", "checkpoint": "memory",
            "model_config": {"provider": settings.llm_provider,
                             "model": settings.active_llm_model,
                             "reasoning_effort": settings.agent_reasoning_effort},
            "navigation_retrieval": "synthetic_lexical",
            "ledger": "temporary_sqlite", "repeat": repeat,
            "planned_samples": len(selected) * repeat,
            "stopped_early": len(results) < len(selected) * repeat,
            "narration": "production", "scores": scores, "cases": results,
            "metrics": request_metrics.collect(paths)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=[case[0] for case in CASES])
    parser.add_argument("--repeat", type=int, choices=range(1, 51), default=1)
    parser.add_argument("--fail-fast", action="store_true", help="首次失败后停止扩量")
    args = parser.parse_args(argv)
    journal = Path("eval/report") / ("_native_journal_" + golden_trace.resolve_run_id() + ".jsonl")
    journal.parent.mkdir(parents=True, exist_ok=True)

    def completed(row):
        with journal.open("a") as output:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps({"case": row["case"], "sample": row["sample"],
                          "passed": row["passed"], "reason": row.get("reason")},
                         ensure_ascii=False), flush=True)

    report = run(args.case, args.repeat, fail_fast=args.fail_fast, on_sample=completed)
    report["sample_journal"] = str(journal)
    path = Path("eval/report") / f"_smoke_native_contracts_{report['trace_run']}" / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"report": str(path), "scores": report["scores"]}, ensure_ascii=False))
    return 0 if all(case["passed"] for case in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
