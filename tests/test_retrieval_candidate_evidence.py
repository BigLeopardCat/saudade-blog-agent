"""Candidates distinguish source fields while preserving every ranked read locator."""

from copy import deepcopy

import pytest
from _memory_support import Capture, config
from langchain_core.messages import ToolMessage

from agent.domains.articles.evidence import ArticleEvidence
from agent.products.blog.articles import ArticleMiddleware
from agent.runtime.session import ToolSession
from agent.runtime.tool_execution import result_limit
from config.settings import settings
from rag import search
from rag.chunks import split
from rag.corpus import content_revision
from rag.ranking import EXCERPT_CHARS, documents, source_excerpt
from rag.reading import candidate_text, render_search_result, search_text
from rag.sections import outline
from rag.tokenization import tokenize
from rag.vector_index import rrf_fuse
from tools.notes import rag_search

QUERY = "持久调度归还槽位"
FACT = "事务提交后保存执行结果，工作线程退出后归还并发槽位。"


def index_for(body, title="合成调度材料"):
    doc = {"type": "note", "id": 841, "title": title, "content": body}
    doc["revision"] = content_revision(title, body)
    index = search.RagIndex(corpus_loader=lambda: [doc], vector_warmer=lambda _: None)
    index.build()
    return index


def test_title_only_background_is_retained_but_not_presented_as_a_body_match():
    body = "## 背景\n" + "容量规划背景。" * 900 + "\n## 运行机制\n" + QUERY + "：" + FACT
    body += "\n" + "同一小节的其他背景信息。" * 900
    body += "\n## 附录\n" + "容量规划背景。" * 200
    index = index_for(body)
    hit = index._lexical_ranked(QUERY, 1, article_id=841)[0]
    strong = next(match for match in hit["matches"] if "body" in match["matched_fields"])
    weak = [match for match in hit["matches"] if match["matched_fields"] == ["title"]]
    assert len(hit["matches"]) == 3 and len(weak) == 2
    assert len({match["section_id"] for match in hit["matches"]}) == 3
    assert strong["retrieval_scores"]["bm25"] > max(
        match["retrieval_scores"]["bm25"] for match in weak)
    assert FACT in strong["excerpt"]["text"]
    sections = {row["section_id"]: row["text"] for row in outline(body)}
    for match in hit["matches"]:
        excerpt = source_excerpt(match)
        assert excerpt and len(excerpt["text"]) <= EXCERPT_CHARS
        assert sections[match["section_id"]][excerpt["offset"]:excerpt["end"]] == excerpt["text"]
        assert "_source_index" not in match
    wire = candidate_text(1, hit)
    assert "match_fields=title,body" in wire and "match_fields=title index_excerpt" in wire
    assert "bm25=" in wire and FACT in wire
    assert all(f"section={match['section_id']} " in wire for match in hit["matches"])


def test_different_chapters_keep_their_own_relevant_excerpts_and_locations():
    body = "## 租约取消\n运行时通过取消信号释放租约。\n## 回执幂等\n业务执行账保存回执并去重。\n"
    hit = index_for(body, "运行材料")._lexical_ranked("租约取消 回执幂等", 1)[0]
    assert set(hit["section_ids"]) == {"s1", "s2"}
    assert all("body" in match["matched_fields"] or "heading" in match["matched_fields"]
               for match in hit["matches"])
    wire = candidate_text(1, hit)
    assert "运行时通过取消信号释放租约" in wire and "业务执行账保存回执并去重" in wire
    assert "section=s1 " in wire and "section=s2 " in wire


def test_candidate_versions_stay_with_their_document_and_do_not_mutate_evidence():
    first = ranked("bm25")
    first["document_revision"] = "a" * 64
    second = {**deepcopy(first), "id": 2, "document_revision": "b" * 64}
    before = deepcopy([first, second])
    meta = {"retrieval": {"route": "lexical"}}
    text = search_text(meta, candidate_text(1, first) + "\n" + candidate_text(2, second))
    shown = render_search_result([first, second], text, meta, len(text) - 1)
    assert shown.kind == "ok" and shown.data == [first]
    assert "revision=" + "a" * 64 in shown.text
    assert "revision=" + "b" * 64 not in shown.text
    assert [first, second] == before


def test_excerpt_offsets_are_exact_for_deep_unicode_crlf_sources():
    body = "👩🏽‍💻 é\r\n" * 200 + "按 API_TOKEN 读取回执。\r\n" + "无关说明。" * 100
    chunk = {"type": "note", "id": 1, "title": "材料", "section": "配置", "section_id": "s7",
             "text": "配置\n" + body, "prefix_chars": 3, "start": 30000,
             "end": 30000 + len(body), "revision": "a" * 64}
    hit = documents([chunk], {0: 0.9}, "corpus", query_terms=tokenize("API_TOKEN"))[0]
    match = hit["matches"][0]
    excerpt = source_excerpt(match)
    assert excerpt and "API_TOKEN" in excerpt["text"]
    assert excerpt["offset"] > chunk["start"]
    low, high = excerpt["offset"] - chunk["start"], excerpt["end"] - chunk["start"]
    assert body[low:high] == excerpt["text"]
    assert "\\r\\n" in candidate_text(1, hit)


def ranked(method, *, ident="s1", offset=0):
    text = "持久调度释放槽位。"
    chunk = {"type": "note", "id": 1, "title": "调度", "section": "机制", "section_id": ident,
             "text": text, "start": offset, "end": offset + len(text), "revision": "a" * 64}
    return documents([chunk], {0: 4.5 if method == "bm25" else 0.7}, "corpus",
                     retriever=method, query_terms=tokenize(QUERY))[0]


def test_fusion_merges_same_location_scores_without_mutation_or_mixing_score_scales():
    lexical, dense = ranked("bm25"), ranked("vector")
    before = deepcopy((lexical, dense))
    result = rrf_fuse([lexical], [dense], 60, 1)[0]
    match = result["matches"][0]
    assert len(result["matches"]) == 1
    assert match["retrieval_scores"] == {"bm25": 4.5, "vector": 0.7}
    assert source_excerpt(match) == source_excerpt(lexical["matches"][0])
    assert "bm25=4.5 vector=0.7" in candidate_text(1, result)
    assert "score" not in result and (lexical, dense) == before


def test_dense_location_and_its_excerpt_survive_three_lexical_locations():
    lexical = ranked("bm25")
    for ident, offset in [("s2", 50), ("s3", 100)]:
        other = ranked("bm25", ident=ident, offset=offset)
        for key in ("sections", "section_ids", "matches"):
            lexical[key].extend(other[key])
    dense = ranked("vector", ident="s22", offset=30000)
    result = rrf_fuse([lexical], [dense], 60, 1)[0]
    match = next(row for row in result["matches"] if row["section_id"] == "s22")
    assert match["offset"] == 30000 and source_excerpt(match) == dense["matches"][0]["excerpt"]
    wire = candidate_text(1, result)
    assert "section=s22 " in wire and "index_excerpt[30000:" in wire


def test_fusion_rejects_conflicting_source_text_for_one_version_and_range():
    lexical, dense = ranked("bm25"), ranked("vector")
    dense["matches"][0]["excerpt"]["text"] = "错" * len(dense["matches"][0]["excerpt"]["text"])
    with pytest.raises(ValueError, match="conflicting source excerpts"):
        rrf_fuse([lexical], [dense], 60, 1)


def test_semantic_locator_without_literal_overlap_stays_visible():
    text = "调度器在退出时释放许可。"
    chunk = {"type": "note", "id": 1, "title": "材料", "section": "机制", "section_id": "s1",
             "text": text, "start": 0, "end": len(text), "revision": "a" * 64}
    hit = documents([chunk], {0: 0.8}, "corpus", retriever="vector",
                    query_terms=tokenize("worker cancellation"))[0]
    assert hit["matches"][0]["matched_fields"] == []
    wire = candidate_text(1, hit)
    assert "vector=0.8 match_fields=none" in wire and chunk["text"] in wire


def test_index_excerpt_does_not_become_live_body_read_evidence():
    hit = ranked("bm25")
    evidence = ArticleEvidence()
    message = ToolMessage(content=candidate_text(1, hit), tool_call_id="search",
                          artifact={"kind": "ok", "data": [hit]})
    evidence.observe("rag_search", {"query": QUERY}, message)
    assert evidence.candidates == {1: hit["title"]}
    assert evidence.documents == {} and evidence.read_ranges == {}


@pytest.mark.parametrize("excerpt", [
    {"offset": None, "end": 2, "text": "ab"},
    {"offset": 0, "end": None, "text": "ab"},
    {"offset": True, "end": 2, "text": "ab"},
    {"offset": -1, "end": 1, "text": "ab"},
    {"offset": 0, "end": 1, "text": "ab"},
    {"offset": 0, "end": EXCERPT_CHARS + 1, "text": "x" * (EXCERPT_CHARS + 1)},
    {"offset": 500, "end": 502, "text": "ab"},
])
def test_invalid_excerpt_coordinates_are_never_upgraded_to_visible_source_evidence(excerpt):
    hit = ranked("bm25")
    match = hit["matches"][0]
    match["excerpt"] = excerpt
    assert source_excerpt(match) is None
    assert "index_excerpt[" not in candidate_text(1, hit)


def test_bad_chunk_ranges_do_not_create_an_excerpt_and_budgeting_keeps_whole_candidates():
    chunk = {"type": "note", "id": 1, "title": "材料", "section": "配置",
             "text": "body", "start": 0, "end": 999}
    invalid = documents([chunk], {0: 1}, "corpus", query_terms=["body"])[0]
    assert "excerpt" not in invalid["matches"][0]
    first = ranked("bm25")
    second = {**ranked("vector"), "id": 2}
    meta = {"retrieval": {"mode": "hybrid"}}
    text = search_text(meta, candidate_text(1, first) + "\n" + candidate_text(2, second))
    shown = render_search_result([first, second], text, meta, len(text) - 1)
    assert shown.kind == "ok" and shown.data == [first]
    assert candidate_text(1, first) in shown.text and "id=2 " not in shown.text


@pytest.mark.parametrize("token_limit", [None, 4096])
def test_full_tool_frame_keeps_multiple_documents_or_reports_exact_omissions(
        monkeypatch, token_limit):
    chunks = []
    for ident in range(1, 9):
        title = "公开技术材料" + str(ident)
        body = "".join(f"## 章节{n}\n{QUERY}：{FACT}\n" + "其他证据背景。" * 100 + "\n"
                       for n in range(1, 4))
        chunks.extend({**chunk, "type": "note", "id": ident, "title": title,
                       "revision": content_revision(title, body)} for chunk in split(title, body))
    hits = documents(chunks, {index: 1.0 for index in range(len(chunks))}, "corpus",
                     query_terms=tokenize(QUERY), limit=8)
    assert len(hits) == 8 and all(len(hit["matches"]) == 3 for hit in hits)
    meta = {"retrieval": {"mode": "lexical", "limit": 8}}
    text = search_text(meta, "\n".join(candidate_text(i + 1, hit) for i, hit in enumerate(hits)))
    message = ToolMessage(content=text, tool_call_id="search", artifact={
        "kind": "ok", "data": hits, "meta": meta})
    if token_limit is not None:
        monkeypatch.setattr(settings, "agent_tool_result_tokens", token_limit)
    session = ToolSession(Capture(), config(), {"rag_search": rag_search}, {})
    middleware = ArticleMiddleware(session)
    cap = result_limit(message)
    shown = middleware.render_result({"name": "rag_search", "args": {"query": QUERY}}, message, cap)
    visible = shown.artifact["data"]
    assert shown.status == "success" and shown.artifact["kind"] == "ok"
    assert len(shown.content) <= cap and shown.content.count("index_excerpt[") == 3 * len(visible)
    assert set(middleware.evidence.candidates) == {hit["id"] for hit in visible}
    assert middleware.evidence.documents == {} and middleware.evidence.read_ranges == {}
    for i, hit in enumerate(visible):
        assert candidate_text(i + 1, hit) in shown.content
    if token_limit is None:
        assert len(visible) == 8 and "presentation" not in shown.artifact["meta"]
    else:
        assert 0 < len(visible) < 8
        expected = {"shown_candidates": len(visible), "omitted_candidates": 8 - len(visible)}
        assert shown.artifact["meta"]["presentation"] == expected
        assert '"presentation":' in shown.content
        assert all(f"id={hit['id']} " not in shown.content for hit in hits[len(visible):])
