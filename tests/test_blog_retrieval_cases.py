"""Fixed Chinese retrieval evidence, independent of deployment content or model access."""

import copy
from pathlib import Path

import httpx
import pytest
from _memory_support import Capture, config
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.domains.articles.evidence import ArticleEvidence
from agent.products.blog.application import BlogApplication
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from eval import dataset_audit, native_assertions, online_baseline
from eval.fixtures.document_reads import DEFAULT_READ_CHARS, article_page, document_page
from eval.offline_rag_eval import bm25_run, evaluate
from eval.rag_dataset import load
from rag.corpus import content_revision
from rag.reading_requests import tool_args
from rag.search import RagIndex
from rag.sections import PARSER_VERSION, outline
from tools import base
from tools.notes import get_article_detail
from tools.result import ok

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval/fixtures/blog_retrieval"
SUITE = ROOT / "eval/golden/retrieval_requests.jsonl"


@pytest.fixture(scope="module")
def corpus():
    data = load(DATASET, "dev")
    keys = sorted(data["corpus"])
    documents = [
        {"type": "note", "id": number, "title": data["corpus"][key]["title"],
         "content": data["corpus"][key]["text"],
         "revision": content_revision(data["corpus"][key]["title"], data["corpus"][key]["text"])}
        for number, key in enumerate(keys, 1)
    ]
    return data, dict(zip(keys, documents))


def index(documents):
    selected = RagIndex(corpus_loader=lambda: list(documents.values()),
                        vector_warmer=lambda _: None)
    selected.build()
    return selected


@pytest.fixture
def backend(corpus, monkeypatch):
    _, documents = corpus
    public = copy.deepcopy(documents)
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        parts = request.url.path.split("/")
        assert parts[:5] == ["", "api", "public", "documents", "note"]
        assert len(parts) == 7 and parts[-1] == "read"
        identifier = int(parts[-2])
        source = next((row for row in public.values() if row["id"] == identifier), None)
        if source is None:
            return httpx.Response(404)
        params = dict(request.url.params)
        revision = content_revision(source["title"], source["content"])
        if (params.get("revision") and params["revision"] != revision
                or params.get("section_schema") and params["section_schema"] != PARSER_VERSION):
            return httpx.Response(409)
        value = document_page("note", identifier, source["title"], source["content"], params)
        return httpx.Response(200, json={"code": 200, "data": value})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield public, calls


def test_labelled_source_spans_are_unique_and_beyond_the_default_read_window(corpus):
    data, documents = corpus
    for query in data["queries"].values():
        for evidence in query.get("evidence_spans", []):
            key, quote = evidence["document_id"], evidence["quote"]
            assert data["qrels"][query["_id"]][key] > 0
            document = documents[key]
            assert document["content"].count(quote) == 1
            section = next(row for row in outline(document["content"], document["title"])
                           if row["section_id"] == evidence["section_id"])
            assert quote in section["text"]
    tail = data["queries"]["long_tail"]["evidence_spans"][0]
    assert documents["runtime"]["content"].index(tail["quote"]) > DEFAULT_READ_CHARS
    assert "not a production quality score" in data["manifest"]["scope"]


@pytest.mark.parametrize("qid", ["long_tail", "duplicate_heading"])
def test_real_framework_retrieval_locator_reads_the_labelled_original_body(corpus, backend, qid):
    data, _ = corpus
    documents, calls = backend
    query = data["queries"][qid]
    expected = query["evidence_spans"][0]
    source = documents[expected["document_id"]]
    hit = index(documents)._lexical_ranked(query["text"], 3)[0]
    assert hit["id"] == source["id"]
    section = next(row for row in outline(source["content"], source["title"])
                   if row["section_id"] == expected["section_id"])
    matches = [match for match in hit["matches"]
               if match["section_id"] == expected["section_id"]]
    assert matches and expected["section_id"] in hit["section_ids"]
    assert any(expected["quote"] in section["text"][match["offset"]:match["end"]]
               for match in matches)
    canonical = content_revision(source["title"], source["content"])
    assert hit["document_revision"] == hit["content_revision"] == canonical
    assert hit["section_schema"] == PARSER_VERSION
    args = {"article_id": source["id"], "view": "source_range", "section": expected["section_id"],
            "revision": hit["document_revision"], "section_schema": hit["section_schema"],
            "max_chars": 1024}
    model = Capture(AIMessage(content="", tool_calls=[{
        "name": "get_article_detail", "id": "located-read", "args": tool_args(args)}]),
        AIMessage(content="正文中的原话是：" + expected["quote"]
                  + f"。[来源](/article/{source['id']})"))
    runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
        application=BlogApplication(ToolCatalog(
            read_tools={get_article_detail.name: get_article_detail})),
        model_factory=lambda **_: model)
    result = list(runtime.stream_events({"messages": [HumanMessage(content=query["text"])]},
                                        config()))[-1]
    evidence = next(message.artifact for message in model.prompts[-1]
                    if message.type == "tool" and message.name == "get_article_detail")
    assert evidence["kind"] == "ok"
    selected = evidence["data"]
    assert expected["quote"] in result.reply and f"/article/{source['id']}" in result.reply
    assert expected["quote"] in selected["sectionText"] and selected["readComplete"]
    assert selected["contentRevision"] == canonical and selected["sectionSchema"] == PARSER_VERSION
    assert selected["source"] == {
        "type": "note", "id": source["id"], "revision": canonical,
        "section_schema": PARSER_VERSION, "section_id": expected["section_id"],
        "offset": 0, "end": len(selected["sectionText"])}
    assert len(calls) == 1 and len(model.prompts) == 2
    assert dict(calls[0].url.params) == {
        "view": "body", "section": expected["section_id"], "offset": "0", "limit": "1024",
        "revision": canonical, "section_schema": PARSER_VERSION}
    sibling = data["queries"]["duplicate_heading" if qid == "long_tail" else "long_tail"]
    assert sibling["evidence_spans"][0]["quote"] not in selected["sectionText"]


def test_duplicate_headings_and_first_window_require_explicit_location(corpus, backend):
    data, _ = corpus
    documents, _ = backend
    source = documents["runtime"]
    expected = data["queries"]["long_tail"]["evidence_spans"][0]
    ambiguous = get_article_detail.invoke(
        tool_args({"view": "source_range", "article_id": source["id"], "section": "故障恢复"}))
    assert ambiguous.kind == "ok" and ambiguous.data["sectionText"] == ""
    assert ambiguous.data["source"] is None and len(ambiguous.data["sections"]) >= 4
    first = get_article_detail.invoke(tool_args({"view": "full_text", "article_id": source["id"],
                                       "max_chars": DEFAULT_READ_CHARS}))
    assert not first.data["readComplete"] and expected["quote"] not in first.data["noteContent"]


@pytest.mark.parametrize("change", ["content", "section_schema"])
def test_search_locator_cannot_read_edited_content_or_another_section_view(
    corpus, backend, monkeypatch, change
):
    data, _ = corpus
    documents, calls = backend
    query = data["queries"]["long_tail"]
    hit = index(documents)._lexical_ranked(query["text"], 3)[0]
    source = documents["runtime"]
    if change == "content":
        source["content"] = source["content"].replace("工作线程实际退出时", "工作线程启动时")
    invalidations = []
    monkeypatch.setattr("rag.search.invalidate_document", lambda *args: invalidations.append(args))
    result = get_article_detail.invoke(tool_args({"view": "source_range",
        "article_id": source["id"], "section": "s4", "revision": hit["document_revision"],
        "section_schema": ("obsolete-parser" if change == "section_schema"
                           else hit["section_schema"])}))
    assert result.kind == "unavailable" and result.data is None and result.meta["status"] == 409
    assert invalidations == [(source["id"], hit["document_revision"])] and len(calls) == 1


def test_withdrawn_document_cannot_be_read_and_disappears_after_index_rebuild(corpus, backend):
    data, _ = corpus
    public, calls = backend
    selected = index(public)
    query = data["queries"]["long_tail"]["text"]
    previous = selected._lexical_ranked(query, 3)
    removed = public.pop("runtime")
    assert previous[0]["id"] == removed["id"]
    refused = get_article_detail.invoke(tool_args({"view": "source_range",
        "article_id": removed["id"], "section": "s4", "revision": previous[0]["document_revision"],
        "section_schema": previous[0]["section_schema"]}))
    assert refused.kind == "not_found" and refused.data is None and len(calls) == 1
    selected.build()
    assert removed["id"] not in {hit["id"] for hit in selected._lexical_ranked(query, 3)}


def test_standard_metrics_require_both_articles_and_keep_no_answer_out_of_recall(corpus):
    data, _ = corpus
    run, _ = bm25_run(data, 3)
    result = evaluate(data, run, [1, 3])
    assert result["per_query"]["multi_evidence"]["EvidenceCoverage@1"] == 0.5
    assert result["per_query"]["multi_evidence"]["EvidenceCoverage@3"] == 1
    assert result["per_query"]["multi_evidence"]["R@3"] == 1
    assert run["absent"] == {}
    assert "R@3" not in result["per_query"]["absent"]
    assert result["summary"]["all"]["R@3"]["queries"] == sum(
        any(grade > 0 for grade in row.values()) for row in data["qrels"].values())
    for qid in ("firmware_layout", "firmware_paraphrase"):
        assert next(iter(run[qid])) == "firmware"


def test_multiturn_retrieval_suite_uses_real_turns_and_the_standard_http_entrypoint():
    cases = online_baseline.load_suite(SUITE)
    assert len(cases) == 2
    audit = dataset_audit.audit(cases)
    assert audit["rounds"] == audit["annotated_decisions"] == 4
    assert not audit["missing_source_groups"] and not audit["exact_duplicate_inputs"]
    assert not {case["id"] for case in cases}.intersection(
        case["id"] for case in online_baseline.load_suite())
    for case in cases:
        assert "history" not in case["context"] and len(case["rounds"]) == 2


def test_short_reference_judgment_rejects_reading_the_wrong_article_despite_correct_link():
    case = online_baseline.load_suite(SUITE)[0]
    gold = case["rounds"][1]["gold"]
    result = {"text": "文章观点。来源：[文章](/article/23)", "native_events": [{
        "node": "native", "event": "call", "name": "get_article_detail",
        "args": {"article_id": 22}, "kind": "ok"}]}
    assert native_assertions.check(gold, result)
    result["native_events"][0]["args"]["article_id"] = 23
    assert native_assertions.check(gold, result)
    data = article_page({"noteTitle": "Source", "noteContent": "original body"}, 23)
    evidence = ArticleEvidence()
    evidence.observe("get_article_detail", {"article_id": 23}, ok("body", data=data))
    result["native_events"].append({"node": "native", "event": "article_evidence",
                                    "provenance": evidence.provenance("")})
    assert not native_assertions.check(gold, result)
    result["native_events"][-1]["provenance"]["body_reads"][0]["id"] = 22
    assert native_assertions.check(gold, result)


def test_short_reference_acceptance_retains_a_per_round_model_call_limit():
    gold = online_baseline.load_suite(SUITE)[0]["rounds"][1]["gold"]
    events = [{"event": "llm_start"} for _ in range(4)]
    events.append({"node": "native", "event": "call", "name": "get_article_detail",
                   "args": {"article_id": 23}, "kind": "ok"})
    assert any("模型调用超过" in failure for failure in native_assertions.check(
        gold, {"text": "来源 /article/23", "native_events": events}))
