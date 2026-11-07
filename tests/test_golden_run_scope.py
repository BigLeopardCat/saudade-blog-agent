"""Only complete diagnostic runs may replace their stored baseline."""

import pytest

from eval.run_golden import diagnostic_exit_code, is_full_run


@pytest.mark.parametrize("only,limit,skip_arg,skipped,designed,complete", [
    ("", 0, "", [], [], True),
    ("", 0, "", ["approved_write"], ["approved_write"], True),
    ("", 0, "", ["approved_write", "missing_service"], ["approved_write"], False),
    ("", 0, "", ["missing_service"], [], False),
    ("one_case", 0, "", [], [], False),
    ("", 3, "", [], [], False),
    ("", 0, "missing_service", ["missing_service"], [], False),
])
def test_complete_run_selection(only, limit, skip_arg, skipped, designed, complete):
    assert is_full_run(only=only, limit=limit, skip_ids_arg=skip_arg,
                       skipped_ids=skipped, design_skipped_ids=designed) is complete


def test_custom_corpus_never_replaces_the_default_baseline():
    assert not is_full_run(only="", limit=0, skip_ids_arg="", skipped_ids=[],
                           design_skipped_ids=[], golden_file="custom.jsonl")


@pytest.mark.parametrize("results,unverified,code", [
    ([{"ok": True}], False, 0),
    ([{"ok": False, "rerun": {"ok": True}, "final_ok": True}], False, 1),
    ([{"ok": True, "error": "incomplete evidence"}], False, 1),
    ([{"ok": True}], True, 3),
    ([], True, 3),
    ([], False, 2),
])
def test_diagnostic_status_never_certifies_release_or_hides_first_failures(
        results, unverified, code):
    assert diagnostic_exit_code(results, unverified=unverified) == code
