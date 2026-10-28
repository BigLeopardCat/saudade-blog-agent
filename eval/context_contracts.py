"""Multi-turn task outcomes with verifiable references and source reuse."""

from eval.own_fixtures import message_backend
from utils.trace_io import load_trace


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def mailbox_short_followup(turn, state):
    state["items"][0]["content"] = "取件码是482619，有效期到周六。"
    listed = turn("先只列出我最近收到的站内信标题，最后问我要不要读第一封。"
                  "先别读取正文，不标记已读。")
    _require("欢迎来信" in listed["text"], "mailbox list omitted the first message")
    _require(not any(request.url.path.rsplit("/", 1)[-1].isdigit()
                     for request in state["requests"]), "list request read a message body")
    before = len(state["requests"])
    detail = turn("嗯")
    reads = state["requests"][before:]
    _require(len(reads) == 1 and reads[0].method == "GET"
             and reads[0].url.path.endswith("/messages/201"),
             "short reply did not read exactly the previously offered first message")
    _require("482619" in detail["text"], "short reply did not return the source content")
    before = len(state["requests"])
    recalled = turn("不用再查，刚才取件码是多少？")
    _require("482619" in recalled["text"], "follow-up lost the recent tool fact")
    _require(len(state["requests"]) == before, "follow-up repeated a completed read")
    trace = load_trace(recalled["trace"])
    _require(trace and sum(event.get("node") == "native" and event.get("event") == "llm_done"
                           for event in trace["events"]) == 1,
             "source reuse did not finish in one model call")
    _require(not any(result["confirm_tokens"] or result["exec_rows"]
                     for result in (listed, detail, recalled)),
             "read-only dialogue prepared or executed a write")
    _require(all(request.method == "GET" for request in state["requests"]),
             "read-only dialogue changed business data")


CASES = [("mailbox_short_followup", message_backend, mailbox_short_followup)]
