"""CommonMark boundaries and source maps remain revision-bound article evidence."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from eval.fixtures.document_reads import article_page
from rag import sections
from rag.chunks import strategy_signature
from rag.corpus import content_revision
from rag.search import RagIndex


@pytest.mark.parametrize(
    "content,expected",
    [
        ("    # indented code\n\n## real\nbody", ["real"]),
        ("\t# tab-indented code\n\n## real\nbody", ["real"]),
        ("<script>\n# HTML example\n</script>\n\n## real\nbody", ["real"]),
        ("> # quoted heading\n> text\n\n## real\nbody", ["real"]),
        ("- # list-local heading\n  body\n\n## real\nbody", ["real"]),
        ("- ```md\n  # fenced example\n  ```\n\n## real\nbody", ["real"]),
        ("```lang`invalid\n## real\nbody", ["real"]),
        ("~~~\n# code\n```\n## still code\n~~~\n## real\nbody", ["real"]),
        ("```\n# code\n## still code", []),
        ("## **raw** `title` ###\nbody", ["**raw** `title`"]),
        ("First line\nsecond line\n===\nbody\n\nSubheading\n---\nbody",
         ["First line\nsecond line", "Subheading"]),
        ("\\# escaped\n####### too many\n#hashtag\n\n## real\nbody", ["real"]),
        ("##\n### child\nbody", ["", "child"]),
    ],
)
def test_commonmark_boundaries_ignore_code_html_and_container_local_headings(content, expected):
    assert [row["section"] for row in sections.outline(content) if row["level"]] == expected


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_line_maps_slice_original_newlines_unicode_and_descendant_heading_syntax(newline):
    preamble = "说明\u2028仍是同一行" + newline + newline
    child_heading = "###  Child *raw* ###" + newline
    body = "👩🏽‍💻 é needle" + newline + "```text" + newline + "# code" + newline + "```" + newline
    content = (
        preamble + "## Parent ##" + newline + child_heading + body + "## End" + newline + "end"
    )
    rows = sections.outline(content, "T")
    parent, child = rows[1:3]
    assert rows[0]["text"] == preamble
    assert child["heading"] == child_heading
    assert child["section"] == "Child *raw*"
    assert child["text"] == body
    assert content[child["body_start"]:child["body_end"]] == body
    assert child["path"] == ["Parent", "Child *raw*"]
    data = {"noteTitle": "T", "noteContent": content}
    first = article_page(data, 1)
    selected = article_page(data, 1, parent["section_id"], revision=first["contentRevision"])
    assert selected["sectionText"] == child_heading + body
    assert selected["source"]["end"] == len(child_heading + body)
    assert "End" not in selected["subsections"]


def test_empty_parents_duplicate_headings_and_scope_remain_unambiguous():
    content = "Parent\n======\n### duplicate\nfirst\n### duplicate\nsecond\n## sibling\nlast"
    rows = sections.outline(content)
    assert [row["section_id"] for row in rows] == ["s1", "s2", "s3", "s4"]
    assert rows[0]["text"] == "" and rows[1]["path"] == ["Parent", "duplicate"]
    data = {"noteTitle": "T", "noteContent": content}
    initial = article_page(data, 1)
    assert article_page(data, 1, "duplicate")["sectionText"] == ""
    selected = article_page(data, 1, "s3", revision=initial["contentRevision"])
    assert selected["sectionText"] == "second\n"
    parent = article_page(data, 1, "s1", revision=initial["contentRevision"])
    assert parent["sectionText"] == content[rows[0]["body_start"]:]


def test_parser_strategy_is_separate_from_canonical_content_and_rejects_old_views(monkeypatch):
    from agent.domains.articles.reading import project
    from eval.fixtures.document_reads import document_page

    body = "## Parent\nbody\n### Child\nnext"
    first = document_page("note", 1, "T", body, {"limit": 8})
    old_space = strategy_signature()
    assert first["contentRevision"] == content_revision("T", body)
    monkeypatch.setattr(sections, "PARSER_VERSION", "a-new-commonmark-source-view")
    assert strategy_signature() != old_space
    assert sections.reading_revision("T", body) == first["contentRevision"]
    with pytest.raises(ValueError, match="section view"):
        project(first, "note", 1, "", 0, 8, first["contentRevision"], first["sectionSchema"])


def test_retrieval_revision_and_chunk_ranges_are_readable_under_the_same_parser():
    content = "Intro\r\n=====\r\n> # quoted\r\n\r\n## Actual ##\r\nneedle👩🏽‍💻é\r\nend"
    source = {"type": "note", "id": 1, "title": "T", "content": content,
              "revision": content_revision("T", content)}
    index = RagIndex(corpus_loader=lambda: [source], vector_warmer=lambda _: None)
    index.build()
    hit = index._lexical_ranked("needle", 1)[0]
    match = hit["matches"][0]
    frame = article_page(
        {"noteTitle": "T", "noteContent": content}, 1, match["section_id"],
        offset=match["offset"], max_chars=match["end"] - match["offset"],
        revision=hit["document_revision"],
    )
    assert hit["document_revision"] == source["revision"]
    assert frame["contentRevision"] == hit["document_revision"]
    assert frame["sectionText"] == "needle👩🏽‍💻é\r\nend"


def test_parallel_parser_reads_keep_source_maps_deterministic():
    content = "## Parent\r\n### Child\r\nbody\r\n```\r\n# code\r\n```\r\n"
    expected = sections.outline(content)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: sections.outline(content), range(24)))
    assert all(rows == expected for rows in results)
