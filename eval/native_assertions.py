"""Outcome checks over native observations, independent of planner node shape."""

import json
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from tools.base import SITE_BASE
from utils.trace_io import load_trace


def command_matches(expected, actual):
    """Match navigation destinations by URL, preserving origin/query/fragment."""
    kind, _, target = expected.partition(":")
    actual_kind, _, actual_target = actual.partition(":")
    navigation = {"NAVIGATE", "AUTO_NAVIGATE"}
    if kind in navigation:
        return actual_kind in navigation and (
            not target
            or urljoin(SITE_BASE + "/", target) == urljoin(SITE_BASE + "/", actual_target)
        )
    return expected in actual


def contains(actual, expected):
    """Match declared object fields while preserving list length and order."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and contains(actual[k], v) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            contains(value, wanted) for value, wanted in zip(actual, expected)
        )
    return isinstance(actual, bool) == isinstance(expected, bool) and actual == expected


def _answer_value(wanted, result, calls):
    values = []
    for event in calls:
        if event.get("name") not in wanted["tools"] or event.get("kind") != "ok":
            continue
        try:
            value = json.loads(event.get("result", ""))
            for key in wanted["path"]:
                value = value[key]
        except (ValueError, KeyError, TypeError):
            continue
        values.append(str(value))
    match = re.search(wanted["pattern"], result.get("text", ""))
    return bool(values and match and match.group(1) in values)


def _answer_list(wanted, result, calls):
    expected = None
    for event in calls:
        if event.get("name") not in wanted["tools"] or event.get("kind") != "ok":
            continue
        try:
            items = json.loads(event.get("result", ""))
            for key in wanted["path"]:
                items = items[key]
            expected = (
                [item[wanted["field"]] for item in items] if isinstance(items, list) else None
            )
            if expected is not None and not all(isinstance(value, str) for value in expected):
                expected = None
        except (ValueError, KeyError, TypeError):
            expected = None
    if expected is None:
        return False
    text = result.get("text", "").strip()
    if wanted["format"] == "json" or not expected:
        try:
            return json.loads(text) == expected
        except ValueError:
            return False
    return wanted["format"] == "lines" and text.splitlines() == expected


def _runtime_stops(events):
    return [
        e.get("event")
        for e in events
        if e.get("event")
        in {
            "budget_stop",
            "task_stopped",
            "handoff_stopped",
            "legacy_handoff",
            "answer_truncated",
            "answer_blocked",
            "invalid_tool_call",
        }
        or e.get("event") == "llm_done"
        and e.get("finish_reason") == "length"
    ]


def _call_key(event):
    call_id, round_index = event.get("call_id"), event.get("round")
    return ((round_index, call_id)
            if type(round_index) is int and isinstance(call_id, str) and call_id else None)


def _preparation_facts(events):
    facts = {}
    for event in events:
        key, source, value = _call_key(event), event.get("source"), event.get("facts")
        if (event.get("event") == "preparation_observation" and key is not None
                and event.get("complete") is True and isinstance(source, str)
                and isinstance(value, dict)):
            facts.setdefault(key, {}).setdefault(source, {}).update(value)
    return facts


def observations(events):
    """Apply defaults declared by the actual bound tool, never guessed defaults."""
    defaults, calls, facts = {}, [], _preparation_facts(events)
    for event in events:
        if event.get("event") == "tool_contract":
            function = (event.get("schema") or {}).get("function") or {}
            properties = (function.get("parameters") or {}).get("properties") or {}
            defaults[function.get("name")] = {
                k: v["default"] for k, v in properties.items() if "default" in v
            }
        elif event.get("node") == "native" and event.get("event") == "call":
            details = facts.get(_call_key(event))
            calls.append(
                {**event, "args": {**defaults.get(event.get("name"), {}), **event.get("args", {})},
                 **({"facts": details} if details else {})}
            )
    return calls


def _matches_observation(wanted, event):
    args = (wanted.get("args_by_tool") or {}).get(event.get("name"), wanted.get("args", {}))
    if (
        event.get("name") not in wanted["tools"]
        or not contains(event.get("args", {}), args)
        or wanted.get("kinds")
        and event.get("kind") not in wanted["kinds"]
        or wanted.get("facts")
        and not contains(event.get("facts"), wanted["facts"])
    ):
        return False
    expected = (wanted.get("result_by_tool") or {}).get(event.get("name"), wanted.get("result"))
    if expected is not None:
        try:
            return contains(json.loads(event.get("result", "")), expected)
        except (ValueError, TypeError):
            return False
    return True


def _scheduled_todo(wanted, result):
    document = load_trace(result.get("trace")) if result.get("trace") else None
    try:
        due = (
            datetime.fromisoformat(document["started_at"]).date() + timedelta(days=wanted["days"])
        ).isoformat()
    except (ValueError, TypeError, KeyError):
        return False
    return any(
        spec.get("tool") == "create_dashboard_todo"
        and contains(spec.get("args"), {"text": wanted["text"], "date": due})
        for payload in result.get("confirm_payloads", [])
        for spec in payload.get("specs", [])
    )


def _automation_schedule(wanted, result):
    document = load_trace(result.get("trace")) if result.get("trace") else None
    try:
        zone = ZoneInfo(wanted.get("timezone", "Asia/Shanghai"))
        started = datetime.fromisoformat(document["started_at"]).astimezone(zone)
        for payload in result.get("confirm_payloads", []):
            for spec in payload.get("specs", []):
                if spec.get("tool") != "create_automation":
                    continue
                definition = spec.get("args", {}).get("definition", {})
                if definition.get("action") != wanted["action"] or definition.get("cron"):
                    continue
                if "delay_seconds" in wanted:
                    if (
                        definition.get("delay_seconds") == wanted["delay_seconds"]
                        and definition.get("run_at") is None
                    ):
                        return True
                elif definition.get("delay_seconds") is None:
                    stamp = datetime.fromisoformat(definition["run_at"].replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        return False
                    local = stamp.astimezone(zone)
                    due = started.date() + timedelta(days=wanted["days"])
                    if local.date() == due and local.strftime("%H:%M:%S") == wanted["clock"]:
                        return True
    except (ValueError, TypeError, KeyError):
        return False
    return False


def _confirmation_preview(pattern, result):
    previews = [
        event.get("data", {}).get("q", "")
        for event in result.get("http_events", [])
        if event.get("event") == "confirmation" and isinstance(event.get("data"), dict)
    ]
    return any(isinstance(text, str) and re.search(pattern, text) for text in previews)


def _prepared_or_noop(wanted, result, events):
    specs = [
        spec
        for payload in result.get("confirm_payloads", [])
        if payload.get("workflow") == wanted["workflow"] and len(payload.get("specs", [])) == 1
        for spec in payload["specs"]
    ]
    if any(contains(spec, wanted["spec"]) for spec in specs):
        proof = wanted.get("prepared_observations")
        return not proof or any(_matches_observation(expected, call)
                                for expected in proof for call in observations(events))
    if any(_matches_observation(proof, call)
           for proof in wanted.get("noop_observations", []) for call in observations(events)):
        return True
    noop_calls = {
        (event.get("round"), event.get("call_id"))
        for event in events
        if event.get("event") == "call"
        and event.get("kind") == "noop"
        and event.get("name") in wanted.get("noop_tools", [])
        and event.get("call_id")
    }
    return any(
        event.get("event") == "preparation_observation"
        and (event.get("round"), event.get("call_id")) in noop_calls
        and event.get("complete") is True
        and event.get("source") == wanted["source"]
        and contains(event.get("facts"), wanted["facts"])
        for event in events
    )


def _outcome_failures(gold, result, events):
    limit = gold.get("max_model_calls")
    attempts = sum(event.get("event") == "llm_start" for event in events)
    budget = ([f"模型调用超过场景验收上限：{attempts} > {limit}"]
              if limit is not None and attempts > limit else [])
    return budget + [
        "缺少匹配的确认操作或同次工具核实的无需改动事实"
        for wanted in gold.get("require_prepared_or_noop", [])
        if not _prepared_or_noop(wanted, result, events)
    ]


def _compaction_failures(gold, events):
    compacted = any(
        event.get("node") == "memory"
        and event.get("event") == "compacted"
        and event.get("source_messages", 0) > 0
        for event in events
    )
    if gold.get("require_memory_compaction") and not compacted:
        return ["缺少成功覆盖历史消息的框架压缩证据"]
    if gold.get("forbid_memory_compaction") and (
        compacted
        or any(e.get("node") == "summarizer" and e.get("event") == "llm_start" for e in events)
    ):
        return ["容量充足的历史回忆不应额外调用摘要模型"]
    return []


def _answer_failures(gold, result, calls):
    failures = []
    for wanted in gold.get("require_answer_value", []):
        if not _answer_value(wanted, result, calls):
            failures.append(f"回答值不来自本轮实际读取结果：{wanted}")
    for wanted in gold.get("require_answer_list", []):
        if not _answer_list(wanted, result, calls):
            failures.append(f"回答列表与最近一次实际读取结果或指定格式不符：{wanted}")
    return failures


def _revoked(events):
    calls = {
        (event.get("round"), event.get("call_id"))
        for event in events
        if event.get("node") == "native"
        and event.get("event") == "call"
        and event.get("name") == "cancel_pending_operation"
        and event.get("call_id")
    }
    facts = [
        event.get("facts", {})
        for event in events
        if event.get("node") == "native"
        and (
            event.get("event") == "approval_feedback"
            or event.get("event") == "operation_observation"
            and (event.get("round"), event.get("call_id")) in calls
        )
    ]
    phases = [
        event
        for event in events
        if event.get("node") == "native" and event.get("event") == "turn_state"
    ]
    return (
        any(
            fact.get("status") in {"cancelled", "superseded"}
            and fact.get("scope") == "operation"
            and fact.get("executed") is False
            for fact in facts
        )
        and bool(phases)
        and phases[-1].get("phase") == "cancelled"
        and phases[-1].get("receipt_count") == 0
    )


def _history_scope(result, calls):
    """Optional history reads must stay inside the request's actual conversation."""
    allowed = {"get_chat_history", "load_own_activity_info_tools"}
    if any(call.get("name") not in allowed for call in calls):
        return False
    reads = [call for call in calls if call.get("name") == "get_chat_history"]
    if not reads:
        return True
    document = load_trace(result.get("trace")) if result.get("trace") else None
    cid = ((document or {}).get("input") or {}).get("conversation_id")
    if type(cid) is not int or cid <= 0:
        return False
    for call in reads:
        if set(call.get("args", {})) - {"limit", "before_id", "message_id", "content_offset"}:
            return False
        if call.get("kind") != "ok":
            continue
        try:
            data = json.loads(call.get("result", ""))
        except (ValueError, TypeError):
            return False
        if (
            not isinstance(data, dict)
            or type(data.get("conversationId")) is not int
            or data["conversationId"] != cid
            or not isinstance(data.get("items"), list)
        ):
            return False
    return True


def _conversation_failures(gold, result, events, calls):
    failures = []
    if gold.get("require_revoked_operation") and not _revoked(events):
        failures.append("缺少本次操作已撤销、未执行且已退出待确认状态的证据")
    if gold.get("require_current_conversation_history") and not _history_scope(result, calls):
        failures.append("历史查询超出当前会话范围或缺少可核对的会话证据")
    return failures


def check(gold, result):
    keys = {
        "max_model_calls",
        "require_observations",
        "require_observations_any",
        "require_todo_schedule",
        "require_automation_schedule",
        "require_confirm_specs",
        "require_confirm_preview",
        "forbid_runtime_stop",
        "forbid_business_write",
        "require_answer_value",
        "require_prepared_or_noop",
        "require_memory_compaction",
        "forbid_memory_compaction",
        "require_answer_list",
        "require_revoked_operation",
        "require_current_conversation_history",
    }
    if not keys.intersection(gold):
        return []
    events = result.get("native_events")
    if not isinstance(events, list):
        return ["[未评估] 缺少原生运行时事件，不能验证任务结果"]
    failures = []
    calls = observations(events)
    failures.extend(_outcome_failures(gold, result, events))
    failures.extend(_compaction_failures(gold, events))
    failures.extend(_answer_failures(gold, result, calls))
    failures.extend(_conversation_failures(gold, result, events, calls))
    for wanted in gold.get("require_observations", []):
        if not any(_matches_observation(wanted, event) for event in calls):
            failures.append(f"缺少实际工具观察：{wanted}")
    alternatives = gold.get("require_observations_any")
    if alternatives and not any(
        _matches_observation(wanted, event) for wanted in alternatives for event in calls
    ):
        failures.append("缺少任一可验证的工具结果路径")
    schedule = gold.get("require_todo_schedule")
    if schedule and not _scheduled_todo(schedule, result):
        failures.append("确认卡待办正文或相对排期不符，或缺少本轮时间证据")
    automation = gold.get("require_automation_schedule")
    if automation and not _automation_schedule(automation, result):
        failures.append("确认卡的定时动作或时间基准不符，或缺少请求时间证据")
    for wanted in gold.get("require_confirm_specs", []):
        specs = [s for p in result.get("confirm_payloads", []) for s in p.get("specs", [])]
        if not any(contains(s, wanted) for s in specs):
            failures.append(f"确认卡目标或参数不符：{wanted}")
    for pattern in gold.get("require_confirm_preview", []):
        if not _confirmation_preview(pattern, result):
            failures.append(f"HTTP 确认卡展示内容不符或缺失：{pattern}")
    if gold.get("forbid_runtime_stop"):
        stops = _runtime_stops(events)
        if stops:
            failures.append(f"运行时提前终止：{stops}")
    if gold.get("forbid_business_write"):
        writes = [
            e for e in events if e.get("event") in {"write_done", "write_error", "write_replay_hit"}
        ]
        rows = [r for r in result.get("exec_rows", []) if not r.get("cmd")]
        if writes or rows:
            failures.append("本轮不应执行后台写入")
    return failures
