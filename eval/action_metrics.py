"""Native decision and action timing; discovery is distinct from business execution."""

from eval.statistical_metrics import proportion


def first_decision(events):
    first = next((event for event in events if event.get("node") == "native"
                  and event.get("event") == "llm_done"), None)
    if first is None or "tool_calls" not in first:
        return None
    return first["tool_calls"]


def decision_score(events, expected):
    actual = first_decision(events)
    if expected is None or actual is None:
        return None
    first = next(event for event in events if event.get("node") == "native"
                 and event.get("event") == "llm_done")
    if first.get("invalid_tool_calls") or first.get("finish_reason") == "length":
        return False
    if not actual:
        return expected.get("allow_text") is True
    return set(actual) <= set(expected.get("any_tools", []))


def timings(events):
    discovery = {event.get("call_id") for event in events
                 if event.get("event") == "load_domain"}
    business = [event for event in events if event.get("event") == "tool_requested"
                and event.get("call_id") not in discovery
                and not event.get("name", "").startswith("load_")]
    results = [event for event in events if event.get("event") == "call"
               and event.get("node") == "native"]
    ttft = [event["duration_s"] for event in events
            if event.get("event") == "model_first_token" and event.get("duration_s") is not None]
    return {"first_business_tool_s": business[0].get("t") if business else None,
            "first_business_result_s": results[0].get("t") if results else None,
            "model_ttft_s": ttft}


def decision_report(groups):
    scores = [all(group) if group and None not in group else None for group in groups]
    measured = [score for score in scores if score is not None]
    decisions = [score for group in groups for score in group if score is not None]
    return {"expected": len(groups), "measured": len(measured),
            "correct": proportion(sum(measured), len(measured)),
            "unverified": len(groups) - len(measured),
            "decision_rate": sum(decisions) / len(decisions) if decisions else None,
            "measured_decisions": len(decisions),
            "scope": "complete scenario: every initial decision correct; not task success"}
