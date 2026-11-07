"""Fixed Chinese retrieval evidence, independent of deployment content or model access."""

import copy
from pathlib import Path

import pytest

from eval import dataset_audit, native_assertions, online_baseline
from eval.offline_rag_eval import bm25_run, evaluate
from eval.rag_dataset import load
from rag.corpus import content_revision
from rag.reading import DEFAULT_READ_CHARS, RevisionChanged, article_page
from rag.search import RagIndex
from rag.sections import outline

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval/fixtures/blog_retrieval"
SUITE = ROOT / "eval/golden/retrieval_requests.jsonl"


@pytest.fixture(scope="module")
def corpus():
    data = load(DATASET, "test")
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
def test_real_retrieval_locator_selects_the_labelled_body_not_a_same_named_sibling(corpus, qid):
    data, documents = corpus
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
    selected = article_page(source, source["id"], section=expected["section_id"],
                            revision=hit["document_revision"])
    assert expected["quote"] in selected["sectionText"] and selected["readComplete"]
    sibling = data["queries"]["duplicate_heading" if qid == "long_tail" else "long_tail"]
    assert sibling["evidence_spans"][0]["quote"] not in selected["sectionText"]
    ambiguous = article_page(source, source["id"], section="故障恢复")
    assert ambiguous["sectionText"] == "" and len(ambiguous["sections"]) >= 4
    if qid == "long_tail":
        first = article_page(source, source["id"])
        assert not first["readComplete"] and expected["quote"] not in first["noteContent"]


def test_search_locator_cannot_read_the_edited_version_under_the_old_revision(corpus):
    data, documents = corpus
    query = data["queries"]["long_tail"]
    hit = index(documents)._lexical_ranked(query["text"], 3)[0]
    source = copy.deepcopy(documents["runtime"])
    source["content"] = source["content"].replace("工作线程实际退出时", "工作线程启动时")
    with pytest.raises(RevisionChanged):
        article_page(source, source["id"], section="s4", revision=hit["document_revision"])


def test_rebuilt_public_corpus_removes_withdrawn_document_from_the_lexical_index(corpus):
    data, documents = corpus
    public = copy.deepcopy(documents)
    selected = index(public)
    query = data["queries"]["long_tail"]["text"]
    previous = selected._lexical_ranked(query, 3)
    removed = public.pop("runtime")
    assert previous[0]["id"] == removed["id"]
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
    assert result["summary"]["all"]["R@3"]["queries"] == 6
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
    assert not native_assertions.check(gold, result)


def test_short_reference_acceptance_retains_a_per_round_model_call_limit():
    gold = online_baseline.load_suite(SUITE)[0]["rounds"][1]["gold"]
    events = [{"event": "llm_start"} for _ in range(4)]
    events.append({"node": "native", "event": "call", "name": "get_article_detail",
                   "args": {"article_id": 23}, "kind": "ok"})
    assert any("模型调用超过" in failure for failure in native_assertions.check(
        gold, {"text": "来源 /article/23", "native_events": events}))
