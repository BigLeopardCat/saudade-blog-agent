"""Conversation memory and private execution plans use distinct framework state."""

import json

from _framework_fixture import call, model_replies
from _own_support import config
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.results import turn_phase
from agent.runtime.service import AgentRuntime
from eval.navigation_contracts import backend


def selected(model, saver):
    return AgentRuntime(CheckpointConversationStore(saver), application=BlogApplication(),
                        model_factory=lambda **_: model)


def request(runtime, message):
    return list(runtime.stream_events({"messages": [HumanMessage(content=message)]}, config()))


def test_remembering_order_takes_one_call_and_the_next_turn_can_read_the_second_article():
    order = "记住阅读顺序：文章22在前，文章23在后；现在不要读正文或概括。"
    acknowledgement = "记住了，先22后23；等你要求时再读。"
    saver = InMemorySaver()
    with backend([{"id": 22, "title": "第一篇"}, {"id": 23, "title": "第二篇"}]) as host, \
            model_replies(
                {"content": acknowledgement},
                {"tool_calls": [call("get_article_detail", "read-second",
                                     article_id=23, view="full_text")]},
                {"content": "第二篇介绍编程实践。[来源](/article/23)"},
            ) as (model, calls):
        runtime = selected(model, saver)
        first = request(runtime, order)
        assert len(calls) == 1 and not host["requests"]
        assert first[-1].reply == acknowledgement and turn_phase(first[-1]) == "responded"
        assert runtime.store.load(config()).messages[-1].content == acknowledgement
        second = request(runtime, "先读第二篇，告诉我一个核心观点，并附上链接。")
        assert len(calls) == 3 and len(host["requests"]) == 1
        assert host["requests"][0].url.path == "/api/public/documents/note/23/read"
        assert order in json.dumps(calls[1]["messages"], ensure_ascii=False)
        assert "/article/23" in second[-1].reply
        assert not host["unsupported_reads"]


def test_model_created_pending_todo_does_not_force_two_more_calls_or_duplicate_streamed_reply():
    pending = [{"content": "等用户要求时再阅读文章22、23", "status": "pending"}]
    acknowledgement = "顺序已记住，现在没有读取正文；等你要求时再读。"
    saver = InMemorySaver()
    with model_replies(
            {"tool_calls": [call("write_todos", "plan", todos=pending)]},
            {"content": acknowledgement},
    ) as (model, calls):
        runtime = selected(model, saver)
        events = request(runtime, "记住22在前、23在后，现在不要阅读。")
        assert len(calls) == 2
        assert events[-1].reply == acknowledgement
        assert "".join(event.content for event in events
                       if isinstance(event, AIMessageChunk)) == acknowledgement
        assert turn_phase(events[-1]) == "responded"
        assert runtime.store.load(config()).messages[-1].content == acknowledgement
        assert any(row.checkpoint["channel_values"].get("todos") == pending
                   for row in saver.list(None))
        assert all("Runtime completion review" not in json.dumps(call["messages"])
                   for call in calls)


def test_optional_planning_schema_and_instructions_load_only_when_requested():
    from agent.runtime.task_planning import SYSTEM_PROMPT

    pending = [{"content": name, "status": "pending"} for name in ("查询", "比较", "准备")]
    with model_replies(
            {"content": "你好。"},
            {"tool_calls": [call("load_task_planning_info_tools", "discover")]},
            {"tool_calls": [call("write_todos", "plan", todos=pending)]},
            {"content": "清单已记录，尚未执行。"},
    ) as (model, calls):
        runtime = selected(model, InMemorySaver())
        request(runtime, "你好")
        request(runtime, "为查询、比较、准备三个步骤列执行清单，先不执行。")
        def names(row):
            return {item["function"]["name"] for item in row["tools"]}

        assert "write_todos" not in names(calls[0])
        assert "load_task_planning_info_tools" in names(calls[0])
        assert SYSTEM_PROMPT not in str(calls[0]["messages"])
        assert "write_todos" in names(calls[2])
        assert SYSTEM_PROMPT in str(calls[2]["messages"])
        assert len(calls) == 4
