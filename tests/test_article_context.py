"""Deduplicate source bodies without changing native exchanges, history or evidence."""

import copy
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.products.blog.article_context import source_references
from agent.products.blog.articles import ArticleMiddleware
from agent.runtime.session import ToolSession
from eval.fixtures.document_reads import document_page
from rag.reading import article_frame_text
from rag.sections import PARSER_VERSION
from tools import notes

BODY = "完整的源材料保留正文与限制，不重复交付。" * 100


def body(ident, **changes):
    source = {"type": "note", "id": 19, "revision": "a" * 64,
              "section_schema": PARSER_VERSION, "section_id": "", "offset": 0,
              "end": len(BODY)}
    data = {"view": "body", "noteId": 19, "noteContent": BODY,
            "source": source, "contentRevision": source["revision"],
            "sectionSchema": PARSER_VERSION, **changes}
    return ToolMessage(content=article_frame_text(data), name="get_article_detail",
                       tool_call_id=ident, id="message-" + ident,
                       artifact={"kind": "ok", "data": copy.deepcopy(data)},
                       additional_kwargs={"provider_metadata": "preserved"})


def batch(*identifiers):
    return AIMessage(content="", tool_calls=[{"id": ident, "name": "get_article_detail",
                                              "args": {}} for ident in identifiers])


def test_same_batch_references_exact_body_without_mutating_checkpoint_material():
    messages = [HumanMessage("Read"), batch("a", "b"), body("a"), body("b")]
    frozen = copy.deepcopy(messages)
    output = source_references(messages)
    assert messages == frozen and output[:-1] == messages[:-1]
    data = json.loads(output[-1].content)
    assert data["view"] == "source_reference" and data["body_in_tool_call"] == "a"
    assert output[-1].tool_call_id == "b" and output[-1].id == "message-b"
    assert output[-1].artifact == messages[-1].artifact
    assert output[-1].additional_kwargs == messages[-1].additional_kwargs
    assert BODY not in output[-1].content


def test_new_user_turn_keeps_the_previous_model_prefix_identical():
    messages = [HumanMessage("Read"), batch("a", "b"), body("a"), body("b")]
    previous = source_references(messages)
    assert source_references([*messages, AIMessage("Answer"), HumanMessage("Continue")])[
        :len(messages)] == previous


def test_new_batch_and_missing_original_body_do_not_produce_dangling_references():
    messages = [batch("a"), body("a"), AIMessage("Read again"), batch("b"), body("b")]
    assert source_references(messages) == messages
    remaining = [batch("b"), body("b")]
    assert source_references(remaining) == remaining
    unpaired = [body("a"), body("b")]
    assert source_references(unpaired) == unpaired


@pytest.mark.parametrize("change", [
    {"contentRevision": "b" * 64}, {"noteId": 20}, {"sectionSchema": "unknown"},
    {"noteContent": "different"}, {"view": "summary"}, {"source": None},
])
def test_inconsistent_or_different_sources_are_not_reused(change):
    messages = [batch("a", "b"), body("a"), body("b", **change)]
    assert source_references(messages) == messages


def test_different_revisions_and_range_spaces_are_not_reused():
    for field, value in (("revision", "b" * 64), ("section_id", "s1")):
        second = body("b")
        data = json.loads(second.content)
        data["source"][field] = value
        if field == "revision":
            data["contentRevision"] = value
        second.content = article_frame_text(data)
        messages = [batch("a", "b"), body("a"), second]
        assert source_references(messages) == messages


def test_budget_and_sdk_receive_one_body_for_parallel_full_read_and_summary(monkeypatch):
    from _framework_fixture import call, model_replies

    def get(path, **kwargs):
        query = {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}
        return document_page("note", 19, "Source", BODY, query)

    monkeypatch.setattr(notes, "_get", get)
    with model_replies({"tool_calls": [
        call("get_article_detail", ident="a", article_id=19, selection={"kind": "full_text"}),
        call("get_article_detail", ident="b", article_id=19, selection={"kind": "summary"})
    ]}, {"content": "Summary. [Source](/article/19)"}) as (model, requests):
        session = ToolSession(model, {}, {notes.get_article_detail.name: notes.get_article_detail},
                              {}, middleware=(ArticleMiddleware,))
        reserved, original = [], session.budget.reserve

        def reserve(messages, *args, **kwargs):
            reserved.append(copy.deepcopy(messages))
            return original(messages, *args, **kwargs)

        monkeypatch.setattr(session.budget, "reserve", reserve)
        result = session.run([HumanMessage("Read and summarize the article")])
        assert result.reply.startswith("Summary.") and len(requests) == 2
        wire = [m for m in requests[-1]["messages"] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in wire] == ["a", "b"]
        data = json.loads(wire[0]["content"])
        assert data["noteContent"] == BODY
        assert list(data).index("sourceUrl") < list(data).index("noteContent")
        assert json.loads(wire[1]["content"])["body_in_tool_call"] == "a"
        budget_tools = [m for m in reserved[-1] if isinstance(m, ToolMessage)]
        assert [m.content for m in budget_tools] == [m["content"] for m in wire]


def test_checkpoint_retains_raw_sources_and_followup_keeps_the_same_model_view(monkeypatch):
    from _framework_fixture import call, model_replies
    from _memory_support import config
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.products.blog.application import BlogApplication
    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime

    def get(path, **kwargs):
        query = {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}
        return document_page("note", 19, "Source", BODY, query)

    monkeypatch.setattr(notes, "_get", get)
    with model_replies({"tool_calls": [
        call("get_article_detail", ident="a", article_id=19, selection={"kind": "full_text"}),
        call("get_article_detail", ident="b", article_id=19, selection={"kind": "summary"})
    ]}, {"content": "Summary. [Source](/article/19)"},
         {"content": "Follow-up. [Source](/article/19)"}) as (model, requests):
        store = CheckpointConversationStore(InMemorySaver())
        runtime = AgentRuntime(store, application=BlogApplication(),
                               model_factory=lambda **_: model)
        list(runtime.stream_events({"messages": [HumanMessage("Read and summarize")]}, config()))
        saved = [m for m in store.load(config()).messages if isinstance(m, ToolMessage)]
        assert len(saved) == 2
        assert all(json.loads(m.content)["noteContent"] == BODY for m in saved)
        first = [m for m in requests[-1]["messages"] if m["role"] == "tool"]
        list(runtime.stream_events({"messages": [HumanMessage("Continue")]}, config()))
        second = [m for m in requests[-1]["messages"] if m["role"] == "tool"]
        assert len(requests) == 3 and first == second
