"""Article observations survive their own task, without becoming unrelated answer sources."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from agent.domains.articles.evidence import ArticleEvidence
from agent.products.blog.articles import ArticleMiddleware
from tools.result import ok


def saved():
    evidence = ArticleEvidence()
    evidence.observe("get_article_detail", {"article_id": 19}, ok("read", data={
        "noteId": 19, "noteTitle": "Architecture", "noteContent": "Original evidence"}))
    return evidence.snapshot()


@pytest.mark.parametrize("owner,expected", [("original", {19}), ("previous", set()),
                                           (None, set())])
def test_recovery_requires_evidence_from_the_same_task(owner, expected):
    state = {"evidence": saved(), "evidence_task_id": owner, "messages": [ToolMessage(
        content="read", tool_call_id="read", artifact={"article_evidence": saved(),
            "article_evidence_task_id": owner})]}
    session = SimpleNamespace(task={"id": "original"}, recovering=True,
        recovery_state=state, render_result=lambda *args: None)
    middleware = ArticleMiddleware(session)
    middleware.before_model(state, None)
    assert set(middleware.evidence.documents) == expected
    assert session.suppress_output is bool(expected)


def test_new_task_ignores_unrelated_confirmation_evidence():
    session = SimpleNamespace(task={"id": "image-request"}, recovering=False,
        render_result=lambda *args: None)
    middleware = ArticleMiddleware(session)
    update = middleware.before_agent({"evidence": saved(), "evidence_task_id": "article-read",
                                      "proposal": {"pending": True}}, None)
    assert not update["evidence"]["documents"]
    assert update["evidence_task_id"] == "image-request"
