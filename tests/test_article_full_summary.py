"""Whole-source synthesis must cover every page without depending on chat compaction."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.domains.articles.evidence import ArticleEvidence
from agent.domains.articles.summary import summarize_article
from agent.products.blog.articles import ArticleMiddleware
from agent.runtime.budget import BudgetLimits, TaskBudget, budget_scope
from agent.runtime.session import ToolSession
from agent.runtime.turns import AgentCancelled
from config.settings import settings
from eval.fixtures.document_reads import document_page
from rag.reading import read_observation
from tools import notes
from tools.result import ok, unavailable

ARTICLE = 19
ARGS = {"article_id": ARTICLE, "selection": {"kind": "summary", "instructions": "总结全部论点"}}


class SummaryModel:
    def __init__(self, *, finish="stop", text="连贯的全文总结。"):
        self.prompts = []
        self.finish = finish
        self.text = text

    def invoke(self, messages, **kwargs):
        self.prompts.append(messages)
        return AIMessage(content=self.text, response_metadata={"finish_reason": self.finish},
                         usage_metadata={"input_tokens": 100, "output_tokens": 12,
                                         "total_tokens": 112})


@pytest.fixture
def source(monkeypatch):
    state = {"body": "开篇论点。\n\n末尾结论。", "requests": [], "fail_at": None}
    model = SummaryModel()

    def get(path, **kwargs):
        parsed = urlsplit(path)
        assert parsed.path == f"/documents/note/{ARTICLE}/read"
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        state["requests"].append(query)
        if state["fail_at"] == len(state["requests"]):
            return unavailable("Changed revision", meta={"status": 409})
        return document_page("note", ARTICLE, "Source", state["body"], query)

    monkeypatch.setattr(notes, "_get", get)
    monkeypatch.setattr("models.get_llm", lambda **kwargs: model)
    monkeypatch.setattr(settings, "agent_summary_output_tokens", 256)
    return state, model


def invoke(budget=None):
    with budget_scope(budget or TaskBudget()):
        return notes.get_article_detail.invoke(ARGS)


def test_short_article_uses_one_source_pass_and_one_model_request(source):
    state, model = source
    budget = TaskBudget()
    result = invoke(budget)
    assert result.kind == "ok" and result.data["strategy"] == "stuff"
    assert state["body"] in model.prompts[0][-1].content
    assert len(model.prompts) == budget.model_calls == 1
    assert budget.tokens == 112
    assert len(state["requests"]) == 1
    coverage = result.data["coverage"]
    assert coverage["complete"] and coverage["start"] == 0
    assert coverage["end"] == coverage["total_chars"] == len(state["body"])
    assert "summary" not in result.data["coverage"]


def test_multi_page_source_is_joined_in_order_with_one_bound_revision(source):
    state, model = source
    state["body"] = "文首。" + "中间材料。" * 15000 + "末尾改变前文条件。"
    result = invoke()
    assert result.kind == "ok" and result.data["coverage"]["pages"] >= 2
    assert state["body"] in str(model.prompts[0])
    assert [int(row["offset"]) for row in state["requests"]] == [0, 32768, 65536]
    assert all(row["revision"] == result.data["coverage"]["revision"]
               for row in state["requests"][1:])
    assert all(row["section_schema"] == result.data["coverage"]["section_schema"]
               for row in state["requests"][1:])


def test_refine_processes_all_source_ranges_and_keeps_previous_summary(source):
    state, model = source
    state["body"] = "\n\n".join(f"章节 {i}：事实与限制 {i}。" * 20 for i in range(45))
    budget = TaskBudget(BudgetLimits(context_tokens=2800, input_tokens=2800,
                                   model_calls=30, task_tokens=100000))
    result = invoke(budget)
    assert result.kind == "ok" and result.data["strategy"] == "refine"
    assert len(model.prompts) == result.data["segments"] > 1
    assert all("先前摘要：\n连贯的全文总结。" in prompt[-1].content
               for prompt in model.prompts[1:])
    for i in range(45):
        assert any(f"事实与限制 {i}。" in prompt[-1].content for prompt in model.prompts)
    assert budget.model_calls == len(model.prompts)
    assert result.data["coverage"]["end"] == len(state["body"])


@pytest.mark.parametrize("failure", ["changed", "gap", "total", "section"])
def test_no_summary_is_published_for_failed_or_inconsistent_continuation(source, failure):
    state, model = source
    state["body"] = "全文材料。" * 9000
    if failure == "changed":
        state["fail_at"] = 2
        result = invoke()
    else:
        count = 0

        def read(identifier, **kwargs):
            nonlocal count
            count += 1
            data = document_page("note", identifier, "Source", state["body"], {
                "view": "body", "offset": kwargs["offset"], "limit": kwargs["max_chars"],
                "revision": kwargs["revision"], "section_schema": kwargs["section_schema"]})
            from agent.domains.articles.reading import project

            data = project(data, "note", identifier, "", kwargs["offset"],
                           kwargs["max_chars"], kwargs["revision"], kwargs["section_schema"])
            if count == 2:
                if failure == "gap":
                    data["source"]["offset"] += 1
                elif failure == "total":
                    data["totalChars"] += 1
                else:
                    data["selectedScope"] = "section_subtree"
            return ok("page", data=data)

        result = summarize_article(ARTICLE, "总结", {}, read=read)
    assert result.kind == "unavailable" and "尚未完成" in str(result)
    assert result.data is None and model.prompts == []


@pytest.mark.parametrize("finish,text", [("length", "被截断的摘要"), ("stop", "")])
def test_truncated_or_empty_model_output_is_not_complete_evidence(source, finish, text):
    _, model = source
    model.finish, model.text = finish, text
    budget = TaskBudget()
    result = invoke(budget)
    assert result.kind == "unavailable" and result.data is None
    assert budget.model_calls == 1 and budget.tokens == 112


def test_budget_exhaustion_does_not_publish_an_intermediate_refinement(source):
    state, model = source
    state["body"] = "章节材料。\n" * 6000
    budget = TaskBudget(BudgetLimits(context_tokens=2800, input_tokens=2800,
                                   model_calls=1, task_tokens=100000))
    result = invoke(budget)
    assert result.kind == "unavailable" and result.meta["reason"] == "model_calls"
    assert model.prompts == []


def test_cancellation_propagates_instead_of_returning_a_complete_summary(source, monkeypatch):
    _, model = source

    def cancelled(config):
        raise AgentCancelled()

    monkeypatch.setattr("agent.domains.articles.summary.check_cancelled", cancelled)
    with pytest.raises(AgentCancelled):
        invoke()
    assert model.prompts == []


def test_summary_has_separate_provenance_and_failed_summary_cannot_be_replaced_by_passages(source):
    result = invoke()
    evidence = ArticleEvidence()
    evidence.observe("get_article_detail", ARGS, result)
    assert evidence.documents == {} and evidence.read_ranges == {}
    assert evidence.summaries[ARTICLE]["coverage"]["complete"]
    assert evidence.issue("[Source](/article/19)", "https://example.test") is None
    restored = ArticleEvidence.restore(json.loads(json.dumps(evidence.snapshot())))
    assert restored.summaries == evidence.summaries
    restored.observe("get_article_detail", ARGS, unavailable("incomplete"))
    restored.documents[ARTICLE] = "A previously read passage"
    assert restored.issue("根据片段得出全文结论", "https://example.test") == (
        "article_summary_incomplete")


@pytest.mark.parametrize("failed", [False, True])
def test_toolnode_workflow_delivers_only_a_complete_summary_or_explicit_failure(source, failed):
    state, summarizer = source
    state["body"] = "全文材料。" * 9000
    if failed:
        state["fail_at"] = 2

    class AgentModel:
        max_tokens = 256

        def __init__(self):
            self.prompts = []

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt, **kwargs):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return AIMessage(content="", tool_calls=[{
                    "name": "get_article_detail", "args": ARGS, "id": "summary-call"}])
            return AIMessage(content="完整总结。[Source](/article/19)")

    model = AgentModel()
    session = ToolSession(model, {}, {notes.get_article_detail.name: notes.get_article_detail},
                          {}, middleware=(ArticleMiddleware,))
    result = session.run([HumanMessage("总结全文")])
    assert len(model.prompts) == 2
    message = next(m for m in model.prompts[-1] if m.type == "tool")
    observation = read_observation({"name": "get_article_detail", "args": ARGS,
                                    "id": "summary-call"}, message)
    assert observation["operation"] == "summary" and observation["ranges"] == []
    if failed:
        assert "全文总结尚未完成" in result.reply and summarizer.prompts == []
        assert message.status == "error"
    else:
        assert "全文材料。" not in message.content
        assert json.loads(message.content)["coverage"]["complete"]
        assert message.artifact["article_evidence"]["summaries"][ARTICLE]["coverage"]["complete"]


def test_summary_presentation_is_all_or_nothing(source):
    result = invoke()
    session = ToolSession(None, {}, {}, {})
    middleware = ArticleMiddleware(session)
    from langchain_core.messages import ToolMessage

    raw = ToolMessage(content=str(result), tool_call_id="summary", artifact={
        "kind": "ok", "data": result.data})
    rendered = middleware.render_result({"name": "get_article_detail", "args": ARGS,
                                        "id": "summary"}, raw, 20)
    assert rendered.status == "error" and rendered.artifact["data"] is None
    assert middleware.evidence.summary_failures == {ARTICLE}


def test_standard_model_callbacks_count_internal_usage_once(source, monkeypatch, tmp_path):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

    from agent.runtime.model_budget import ModelBudgetHandler
    from eval import usage
    from utils import trace

    budget = TaskBudget()
    model = FakeMessagesListChatModel(responses=[AIMessage(content="摘要", usage_metadata={
        "input_tokens": 100, "output_tokens": 12, "total_tokens": 112})],
        callbacks=[ModelBudgetHandler(budget)])
    monkeypatch.setattr("models.get_llm", lambda **kwargs: model)
    recorder = trace.start_trace("summary-usage", 7, "test", dir=str(tmp_path))
    try:
        result = invoke(budget)
        measured = usage.totals(recorder.events)
        assert result.kind == "ok"
        assert budget.model_calls == measured["calls"] == 1
        assert budget.tokens == 112 and measured["in"] == 100 and measured["out"] == 12
        assert [e["event"] for e in recorder.events if e["node"] == "article_summary"] == [
            "source_page", "llm_start", "llm_done", "completed"]
    finally:
        trace.finish_trace("summary-usage", "test", 0.0)


def test_cancel_after_partial_refinement_does_not_publish_partial_summary(source):
    state, model = source
    state["body"] = "\n\n".join(f"章节 {i}：论点 {i}。" * 30 for i in range(20))
    original = model.invoke

    def interrupted(*args, **kwargs):
        if model.prompts:
            raise AgentCancelled()
        return original(*args, **kwargs)

    model.invoke = interrupted
    budget = TaskBudget(BudgetLimits(context_tokens=2800, input_tokens=2800,
                                   model_calls=30, task_tokens=100000))
    with pytest.raises(AgentCancelled):
        invoke(budget)
    assert len(model.prompts) == 1


def test_full_summary_acceptance_requires_the_summary_workflow(source):
    from eval.golden_evaluation import check_gold
    from eval.online_baseline import load_suite

    cases = load_suite("eval/golden/retrieval_requests.jsonl")
    case = next(row for row in cases if row["id"] == "retrieval_whole_article_summary")
    result = {"text": "完整总结。[Source](/article/22)", "tool_calls": [], "commands": [],
              "exec_rows": [], "native_events": [{"node": "native", "event": "call",
                  "name": "get_article_detail", "kind": "ok", "args": {
                      "article_id": 22, "selection": {"kind": "full_text"}}}]}
    assert check_gold(case["gold"], result)
    result["native_events"][0]["args"]["selection"]["kind"] = "summary"
    assert not check_gold(case["gold"], result)


def test_delivered_summary_survives_compaction_with_its_complete_tool_batch(source):
    from langchain_core.messages import ToolMessage

    from agent.runtime.compaction import _protected

    result = invoke()
    session = ToolSession(None, {}, {}, {})
    middleware = ArticleMiddleware(session)
    message = middleware.render_result({"name": "get_article_detail", "args": ARGS,
        "id": "summary"}, ToolMessage(content=str(result), id="summary-result",
            tool_call_id="summary", artifact={"kind": "ok", "data": result.data}), 20000)
    messages = [HumanMessage("总结全文", id="task"),
        AIMessage("", id="summary-request", tool_calls=[
            {"name": "get_article_detail", "args": ARGS, "id": "summary"},
            {"name": "get_article_stats", "args": {"article_id": ARTICLE}, "id": "stats"}]),
        message, ToolMessage("counts", id="stats-result", tool_call_id="stats"),
        AIMessage("", id="later-request", tool_calls=[
            {"name": "get_blog_info", "args": {}, "id": "later"}]),
        ToolMessage("site", id="later-result", tool_call_id="later")]
    _, protected = _protected(messages, "task")
    assert {"summary-request", "summary-result", "stats-result"} <= protected
    messages.append(HumanMessage("新的无关任务", id="new-task"))
    _, protected = _protected(messages, "new-task")
    assert not {"summary-request", "summary-result", "stats-result"} & protected
