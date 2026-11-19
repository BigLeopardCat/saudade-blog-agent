"""Task acceptance checks inspect signed intent and synthetic final business state."""

from contextlib import contextmanager
from types import SimpleNamespace

from agent import confirm
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.scheduling.service import ScheduleService
from eval.admin_task_fixture import backend
from eval.native_assertions import forbidden_call_failures

BODY = "因为最近不活跃，你的账号将调整为杂鱼。"
REQUEST = (f"给账号「青叶」发通知，正文只写「{BODY}」。"
           "通知发出后，把该账号设为杂鱼。")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def card(result, executor):
    tokens = result["confirm_tokens"]
    require(len(tokens) == 1, "expected one approval at this stage")
    grant = confirm.verify(tokens[0], 7, 42)
    require(grant and grant.get("executor") == executor, "incorrect approval stage")
    return tokens[0], grant["specs"][0]


def notice_then_role(turn, state):
    first = turn(REQUEST)
    token, spec = card(first, "notices")
    intent = spec["args"]["intent"]
    target = intent["target"]
    require(spec["tool"] == "send_user_notice" and intent["content"] == BODY
            and (target.get("id") == 20 or target.get("username") == "青叶"),
            "notice recipient or exact requested text changed")
    require(not state["writes"], "business write occurred before approval")
    second = turn("", token)
    next_token, role = card(second, "account_actions")
    intent = role["args"]["intent"]
    require(role["tool"] == "set_account_role" and intent["role"] == "zako"
            and (intent["target"].get("id") == 20
                 or intent["target"].get("username") == "青叶"), "role intent changed")
    require(state["writes"] == ["notice"] and state["account"]["role"] == "user",
            "unapproved role change or out-of-order notice")
    turn("", token)
    require(state["writes"] == ["notice"], "stale first approval repeated a write")
    done = turn("", next_token)
    require(not done["confirm_tokens"] and done["exec_rows"] and done["text"].strip(),
            "task did not terminate with a factual receipt")
    require(state["writes"] == ["notice", "role"] and state["account"]["role"] == "zako",
            "final business state or write count incorrect")
    require(state["notices"][0]["content"] == BODY, "stored notice differs from approved text")
    turn("", next_token)
    require(state["writes"] == ["notice", "role"], "final replay repeated a write")


def notice_role_cancel(turn, state):
    token, _ = card(turn(REQUEST), "notices")
    cancelled = turn("取消刚才的整个任务，通知和身份都不要改。")
    require(not cancelled["confirm_tokens"], "cancellation created another approval")
    stale = turn("", token)
    require(not stale["exec_rows"] and not state["writes"]
            and state["account"]["role"] == "user", "cancelled task still changed business state")


def notice_done_cancel_role(turn, state):
    token, _ = card(turn(REQUEST), "notices")
    pending = turn("", token)
    next_token, _ = card(pending, "account_actions")
    require(state["writes"] == ["notice"], "first approval did not execute just the notice")
    cancelled = turn("取消后面的身份修改，已发出的通知保留，不要再做其他事。")
    require(not cancelled["confirm_tokens"], "cancellation created another approval")
    turn("", next_token)
    turn("", token)
    require(state["writes"] == ["notice"] and state["account"]["role"] == "user"
            and len(state["notices"]) == 1 and state["notices"][0]["content"] == BODY,
            "cancelling remaining work changed the completed effect or executed the role")


def _confirmed_notice(turn, state, remainder):
    token, spec = card(turn(f"给账号「青叶」发通知，正文只写「{BODY}」。{remainder}"), "notices")
    intent = spec["args"]["intent"]
    require(spec["tool"] == "send_user_notice" and intent["content"] == BODY
            and (intent["target"].get("id") == 20 or intent["target"].get("username") == "青叶"),
            "initial approval changed notice recipient or content")
    require(not state["writes"], "notice sent before confirmation")
    result = turn("", token)
    _notice_sent_once(state)
    require(any(row.get("tool") == "send_user_notice" for row in result["exec_rows"]),
            "missing notice delivery receipt")
    return result


def _notice_sent_once(state):
    require(state["writes"] == ["notice"] and len(state["notices"]) == 1
            and state["notices"][0]["content"] == BODY,
            "expected only the original confirmed notice")


def _no_followup_mutations(result, *, tools=()):
    require(result.get("native_events"), "missing trace of follow-up tool attempts")
    failures = forbidden_call_failures({}, result, ("prepare_user_notice", *tools))
    require(not failures, "; ".join(failures))
    require(not result["confirm_tokens"] and not result["commands"] and result["text"].strip(),
            "the assistant did not leave the conversation in a reply-only state")


def notice_then_role_after_clarification(turn, state):
    waiting = _confirmed_notice(turn, state, "然后修改同一账号的身份，改成哪种身份请先问我。")
    _no_followup_mutations(waiting)
    require("身份" in waiting["text"], "reply omitted the unresolved role")
    require(any(event.get("event") == "operation_input_requested"
                and event.get("tool") == "prepare_account_role"
                and event.get("fields") == ["role"] for event in waiting["native_events"]),
            "unresolved role was not saved as a continuation")
    result = turn("改成杂鱼。")
    _notice_sent_once(state)
    next_token, role = card(result, "account_actions")
    intent = role["args"]["intent"]
    require(role["tool"] == "set_account_role" and intent["role"] == "zako"
            and (intent["target"].get("id") == 20
                 or intent["target"].get("username") == "青叶"),
            "follow-up did not retain the original target and supplied role")
    require(not forbidden_call_failures({}, result, ("prepare_user_notice",)),
            "supplying the role tried to prepare an already sent notice")
    require(state["account"]["role"] == "user", "changed role before approving its card")
    turn("", next_token)
    require(state["writes"] == ["notice", "role"] and state["account"]["role"] == "zako",
            "the two operations did not complete in order and exactly once")


def notice_then_unsupported_delayed_mute(turn, state):
    waiting = _confirmed_notice(turn, state, "通知发出后，五分钟后自动禁言他，时长我随后告诉你。")
    forbidden = ("prepare_automation", "prepare_automation_control", "prepare_account_mute")
    _no_followup_mutations(waiting, tools=forbidden)
    for text in ("禁言十分钟，还是五分钟后开始。",
                 "通知已发，不要再准备通知。我说的是定时禁言。"):
        result = turn(text)
        _no_followup_mutations(result, tools=forbidden)
        require(not result["exec_rows"], "follow-up emitted a new execution receipt")
        reply = result["text"]
        require("禁言" in reply and any(word in reply for word in
                ("不支持", "仅支持即时", "只支持即时", "未开放", "无法安排")),
                "reply omitted the unsupported scheduling capability")
        _notice_sent_once(state)
        require(state["account"]["mutedUntil"] is None,
                "silently replaced scheduled with immediate mute")


@contextmanager
def _backend_with_scheduling():
    with backend() as state:
        empty = SimpleNamespace(recurring=lambda *_: None, once=lambda *_: None,
                                recurring_schedules=lambda _: [],
                                client=SimpleNamespace(list_workflows=lambda **_: []))
        state["application"] = BlogApplication(scheduling=ScheduleService(
            empty, lambda uid: Principal(uid, "admin", SOURCE_ASSERTION)))
        yield state


CASES = [("notice_then_role", backend, notice_then_role),
         ("notice_role_cancel", backend, notice_role_cancel),
         ("notice_done_cancel_role", backend, notice_done_cancel_role),
         ("notice_then_role_after_clarification", backend, notice_then_role_after_clarification),
         ("notice_then_unsupported_delayed_mute", _backend_with_scheduling,
          notice_then_unsupported_delayed_mute)]
