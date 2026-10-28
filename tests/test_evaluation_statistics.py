"""Task-level uncertainty and paired outcomes must not manufacture confidence."""

import pytest

from eval.action_metrics import decision_report, decision_score, timings
from eval.judge_calibration import load_samples, summarize
from eval.statistical_metrics import paired_delta, paired_outcomes, proportion


def test_wilson_interval_distinguishes_observed_score_from_confidence():
    row = proportion(127, 132)
    assert row["rate"] == 127 / 132
    assert row["wilson_95"] == pytest.approx([0.9143919641, 0.9837138209], abs=1e-10)
    assert proportion(20, 20)["wilson_95"][0] < 0.84
    assert proportion(0, 0)["wilson_95"] is None
    assert proportion(0, 132)["wilson_95"][1] > 0.028


@pytest.mark.parametrize("counts", [(-1, 10), (11, 10), (True, 10), (1, -1)])
def test_invalid_counts_fail_loudly(counts):
    with pytest.raises(ValueError):
        proportion(*counts)


def test_paired_test_reports_regressions_and_does_not_claim_equivalence():
    row = paired_outcomes([True, True, False, False], [True, False, True, False])
    assert row["wins"] == row["losses"] == 1
    assert row["exact_mcnemar_p"] == 1
    assert "does not establish equivalence" in row["interpretation"]
    delta = paired_delta([10, 20, 30], [7, 17, 27])
    assert delta["mean_delta"] == -3 and delta["paired_bootstrap_95"] == [-3, -3]
    assert paired_delta([1], [2])["paired_bootstrap_95"] is None


def test_first_decision_does_not_credit_an_unrelated_parallel_tool():
    expected = {"any_tools": ["load_notifications_info_tools", "list_notifications"],
                "allow_text": False}
    def score(names):
        return decision_score([{"node": "native", "event": "llm_done", "tool_calls": names}],
                              expected)
    assert score(["load_notifications_info_tools"]) is True
    assert score(["get_unread_counts"]) is False
    assert score(["list_notifications", "get_server_status"]) is False
    assert score([]) is False
    assert decision_score([{"node": "native", "event": "llm_done", "tool_calls": [],
                           "invalid_tool_calls": True}],
                          {"any_tools": [], "allow_text": True}) is False
    assert decision_score([], expected) is None
    assert decision_report([True, False, None])["correct"]["total"] == 2


def test_timings_separate_discovery_execution_and_model_first_token():
    row = timings([
        {"event": "model_first_token", "duration_s": 0.3},
        {"event": "tool_requested", "name": "load_messages_info_tools", "call_id": "a", "t": 1},
        {"event": "load_domain", "call_id": "a"},
        {"event": "tool_requested", "name": "list_my_messages", "call_id": "b", "t": 2},
        {"node": "native", "event": "call", "call_id": "b", "t": 3},
    ])
    assert row == {"first_business_tool_s": 2, "first_business_result_s": 3, "model_ttft_s": [0.3]}


def test_calibration_keeps_false_positives_false_negatives_and_unknowns():
    samples = load_samples()
    assert len(samples) >= 12 and {sample["expected_issue"] for sample in samples} == {True, False}
    rows = [{"expected_issue": True, "predicted_issue": True},
            {"expected_issue": True, "predicted_issue": False},
            {"expected_issue": False, "predicted_issue": True},
            {"expected_issue": False, "predicted_issue": False},
            {"expected_issue": True, "predicted_issue": None}]
    got = summarize(rows)
    assert got["fp"] == got["fn"] == got["unverified"] == 1
    assert got["precision"]["rate"] == got["recall"]["rate"] == 0.5
