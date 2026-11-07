"""Approval previews must match the actual user-visible HTTP card fields."""

import pytest

from eval import native_assertions


@pytest.mark.parametrize("change", [None, {"fit": "contain"}, {"image_id": 12}])
def test_cover_preview_checks_the_actual_card_separately_from_signed_execution(change):
    expected = {"image_id": 11, "fit": "cover"}
    gold = {"require_confirm_preview": [{"cover_review": expected}]}
    review = {**expected, **(change or {})}
    result = {"native_events": [], "text": "图片11，铺满裁切", "http_events": [
        {"event": "confirmation", "data": {"q": "图片11，铺满裁切", "cover_review": review}}]}
    assert bool(native_assertions.check(gold, result)) is (change is not None)
    result["http_events"][0]["data"].pop("cover_review")
    assert native_assertions.check(gold, result)


def test_confirmation_preview_is_checked_on_the_card_without_requiring_duplicate_reply():
    gold = {"require_confirm_preview": [r"父级[^\n]*编程"]}
    result = {"native_events": [], "text": "请核对卡片。", "http_events": [
        {"event": "confirmation", "data": {"q": "父级为一级标签「编程」"}}]}
    assert not native_assertions.check(gold, result)
    result["http_events"][0]["data"]["q"] = "父级为一级标签「生活」"
    assert native_assertions.check(gold, result)
    result["text"] = "父级为一级标签「编程」"
    assert native_assertions.check(gold, result)  # Assistant text cannot repair the wrong card.
    result["http_events"][0]["event"] = "text"
    assert native_assertions.check(gold, result)
    result["http_events"] = []
    assert native_assertions.check(gold, result)


@pytest.mark.parametrize("review", [
    {"prompt": "原始提示词", "references": []},
    {"prompt": "被改写的提示词", "references": []},
    {"prompt": "原始提示词", "references": [{"image_id": 7}]},
    {"prompt": "原始提示词"},
    None,
])
def test_interactive_image_review_checks_exact_prompt_and_reference_selection(review):
    expected = {"prompt": "原始提示词", "references": []}
    gold = {"require_confirm_preview": [{"image_review": expected}]}
    result = {"native_events": [], "text": "原始提示词，参考图：无", "http_events": [
        {"event": "confirmation", "data": {"q": "请核对卡片", "image_review": review}}]}
    assert (not native_assertions.check(gold, result)) is (review == expected)


def test_image_review_preview_requires_the_actual_confirmation_event():
    review = {"prompt": "原始提示词", "references": []}
    gold = {"require_confirm_preview": [{"image_review": review}]}
    result = {"native_events": [], "text": "原始提示词", "confirm_payloads": [
        {"specs": [{"args": {"image_review": review}}]}], "http_events": []}
    assert native_assertions.check(gold, result)
    result["http_events"] = [{"event": "text", "data": {"image_review": review}}]
    assert native_assertions.check(gold, result)
    result["http_events"][0]["event"] = "confirmation"
    assert not native_assertions.check(gold, result)
    assert native_assertions.check({"require_confirm_preview": [{}]}, result)
    assert native_assertions.check({"require_confirm_preview": [{"specs": []}]}, result)
