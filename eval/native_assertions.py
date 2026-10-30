"""Outcome checks over native observations, independent of planner node shape."""

import json
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

from tools.base import SITE_BASE
from utils.trace_io import load_trace


def command_matches(expected, actual):
    """Match navigation destinations by URL, preserving origin/query/fragment."""
    kind, _, target = expected.partition(":")
    actual_kind, _, actual_target = actual.partition(":")
    navigation = {"NAVIGATE", "AUTO_NAVIGATE"}
    if kind in navigation:
        return actual_kind in navigation and (not target or
            urljoin(SITE_BASE + "/", target) == urljoin(SITE_BASE + "/", actual_target))
    return expected in actual


def contains(actual, expected):
    """Recursive subset for objects; declared lists preserve their exact meaning."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and contains(actual[k], v) for k, v in expected.items())
    return actual == expected


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
            expected = ([item[wanted["field"]] for item in items]
                        if isinstance(items, list) else None)
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
    return [e.get("event") for e in events
            if e.get("event") in {"budget_stop", "task_stopped", "handoff_stopped",
                                  "legacy_handoff", "answer_truncated", "answer_blocked",
                                  "invalid_tool_call"}
            or e.get("event") == "llm_done" and e.get("finish_reason") == "length"]


def observations(events):
    """Apply defaults declared by the actual bound tool, never guessed defaults."""
    defaults, calls = {}, []
    for event in events:
        if event.get("event") == "tool_contract":
            function = (event.get("schema") or {}).get("function") or {}
            properties = (function.get("parameters") or {}).get("properties") or {}
            defaults[function.get("name")] = {k: v["default"] for k, v in properties.items()
                                               if "default" in v}
        elif event.get("node") == "native" and event.get("event") == "call":
            calls.append({**event, "args": {**defaults.get(event.get("name"), {}),
                                           **event.get("args", {})}})
    return calls


def _matches_observation(wanted, event):
    args = (wanted.get("args_by_tool") or {}).get(event.get("name"), wanted.get("args", {}))
    if (event.get("name") not in wanted["tools"] or not contains(event.get("args", {}), args)
            or wanted.get("kinds") and event.get("kind") not in wanted["kinds"]):
        return False
    if "result" in wanted:
        try:
            return contains(json.loads(event.get("result", "")), wanted["result"])
        except (ValueError, TypeError):
            return False
    return True


def _scheduled_todo(wanted, result):
    document = load_trace(result.get("trace")) if result.get("trace") else None
    try:
        due = (datetime.fromisoformat(document["started_at"]).date()
               + timedelta(days=wanted["days"])).isoformat()
    except (ValueError, TypeError, KeyError):
        return False
    return any(spec.get("tool") == "create_dashboard_todo"
               and contains(spec.get("args"), {"text": wanted["text"], "date": due})
               for payload in result.get("confirm_payloads", [])
               for spec in payload.get("specs", []))


def _confirmation_preview(pattern, result):
    previews = [event.get("data", {}).get("q", "")
                for event in result.get("http_events", [])
                if event.get("event") == "confirmation"
                and isinstance(event.get("data"), dict)]
    return any(isinstance(text, str) and re.search(pattern, text) for text in previews)


def _prepared_or_noop(wanted, result, events):
    specs = [spec for payload in result.get("confirm_payloads", [])
             if payload.get("workflow") == wanted["workflow"] and len(payload.get("specs", [])) == 1
             for spec in payload["specs"]]
    if any(contains(spec, wanted["spec"]) for spec in specs):
        return True
    noop_calls = {(event.get("round"), event.get("call_id")) for event in events
                  if event.get("event") == "call" and event.get("kind") == "noop"
                  and event.get("name") in wanted["noop_tools"] and event.get("call_id")}
    return any(event.get("event") == "preparation_observation"
               and (event.get("round"), event.get("call_id")) in noop_calls
               and event.get("complete") is True
               and event.get("source") == wanted["source"]
               and contains(event.get("facts"), wanted["facts"]) for event in events)


def _outcome_failures(gold, result, events):
    return ["缺少匹配的确认操作或同次工具核实的无需改动事实"
            for wanted in gold.get("require_prepared_or_noop", [])
            if not _prepared_or_noop(wanted, result, events)]


def _compaction_failures(gold, events):
    compacted = any(
            event.get("node") == "memory" and event.get("event") == "compacted"
            and event.get("source_messages", 0) > 0 for event in events)
    if gold.get("require_memory_compaction") and not compacted:
        return ["缺少成功覆盖历史消息的框架压缩证据"]
    if gold.get("forbid_memory_compaction") and (compacted or any(
            e.get("node") == "summarizer" and e.get("event") == "llm_start" for e in events)):
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


def check(gold, result):
    keys = {"require_observations", "require_observations_any", "require_todo_schedule",
            "require_confirm_specs", "require_confirm_preview", "forbid_runtime_stop",
            "forbid_business_write", "require_answer_value", "require_prepared_or_noop",
            "require_memory_compaction", "forbid_memory_compaction", "require_answer_list"}
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
    for wanted in gold.get("require_observations", []):
        if not any(_matches_observation(wanted, event) for event in calls):
            failures.append(f"缺少实际工具观察：{wanted}")
    alternatives = gold.get("require_observations_any")
    if alternatives and not any(_matches_observation(wanted, event)
                                for wanted in alternatives for event in calls):
        failures.append("缺少任一可验证的工具结果路径")
    schedule = gold.get("require_todo_schedule")
    if schedule and not _scheduled_todo(schedule, result):
        failures.append("确认卡待办正文或相对排期不符，或缺少本轮时间证据")
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
        writes = [e for e in events if e.get("event") in {"write_done", "write_error",
                                                         "write_replay_hit"}]
        rows = [r for r in result.get("exec_rows", []) if not r.get("cmd")]
        if writes or rows:
            failures.append("本轮不应执行后台写入")
    return failures
