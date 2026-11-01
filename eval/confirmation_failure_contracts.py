"""A real model must inspect partial business state after an unverified mutation."""

from contextlib import contextmanager

from agent import confirm
from eval.admin_task_contracts import require
from eval.own_fixtures import notification_backend


@contextmanager
def partial_backend():
    with notification_backend() as state:
        state["ignore"] = {102}
        yield state


def partial_read(turn, state):
    prepared = turn("把我的全部未读通知标为已读，然后查询核对哪些成功、哪些还没成功。")
    require(len(prepared["confirm_tokens"]) == 1, "missing fixed-scope approval")
    token = prepared["confirm_tokens"][0]
    grant = confirm.verify(token, 7, 42)
    require(grant["specs"] == [{"tool": "read_notifications", "args": {
        "ids": [101, 102], "all": False}}], "incorrect partial-mutation scope")
    done = turn("", token)
    require(not done["exec_rows"] and done["text"].strip(),
            "unverified mutation produced success evidence or no answer")
    writes = [i for i, request in enumerate(state["requests"]) if request.method == "POST"]
    require(len(writes) == 1, "partial mutation was repeated")
    readbacks = [request for request in state["requests"][writes[0] + 1:]
                if request.method == "GET" and request.url.path.endswith("/notifications")]
    require(len(readbacks) >= 2,
            "Agent did not independently read after domain verification failed")
    actual = {row["id"]: row["isRead"] for row in state["items"]}
    require(actual == {101: True, 102: False, 103: True}, "incorrect partial business effect")
    if done["confirm_tokens"]:
        require(len(done["confirm_tokens"]) == 1, "multiple retry approvals")
        retry = confirm.verify(done["confirm_tokens"][0], 7, 42)
        require(retry and retry["jti"] != grant["jti"] and retry["specs"] == [{
            "tool": "read_notifications", "args": {"ids": [102], "all": False}}],
            "new approval included completed or unobserved targets")
    count = len(state["requests"])
    turn("", token)
    require(len(state["requests"]) == count, "old grant retried the unknown mutation")
    if done["confirm_tokens"]:
        cancelled = turn("剩下的不用再重试了，取消新确认卡，保留现在的已读状态。")
        require(not cancelled["confirm_tokens"], "retry cancellation created a card")
        turn("", done["confirm_tokens"][0])
        require(sum(request.method == "POST" for request in state["requests"]) == 1,
                "cancelled remainder executed")
    state["contract_evidence"] = {"read": [101, 103], "unread": [102], "writes": 1}


CASES = [("notification_partial_readback", partial_backend, partial_read)]
