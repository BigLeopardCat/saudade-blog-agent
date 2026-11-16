"""Deliver complete sources or bounded synthesis without losing coverage evidence."""

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


def test_complete_source_within_observation_budget_needs_no_internal_model(source):
    state, model = source
    budget = TaskBudget()
    result = invoke(budget)
    assert result.kind == "ok" and result.data["view"] == "body"
    assert result.data["noteContent"] == state["body"]
    assert model.prompts == [] and budget.model_calls == budget.tokens == 0
    assert len(state["requests"]) == 1
    assert result.data["readComplete"] and result.data["readOffset"] == 0
    assert result.data["source"]["end"] == result.data["totalChars"] == len(state["body"])


def test_source_above_observation_budget_uses_standard_synthesis(source, monkeypatch):
    state, model = source
    state["body"] = "正文材料。" * 2000
    monkeypatch.setattr(settings, "agent_tool_result_tokens", 1500)
    result = invoke()
    assert result.kind == "ok" and result.data["strategy"] == "stuff"
    assert len(model.prompts) == 1 and len(state["requests"]) == 1
    assert result.data["coverage"]["end"] == len(state["body"])


def test_native_full_source_uses_two_model_requests(source, tmp_path, monkeypatch):
    from _framework_fixture import call, model_replies

    from agent.runtime.message_content import estimate_tokens
    from eval.golden_evaluation import check_gold
    from utils import trace

    state, summarizer = source
    state["body"] = "完整原文包含开篇论点与末尾限制。" * 500
    recorder = trace.start_trace("source-delivery", 7, "test", dir=str(tmp_path))
    try:
        with model_replies({"tool_calls": [call("get_article_detail", **ARGS)]},
                           {"content": "完整总结。[Source](/article/19)"}) as (model, requests):
            monkeypatch.setattr(type(model), "get_num_tokens_from_messages",
                                lambda _self, messages, **_kwargs: estimate_tokens(messages))
            tools = {notes.get_article_detail.name: notes.get_article_detail}
            session = ToolSession(model, {}, tools, {}, middleware=(ArticleMiddleware,))
            result = session.run([HumanMessage("总结全文")])
            assert len(requests) == 2 and summarizer.prompts == []
            message = next(m for m in requests[-1]["messages"] if m["role"] == "tool")
            data = json.loads(message["content"])
            assert data["noteContent"] == state["body"]
            assert data["readComplete"] and data["sourceUrl"].endswith("/article/19")
            assert check_gold({"require_body_reads": [{"article_id": ARTICLE}],
                               "max_article_body_chars": len(state["body"])}, {
                "text": result.reply, "commands": [], "tool_calls": [], "exec_rows": [],
                "native_events": recorder.events}) == []
            failures = check_gold({"max_article_body_chars": len(state["body"]) - 1}, {
                "text": result.reply, "commands": [], "tool_calls": [], "exec_rows": [],
                "native_events": recorder.events})
            assert any("正文读量超过场景验收上限" in item for item in failures)
            reads = [e for e in recorder.events if e.get("event") == "article_observation"]
            assert reads[-1]["operation"] == "body"
            assert reads[-1]["returned_chars"] == len(state["body"])
    finally:
        trace.finish_trace("source-delivery", "test", 0.0)


@pytest.mark.parametrize("field,value", [
    ("readComplete", False), ("totalChars", 999), ("contentRevision", "a" * 64),
    ("sectionSchema", "unknown"), ("selectedScope", "section_subtree"),
    ("noteContent", ["invalid"]), ("noteId", 20),
])
def test_summary_source_rejects_incomplete_or_inconsistent_frames(source, field, value):
    from langchain_core.messages import ToolMessage

    result = invoke()
    data = {**result.data, field: value}
    session = ToolSession(None, {}, {}, {})
    middleware = ArticleMiddleware(session)
    message = ToolMessage(content=str(result), tool_call_id="summary",
                          artifact={"kind": "ok", "data": data})
    rendered = middleware.render_result({"name": "get_article_detail", "args": ARGS,
                                        "id": "summary"}, message, 100000)
    assert rendered.status == "error" and rendered.artifact["data"] is None
    assert middleware.evidence.summary_failures == {ARTICLE}


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
    state, model = source
    state["body"] = "完整材料。" * 7000
    model.finish, model.text = finish, text
    budget = TaskBudget()
    result = invoke(budget)
    assert result.kind == "unavailable" and result.data is None
    assert budget.model_calls == 1 and budget.tokens == 112


@pytest.mark.parametrize("receipt,finish,accepted", [
    (200, "stop", True), (257, "stop", False), (200, "length", False),
    (None, "stop", False), (0, "stop", False),
])
def test_summary_output_validation_uses_actual_usage_before_conservative_estimates(
    source, monkeypatch, receipt, finish, accepted
):
    state, model = source
    state["body"] = "完整材料。" * 7000
    from agent.runtime.message_content import estimate_tokens

    text = " documentation" * 100
    assert estimate_tokens([HumanMessage(content=text)]) > settings.agent_summary_output_tokens
    original = model.invoke

    def answer(messages, **kwargs):
        response = original(messages, **kwargs)
        return response.model_copy(update={"content": text,
            "response_metadata": {"finish_reason": finish},
            "usage_metadata": None if receipt is None else {
                "input_tokens": 100, "output_tokens": receipt, "total_tokens": 100 + receipt}})

    monkeypatch.setattr(model, "invoke", answer)
    result = invoke()
    assert (result.kind == "ok") is accepted
    assert len(model.prompts) == 1


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
    state, _ = source
    state["body"] = "完整材料。" * 7000
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


@pytest.mark.parametrize("read_path", ["complete", "gap", "revision", "section"])
def test_failed_summary_requires_complete_same_revision_document_read_to_recover(source, read_path):
    state, summarizer = source
    state["body"] = "# Section\n" + "完整正文材料。" * 100
    evidence = ArticleEvidence()
    evidence.observe("get_article_detail", ARGS, unavailable("incomplete"))

    def read(selection):
        args = {"article_id": ARTICLE, "selection": selection}
        result = notes.get_article_detail.invoke(args)
        assert result.kind == "ok"
        evidence.observe("get_article_detail", args, result)
        return result.data

    page = read({"kind": "full_text", "max_chars": 100})
    assert evidence.summary_failures == {ARTICLE}
    if read_path == "revision":
        state["body"] = state["body"][:-1] + "改"
        page = read({"kind": "metadata"})
    locator = {"revision": page["contentRevision"], "section_schema": page["sectionSchema"]}
    selection = ({"section": "s1"} if read_path == "section" else
                 {"offset": 101 if read_path == "gap" else 100})
    read({"kind": "source_range", **selection, **locator})
    restored = ArticleEvidence.restore(json.loads(json.dumps(evidence.snapshot())))
    issue = restored.issue("依据原文总结。[Source](/article/19)", "https://example.test")
    assert issue == (None if read_path == "complete" else "article_summary_incomplete")
    assert not summarizer.prompts and not restored.summaries


def test_native_tool_loop_recovers_after_truncated_summary_by_reading_the_whole_source(source):
    state, summarizer = source
    state["body"] = "完整材料。" * 7000
    summarizer.finish = "length"

    class AgentModel:
        max_tokens = 256

        def __init__(self):
            self.prompts = []

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt, **kwargs):
            self.prompts.append(prompt)
            if len(self.prompts) < 4:
                selection = ({"kind": "full_text"} if len(self.prompts) == 2 else
                             {"kind": "source_range", "offset": 32768})
                args = ARGS if len(self.prompts) == 1 else {
                    "article_id": ARTICLE, "selection": selection}
                return AIMessage(content="", tool_calls=[{
                    "name": "get_article_detail", "args": args,
                    "id": f"read-{len(self.prompts)}"}])
            return AIMessage(content="依据读完的原文总结。[Source](/article/19)")

    model = AgentModel()
    session = ToolSession(model, {}, {notes.get_article_detail.name: notes.get_article_detail},
                          {}, middleware=(ArticleMiddleware,))
    result = session.run([HumanMessage("总结全文")])
    assert result.reply == "依据读完的原文总结。[Source](/article/19)"
    assert len(model.prompts) == 4 and len(summarizer.prompts) == 1


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

    state, _ = source
    state["body"] = "完整材料。" * 7000
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
            *["source_page"] * result.data["coverage"]["pages"],
            "llm_start", "llm_done", "completed"]
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
