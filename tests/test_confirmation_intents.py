"""Equivalent single-card intents retain exact target and changed-field boundaries."""

import copy

import pytest

from eval.golden_evaluation import check_gold
from eval.online_baseline import load_suite

FAVORITE = {"action": "favorite", "imageIds": [72, 75], "isFavorite": False}
METADATA = {"action": "metadata", "items": [
    {"imageId": 72, "changes": {"isFavorite": False}},
    {"imageId": 75, "changes": {"isFavorite": False}},
]}


def case_and_result(intent):
    case = next(c for c in load_suite() if c["id"] == "gallery_bulk_favorite_prepare")
    payload = {"workflow": "gallery_images", "specs": [{"tool": "manage_gallery_images",
        "args": {"version": "fresh-version", "expiresAt": 123,
                 "intent": copy.deepcopy(intent)}}]}
    return case, {"text": "Review the card.", "native_events": [], "exec_rows": [],
                  "commands": [], "tool_calls": [], "confirm_payloads": [payload]}


@pytest.mark.parametrize("intent,passed", [
    (FAVORITE, True), (METADATA, True),
    ({**FAVORITE, "isFavorite": True}, False),
    ({**FAVORITE, "isFavorite": 0}, False),
    ({**FAVORITE, "imageIds": [72]}, False),
    ({**FAVORITE, "imageIds": [72, 76]}, False),
    ({**FAVORITE, "imageIds": [72, 75, 76]}, False),
    ({**FAVORITE, "folderId": 3}, False),
    ({"action": "metadata", "items": [
        {"imageId": 72, "changes": {"isFavorite": False, "description": "new"}},
        METADATA["items"][1]]}, False),
    ({"action": "metadata", "items": [METADATA["items"][0]]}, False),
    ({"action": "metadata", "items": [METADATA["items"][0]] * 2}, False),
    ({"action": "delete", "imageIds": [72, 75]}, False),
])
def test_complete_intents_accept_equivalence_without_extra_mutations(intent, passed):
    case, result = case_and_result(intent)
    assert (not check_gold(case["gold"], result)) is passed


@pytest.mark.parametrize("fault", ["missing", "two_cards", "two_specs", "tool", "workflow"])
def test_atomic_confirmation_requires_one_card_and_one_correct_operation(fault):
    case, result = case_and_result(METADATA)
    payload = result["confirm_payloads"][0]
    if fault == "missing":
        result["confirm_payloads"] = []
    elif fault == "two_cards":
        result["confirm_payloads"].append(copy.deepcopy(payload))
    elif fault == "two_specs":
        payload["specs"].append(copy.deepcopy(payload["specs"][0]))
    elif fault == "tool":
        payload["specs"][0]["tool"] = "other_tool"
    else:
        payload["workflow"] = "other_workflow"
    assert check_gold(case["gold"], result)
