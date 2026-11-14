"""Expanded reading windows preserve leaf provenance and obey source/reading budgets."""

import copy
import json
from types import SimpleNamespace

import pytest

from rag.context_windows import context_range, expand_context
from rag.reading import candidate_text
from rag.sections import PARSER_VERSION, body_blocks

PROFILE = SimpleNamespace(count=len)


def test_parent_and_neighbor_blocks_cover_sources_without_crossing_section_boundaries():
    text = "前段。\r\n\r\n中段 needle。\r\n\r\n尾段。"
    start = text.index("needle")
    blocks = body_blocks(text)
    assert "".join(text[a:b] for a, b in blocks) == text
    parent = context_range(text, start, start + 6, PROFILE, max_tokens=100)
    neighbors = context_range(text, start, start + 6, PROFILE, mode="neighbors", max_tokens=100)
    assert text[parent["offset"]:parent["end"]] == "中段 needle。\r\n\r\n"
    assert (neighbors["offset"], neighbors["end"]) == (0, len(text))


def test_context_clips_margins_without_removing_the_retrieved_anchor():
    text = "甲" * 150 + "needle" + "乙" * 150
    selected = context_range(text, 150, 156, PROFILE, max_tokens=64)
    assert selected["offset"] <= 150 < 156 <= selected["end"]
    assert selected["end"] - selected["offset"] <= 64 and selected["bounded"]
    with pytest.raises(ValueError, match="cannot contain"):
        context_range(text, 150, 156, PROFILE, max_tokens=5)
    with pytest.raises(ValueError, match="valid source"):
        context_range(text, -1, 156, PROFILE)


def test_read_hint_expands_only_the_read_locator_and_keeps_rank_score_and_excerpt():
    text = "before needle after"
    hit = {"type": "note", "id": 1, "title": "T", "score": .8,
           "document_revision": "a" * 64, "section_schema": PARSER_VERSION,
           "sections": ["S"], "section_ids": ["s1"],
           "matches": [{"section_id": "s1", "offset": 7, "end": 13}]}
    saved = copy.deepcopy(hit)
    expanded = expand_context([hit], lambda *_: text, PROFILE, max_tokens=100)
    assert hit == saved and expanded[0]["score"] == .8
    assert expanded[0]["matches"][0]["offset"] == 7
    hint = candidate_text(1, expanded[0])
    request = json.loads(hint)["read_requests"][0]["request"]
    assert expanded[0]["matches"][0]["end"] == 13
    assert request["offset"] == 0 and request["max_chars"] == len(text)
    source = json.loads(hint)["source"]
    assert source["revision"] == "a" * 64 and source["section_schema"] == PARSER_VERSION
