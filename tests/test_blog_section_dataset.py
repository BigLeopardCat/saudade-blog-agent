"""Frozen blog evidence has source-valid labels and disjoint query families."""

from pathlib import Path

from eval.rag_dataset import load


def test_public_blog_snapshot_and_query_families_are_bound_to_source_evidence():
    root = Path(__file__).resolve().parents[1] / "eval/fixtures/blog_evidence"
    development, test, all_rows = (load(root, split) for split in ("dev", "test", "all"))
    assert not set(development["queries"]) & set(test["queries"])
    assert set(development["queries"]) | set(test["queries"]) == set(all_rows["queries"])
    assert not {q["family"] for q in development["queries"].values()} & {
        q["family"] for q in test["queries"].values()}
    assert development["corpus"] == test["corpus"] == all_rows["corpus"]
    assert any(not d["title"] for d in all_rows["corpus"].values())
    for query in all_rows["queries"].values():
        assert query.get("evidence_spans") or query.get("answerable") is False
    assert {q["category"] for q in all_rows["queries"].values()} >= {
        "known_article", "multi_document", "unanswerable", "historical", "long_tail"}
    assert "Independent human" in all_rows["manifest"]["annotation_status"]
