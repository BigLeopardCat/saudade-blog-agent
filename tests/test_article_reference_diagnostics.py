"""Rejected source diagnostics retain the evidence boundary without leaking URLs."""

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.tools import tool

from agent.domains.articles.evidence import ArticleEvidence
from agent.products.blog import articles
from agent.runtime.session import ToolSession
from tools.base import SITE_BASE
from tools.result import ok


@pytest.mark.parametrize("reply,expected", [
    ("[Known](/article/1)", []),
    ("[Unknown](/article/99)", [{"article_id": 99, "same_site": True, "known_id": False}]),
    ("https://user:secret@foreign.invalid/article/1?token=private", [
        {"article_id": 1, "same_site": False, "known_id": True}]),
])
def test_source_diagnostics_distinguish_unknown_target_from_foreign_origin(reply, expected):
    evidence = ArticleEvidence()
    evidence.documents[1] = "Known"
    details = evidence.invalid_references(reply, SITE_BASE)
    assert details == expected
    assert "secret" not in str(details) and "private" not in str(details)
    assert bool(evidence.issue(reply, SITE_BASE)) == bool(expected)


def test_rejected_answer_trace_is_bounded_and_does_not_expose_the_answer(monkeypatch):
    records = []
    monkeypatch.setattr(articles, "record", lambda node, event, **data: records.append(data))

    @tool
    def get_article_detail(article_id: int) -> object:
        """Read the fixture article."""
        data = {"noteTitle": "Source", "noteContent": "Readable source."}
        return ok(str(data), data=data)

    class Model:
        max_tokens = 256
        calls = 0

        def bind_tools(self, *args, **kwargs):
            return self

        def stream(self, prompt, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield AIMessageChunk(content="", tool_calls=[{
                    "name": "get_article_detail", "args": {"article_id": 1}, "id": "read"}])
            else:
                yield AIMessageChunk(content="Private draft " + " ".join(
                    f"https://user:secret@foreign.invalid/article/{i}?token=private"
                    for i in range(90, 100)))

    result = ToolSession(Model(), {}, {get_article_detail.name: get_article_detail}, {},
                         middleware=(articles.ArticleMiddleware,)).run([HumanMessage("Read")])
    rejected = next(row for row in records if row.get("reason") == "unread_article_reference")
    assert len(rejected["invalid_references"]) == 8 and rejected["omitted_references"] == 2
    assert rejected["read_documents"] == 1 and rejected["candidates"] == 0
    assert {row["article_id"] for row in rejected["invalid_references"]} == set(range(90, 98))
    assert all(not row["same_site"] and not row["known_id"]
               for row in rejected["invalid_references"])
    private_values = ("Private draft", "user:secret", "foreign.invalid", "token=private")
    assert not any(secret in str(rejected) + result.reply for secret in private_values)
    assert "无法核实" in result.reply
