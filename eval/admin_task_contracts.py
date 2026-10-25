"""Task acceptance checks inspect signed intent and synthetic final business state."""

from agent import confirm
from eval.admin_task_fixture import backend

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


CASES = [("notice_then_role", backend, notice_then_role),
         ("notice_role_cancel", backend, notice_role_cancel)]
