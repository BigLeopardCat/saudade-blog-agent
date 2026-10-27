"""User-visible execution facts, discovery evidence and optional post-write narration."""

import pytest
from langchain_core.messages import AIMessage

from agent.domains.article_tags.preparation import change_summary
from agent.domains.articles.evidence import ArticleEvidence
from agent.products.blog import narration
from agent.products.blog.catalog import initial_tools
from agent.runtime.budget import BudgetLimits, RequestBudget, budget_scope
from agent.runtime.results import TurnResult
from tools.result import ok


def test_discovery_allows_real_candidate_links_without_forcing_a_body_read():
    evidence = ArticleEvidence()
    evidence.observe(
        "rag_search", {}, ok("candidate", data=[{"type": "note", "id": 22, "title": "Topic"}])
    )
    assert evidence.issue("[Topic](/article/22)", "https://example.invalid") is None
    assert evidence.issue("[invented](/article/99)", "https://example.invalid")
    assert evidence.issue("https://forged.invalid/article/22", "https://example.invalid")
    assert not evidence.documents


def test_device_action_is_visible_only_when_authorized_catalog_contains_it():
    action = object()
    assert initial_tools({}, {"devices": {"device_oled_display": action}}) == {
        "device_oled_display": action
    }
    assert initial_tools({}, {"devices": {}}) == {}


def test_tag_card_describes_only_the_change_without_internal_ids():
    def tag(ident, name):
        return {"id": ident, "name": name, "level": 1, "parentId": None, "color": None}

    plan = {
        "title": "我，管理员！",
        "before": [tag(46, "测试"), tag(47, "权限")],
        "after": [tag(46, "测试"), tag(47, "权限"), tag(None, "泠月喵")],
    }
    text = change_summary(plan)
    assert "添加标签：「泠月喵」" in text and "将新建一级标签：「泠月喵」" in text
    assert "#46" not in text and "权限" not in text and "→" not in text


def test_narration_is_tool_free_budgeted_and_keeps_execution_unchanged(monkeypatch):
    received = []
    execution = {"receipts": [{"tool": "set_article_tags", "result": "added"}]}
    result = TurnResult("已添加标签「泠月喵」。", execution)

    class Model:
        def invoke(self, prompt, **kwargs):
            received.extend(prompt)
            return AIMessage(
                content="加好啦喵～",
                usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
            )

    settings = []
    monkeypatch.setattr(narration, "get_llm", lambda **kwargs: settings.append(kwargs) or Model())
    budget = RequestBudget()
    with budget_scope(budget):
        assert narration.narrate(result, {}) == "加好啦喵～"
    assert budget.model_calls == 1 and budget.tokens == 25
    assert execution == result.execution and settings[0]["max_tokens"] == 192
    assert settings[0]["enable_thinking"] is False
    assert result.reply in received[-1].content
    assert "receipts" not in received[-1].content


def test_exhausted_narration_budget_preserves_committed_result_without_calling_model(monkeypatch):
    def unexpected(**kwargs):
        pytest.fail("model must not be created after budget refusal")

    monkeypatch.setattr(narration, "get_llm", unexpected)
    result = TurnResult("已经更新。", {"receipts": [{"tool": "fixture"}]})
    with budget_scope(RequestBudget(BudgetLimits(model_calls=0))):
        assert narration.narrate(result, {}) == result.reply


def test_narration_failure_does_not_change_successful_execution(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("fixture")

    monkeypatch.setattr(narration, "get_llm", fail)
    result = TurnResult("已经更新。", {"receipts": [{"tool": "fixture"}]})
    assert narration.narrate(result, {}) == result.reply


@pytest.mark.parametrize("response", [
    AIMessage(content='<function_call>{"name":"write","arguments":{}}</function_call>'),
    AIMessage(content="", response_metadata={"finish_reason": "content_filter"}),
    AIMessage(content="", additional_kwargs={"function_call": {
        "name": "write", "arguments": "{}"}}),
])
def test_invalid_narration_preserves_the_successful_receipt_without_retry(monkeypatch, response):
    calls = []

    class Model:
        def invoke(self, *args, **kwargs):
            calls.append(True)
            return response

    monkeypatch.setattr(narration, "get_llm", lambda **_: Model())
    result = TurnResult("已经更新。", {"receipts": [{"tool": "fixture"}]})
    assert narration.narrate(result, {}) == result.reply
    assert calls == [True] and result.execution == {"receipts": [{"tool": "fixture"}]}
