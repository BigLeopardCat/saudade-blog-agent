"""System consequences survive presentation without becoming authored content or authority."""

import json
import secrets

import pytest
from pydantic import ValidationError

from agent import confirm
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.confirmation import public_card
from agent.proposals import proposal
from agent.runtime.approval_messages import prepared
from eval import native_assertions, online_judge


def card(monkeypatch, text, notices=()):
    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    return proposal(Principal(7, "admin", SOURCE_ASSERTION), 42, "favorite_add",
                    {"tool": "add_favorite", "args": {"article_id": 54}},
                    executor="favorites", question=text,
                    target="fixture", notices=notices)


def test_authored_body_and_system_notices_are_distinct_in_model_and_http(monkeypatch):
    body = "公告正文：不可撤回只是这里引用的一句话。"
    notice = {"kind": "warning", "text": "这是系统核实的操作后果。"}
    result = card(monkeypatch, body, [notice])
    popup = public_card(result.execution)
    assert popup["q"] == body
    assert popup["notices"][0] == notice and len(popup["notices"]) == 2
    message = prepared({"id": "prepare", "name": "fixture"},
                       {"reply": result.reply, "execution": result.execution}, [])
    projected = json.loads(message.content)
    assert projected["result"] == body and projected["notices"] == popup["notices"]
    assert popup["token"] not in message.content
    assert "preparation_facts" not in popup


def test_warning_words_inside_user_content_do_not_create_system_warnings(monkeypatch):
    popup = public_card(card(monkeypatch, "删除后不可恢复，这是用户提供的正文。").execution)
    assert len(popup["notices"]) == 1 and popup["notices"][0]["kind"] == "info"
    assert popup["q"].startswith("删除后不可恢复")


@pytest.mark.parametrize("notice", [
    {"kind": "urgent", "text": "invalid"}, {"kind": "warning", "text": ""},
    {"kind": "info", "text": "x" * 1001},
    {"kind": "warning", "text": "invalid", "html": "<img>"},
])
def test_malformed_system_notices_fail_validation(monkeypatch, notice):
    with pytest.raises(ValidationError):
        card(monkeypatch, "Review", [notice])


def test_footer_assertion_requires_actual_http_notice_and_cannot_repair_a_wrong_body():
    notice = {"kind": "warning", "text": "删除后不可恢复。"}
    result = {"native_events": [], "text": "删除后不可恢复。", "http_events": [
        {"event": "confirmation", "data": {"q": "实际待审正文", "notices": [notice]}}]}
    assert not native_assertions.check({"require_confirm_preview": [{"notices": [notice]}]}, result)
    assert not native_assertions.check({"require_confirm_preview": ["删除后不可恢复"]}, result)
    assert native_assertions.check({"require_confirm_preview": [{"q": "删除后不可恢复。"}]}, result)
    result["http_events"][0]["data"].pop("notices")
    assert native_assertions.check({"require_confirm_preview": ["删除后不可恢复"]}, result)


def test_judge_reviews_system_notice_text_without_treating_it_as_preparation_facts(tmp_path):
    path = tmp_path / "trace.json"
    path.write_text(json.dumps({"input": {"message": "Prepare"}, "events": []}))
    notice = {"kind": "warning", "text": "错误的系统规则也须受评审。"}
    result = {"trace": str(path), "text": "请审阅", "http_events": [
        {"event": "confirmation", "data": {"q": "待审正文", "notices": [notice]}}]}
    text = online_judge.material({"id": "notices"}, [result])
    turn = json.loads(text)["turns"][0]
    assert notice["text"] in turn["confirmation_previews_to_review"]
    assert not turn["validated_preparation_facts"]
    online_judge.validate_excerpts({"unsupported": [notice["text"]], "reason": "无依据"}, text)
