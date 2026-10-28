"""Archived no-popup premises remain comparable after leaving runtime authorization."""

from eval.run_golden import check_no_popup_premises


def test_archived_question_premise_retains_positive_and_negative_controls():
    cases = [
        {
            "id": "question",
            "user_input": "小猫咪，如果把文章 12 设为私密呢？",
            "premise_no_popup": {"kind": "question"},
        },
        {
            "id": "command",
            "user_input": "把文章 12 设为私密",
            "premise_no_popup": {"kind": "question"},
        },
    ]
    kept, skipped, rows = check_no_popup_premises(cases)
    assert [case["id"] for case in kept] == ["question"]
    assert skipped == ["command"]
    assert [row["state"] for row in rows] == ["ok", "changed"]
