"""Typed review content survives checkpoint, HTTP and evaluation without gaining authority."""

import json
import secrets

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import ValidationError

from agent import confirm
from agent.contracts.events import ConfirmationDetails, confirmation_text
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.confirmation import public_card
from agent.proposals import proposal
from agent.runtime.approval_messages import prepared
from eval import native_assertions, online_judge


def test_review_is_complete_after_checkpoint_and_does_not_change_the_signed_operation(monkeypatch):
    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    details = {
        "fields": [
            {"label": "服务地址", "value": "https://api.openai.com/v1"},
            {"label": "标题", "value": "**原样** <b>数据</b>"},
        ],
        "sections": [
            {"label": "正文", "text": " 原文\n# 章节\n__CMD__:literal😀\t ", "format": "markdown"}
        ],
    }
    spec = {"tool": "add_favorite", "args": {"article_id": 54}}
    result = proposal(
        Principal(7, "admin", SOURCE_ASSERTION),
        42,
        "favorite_add",
        spec,
        executor="favorites",
        question="确认？",
        target="fixture",
        payload={"private": "unchanged"},
        details=details,
    )
    serializer = JsonPlusSerializer(allowed_msgpack_modules=None)
    execution = serializer.loads_typed(serializer.dumps_typed(result.execution))
    popup = public_card(execution)
    assert popup["details"] == details
    assert execution["pending_action"]["payload"] == {"private": "unchanged"}
    assert "specs" not in popup and "private" not in json.dumps(popup)
    assert confirm.verify(popup["token"], 7, 42)["specs"] == [spec]
    assert confirm.verify(popup["token"], 8, 42) is None
    message = prepared(
        {"name": "prepare", "id": "call"}, {"reply": result.reply, "execution": execution}, []
    )
    assert json.loads(message.content)["details"] == details
    assert popup["token"] not in message.content


@pytest.mark.parametrize(
    "details",
    [
        {"fields": [{"label": "地址", "value": {"url": "https://example.test"}}]},
        {"fields": [{"label": "地址", "value": "plain", "format": "markdown"}]},
        {"sections": [{"label": "正文", "text": "<script>", "format": "html"}]},
        {"fields": [{"label": "", "value": "plain"}]},
        {"sections": [{"label": "正文", "text": "x" * 64001}]},
        {"sections": [{"label": "正文", "text": "x" * 64000}] * 3},
    ],
)
def test_malformed_review_is_rejected_at_the_http_boundary(details):
    with pytest.raises(ValidationError):
        public_card({"pending_confirm": {"q": "Review", "details": details}})


def test_plain_sections_preserve_unicode_and_clear_values():
    details = ConfirmationDetails.model_validate(
        {
            "fields": [{"label": "名称", "value": "😀" * 16000}],
            "sections": [{"label": "清空摘要", "text": ""}],
        }
    )
    assert details.sections[0].format == "plain"


def test_assertions_and_judge_use_the_displayed_content_without_promoting_drafts(tmp_path):
    details = {
        "fields": [{"label": "收件人", "value": "账号11"}],
        "sections": [{"label": "正文", "text": "这是一条待核实的规则。", "format": "markdown"}],
    }
    path = tmp_path / "trace.json"
    path.write_text(json.dumps({"input": {"message": "Prepare"}, "events": []}))
    result = {
        "trace": str(path),
        "native_events": [],
        "text": "请查看卡片",
        "http_events": [{"event": "confirmation", "data": {"q": "确认发送？", "details": details}}],
    }
    assert not native_assertions.check(
        {"require_confirm_preview": ["正文：\\n这是一条待核实的规则。"]}, result
    )
    material = online_judge.material({"id": "structured-review"}, [result])
    turn = json.loads(material)["turns"][0]
    assert not turn["validated_preparation_facts"]
    assert (
        confirmation_text(result["http_events"][0]["data"])
        in turn["confirmation_previews_to_review"]
    )
    online_judge.validate_excerpts(
        {"unsupported": ["这是一条待核实的规则。"], "reason": "无来源"}, material
    )
    result["http_events"][0]["data"]["details"]["sections"][0]["text"] = "错误正文"
    assert native_assertions.check({"require_confirm_preview": ["这是一条待核实的规则。"]}, result)
