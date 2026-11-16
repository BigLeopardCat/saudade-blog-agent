"""Visible retrieval evidence distinguishes a complete section from a bounded excerpt."""

import json

import pytest
from pydantic import ValidationError

from rag.passages import attach_passages
from rag.reading import candidate_text
from rag.sections import PARSER_VERSION, outline, reading_revision
from rag.source import BodyPassage


def passage(*, offset=0, end=None, cap=12000):
    document = {"type": "note", "id": 41, "title": "Model configuration",
                "content": "## 配置\nProvider: example. 温度 0.7。\n"}
    section = outline(document["content"], document["title"])[0]
    total = len(section["text"])
    revision = reading_revision(document["title"], document["content"])
    hit = {"type": "note", "id": 41, "title": document["title"],
           "content_revision": revision, "document_revision": revision,
           "section_schema": PARSER_VERSION, "corpus_revision": "snapshot",
           "matches": [{"section_id": section["section_id"], "section_path": ["配置"],
                        "offset": offset, "end": total if end is None else end}]}
    result = attach_passages([hit], {("note", 41): document}, "snapshot", max_chars=cap)[0]
    return result, total


@pytest.mark.parametrize("offset,end,cap,complete", [
    (0, None, 12000, True), (1, None, 12000, False),
    (0, 5, 12000, False), (0, None, 5, False),
])
def test_source_length_and_completeness_reach_the_model(offset, end, cap, complete):
    hit, total = passage(offset=offset, end=end, cap=cap)
    raw = hit["passages"][0]
    shown = json.loads(candidate_text(1, hit))["passages"][0]
    assert raw["section_chars"] == shown["section_chars"] == total
    assert shown["section_complete"] is complete
    assert shown["range"]["end"] - shown["range"]["offset"] == len(shown["text"])
    assert shown["text"] == raw["text"]


def test_unknown_length_does_not_claim_complete_or_incomplete():
    hit, _ = passage()
    hit["passages"][0].pop("section_chars")
    shown = json.loads(candidate_text(1, hit))["passages"][0]
    assert "section_chars" not in shown and "section_complete" not in shown


@pytest.mark.parametrize("length", [False, -1, 1, "100"])
def test_invalid_lengths_cannot_authorize_section_completeness(length):
    hit, _ = passage()
    with pytest.raises(ValidationError):
        BodyPassage.model_validate({**hit["passages"][0], "section_chars": length})
