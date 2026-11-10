"""Text decision coverage and missing lineage remain explicit in mixed suites."""

from eval.dataset_audit import audit


def test_vision_without_decisions_does_not_hide_complete_text_coverage():
    cases = [
        {"id": "text", "identity": "user", "tags": ["chat"], "user_input": "你好",
         "first_action": {"any_tools": [], "allow_text": True}, "source_group": "chat-source"},
        {"id": "image", "identity": "user", "tags": ["vision"], "user_input": "看图"},
    ]
    result = audit(cases)
    assert result["unannotated_decisions"] == 1
    assert result["text_rounds"] == 1 and result["unannotated_text_decisions"] == 0
    assert result["missing_source_groups"] == ["image"]


def test_multi_turn_decision_gaps_and_duplicate_inputs_remain_visible():
    case = {"id": "first", "identity": "user", "tags": ["memory"],
            "rounds": [{"user_input": "选甲"}, {"user_input": "不，选乙"}],
            "decision_expectations": [{"any_tools": [], "allow_text": True}, None]}
    result = audit([case, {**case, "id": "second"}])
    assert result["text_rounds"] == 4 and result["unannotated_text_decisions"] == 2
    assert result["missing_source_groups"] == ["first", "second"]
    assert result["exact_duplicate_inputs"] == [["first", "second"]]


def test_release_text_suite_has_a_decision_annotation_for_every_round():
    from eval.online_baseline import load_suite

    cases = load_suite()
    for case in cases:
        if "vision" in case.get("tags", []):
            continue
        rounds = case.get("rounds") or [case]
        labels = case.get("decision_expectations") or [case.get("first_action")]
        assert len(labels) == len(rounds) and all(labels), case["id"]
    assert audit(cases)["unannotated_text_decisions"] == 0
