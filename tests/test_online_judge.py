"""Semantic review preserves evidence boundaries and never converts errors to passes."""

import json
import secrets
from os.path import commonprefix
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage
from openai import OpenAIError

from eval import llm_judge, online_judge
from utils.trace import truncation_mark


class Model:
    model_name = "fixture-judge"

    def __init__(self, verdict=None, *, error=None, finish="stop"):
        self.verdict = verdict or {
            "unsupported": [], "answered": True, "verdict": "ok", "reason": "有依据"}
        self.error, self.finish = error, finish
        self.messages = []

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        self.messages.append(messages)
        if self.error:
            raise self.error
        return AIMessage(content=json.dumps(self.verdict, ensure_ascii=False),
                         response_metadata={"finish_reason": self.finish},
                         usage_metadata={"input_tokens": 80, "output_tokens": 20,
                                         "total_tokens": 100})


def recorded(tmp_path, *, trace_extra=None, text="通知正文", **result_extra):
    trace = {"input": {"message": "给账号发通知", "principal_role": "admin"},
             "events": [{"node": "native", "event": "call", "name": "list_accounts",
                         "kind": "ok", "result": '{"role":"user"}'}]}
    if trace_extra:
        trace.update(trace_extra)
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(trace))
    result = {"trace": str(path), "text": text, "exec_rows": [], "error": None,
              "confirm_payloads": [{"specs": [{"tool": "send_user_notice", "args": {
                  "intent": {"content": "活跃后恢复权限", "target": {"id": 20}}}}]}],
              **result_extra}
    return {"id": "notice", "status": "passed", "result": {"rounds": [result]}}


def run(tmp_path, case, model):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"cases": [case], "service_version": {"sha": "fixture"}}))
    before = path.read_bytes()
    result = online_judge.review(path, tmp_path / "semantic", model)
    assert path.read_bytes() == before
    return result


def test_prepared_text_is_reviewed_as_output_not_used_as_supporting_fact(tmp_path):
    case = recorded(tmp_path)
    model = Model({"unsupported": ["活跃后恢复权限"], "answered": True,
                   "verdict": "suspect", "reason": "工具只返回当前身份，无恢复规则"})
    report = run(tmp_path, case, model)
    data = json.loads(model.messages[0][1]["content"])
    turn = data["turns"][0]
    assert turn["tool_observations"][0]["result"] == '{"role":"user"}'
    assert turn["proposed_actions_not_facts"][0]["args"]["intent"]["content"] == "活跃后恢复权限"
    assert not turn["execution_receipts"]
    assert report["rows"][0]["status"] == "suspect"
    assert report["rows"][0]["diagnostics"]["usage"]["total_tokens"] == 100
    assert report["counts"] == {"suspect": 1}
    assessment = json.loads((tmp_path / "semantic" / "assessment.json").read_text())
    assert assessment["counts"] == {"needs_review": 1}


def test_visible_card_text_is_reviewed_even_when_absent_from_signed_intent(tmp_path):
    preview = "账号已经降级。\n确认发送？"
    canary = secrets.token_urlsafe(24)
    case = recorded(tmp_path, text="请核对卡片", http_events=[
        {"event": "confirmation", "data": {"q": preview, "token": canary}},
        {"event": "progress", "data": {"q": "不能当作确认卡"}},
    ])
    model = Model({"unsupported": ["账号已经降级"], "answered": True,
                   "verdict": "suspect", "reason": "账号仍为普通用户，且没有执行回执"})
    report = run(tmp_path, case, model)
    data = json.loads(model.messages[0][1]["content"])
    current = data["turns"][-1]
    assert current["confirmation_previews_to_review"] == [preview]
    assert current["execution_receipts"] == []
    assert canary not in str(model.messages)
    assert report["counts"] == {"suspect": 1}


def test_judge_cannot_quote_a_previous_turn_card_as_current_output(tmp_path):
    case = recorded(tmp_path, http_events=[
        {"event": "confirmation", "data": {"q": "先前卡片"}}])
    first = case["result"]["rounds"][0]
    case["result"]["rounds"].append({**first, "http_events": [
        {"event": "confirmation", "data": {"q": "本轮卡片"}}]})
    text = online_judge.material(case, case["result"]["rounds"])
    with pytest.raises(ValueError, match="excerpts"):
        online_judge.validate_excerpts({"unsupported": ["先前卡片"], "reason": "无回执"}, text)


def test_unanswered_request_cannot_be_normalized_into_semantic_success(tmp_path):
    model = Model({"unsupported": [], "answered": False, "verdict": "ok", "reason": "没有办理"})
    result = run(tmp_path, recorded(tmp_path), model)
    assert result["rows"][0]["status"] == "suspect"
    assert result["rows"][0]["verdict"]["verdict"] == "suspect"


def test_captured_voice_reaches_review_without_replacing_other_product_facts(tmp_path):
    events = [
        {"node": "native", "event": "product_context", "facts": {
            "article_url_template": "https://site.test/article/<id>"}},
        {"node": "native", "event": "product_context", "facts": {
            "conversation_style": "日式雌小鬼风格，称对方杂鱼，不用敬语。"}},
    ]
    case = recorded(tmp_path, trace_extra={"input": {"message": "早呀", "principal_role": "zako"},
                    "events": events}, text="尊敬的主人，欢迎光临。", confirm_payloads=[])
    model = Model({"unsupported": [], "answered": False, "verdict": "suspect",
                   "reason": "称呼与记录中的当前角色语气冲突"})
    report = run(tmp_path, case, model)
    current = json.loads(model.messages[0][1]["content"])["turns"][0]
    facts = current["runtime_context"]["product_facts"]
    assert facts == {**events[0]["facts"], **events[1]["facts"]}
    assert report["counts"] == {"suspect": 1}
    assert report["rows"][0]["verdict"]["unsupported"] == []


def test_missing_historical_voice_stays_unknown():
    assert online_judge.runtime_context([])["product_facts"] is None


def test_recorded_browser_state_is_evidence_without_becoming_authorization(tmp_path):
    page = {"current_url": "/article/23", "page_title": "Asyncio",
            "current_darkmode": "on", "current_effects": "sakura"}
    case = recorded(tmp_path, trace_extra={"input": {"message": "我现在在哪？",
                    "principal_role": "user", "page_context": page}}, text="你在 Asyncio 文章页。")
    model = Model()
    run(tmp_path, case, model)
    turn = json.loads(model.messages[0][1]["content"])["turns"][0]
    assert turn["reported_browser_state_not_authority"] == page
    assert turn["runtime_context"]["caller"] is None
    assert turn["runtime_context"]["permissions"] is None
    assert turn["execution_receipts"] == []


@pytest.mark.parametrize("truncated", [False, True])
def test_correlated_full_http_request_reaches_judge_without_expanding_source_log(
    tmp_path, truncated
):
    full = "给账号发通知，正文如下：" + "内容" * 150
    case = recorded(tmp_path, trace_extra={"trace_id": "request-a", "input": {
        "message": full[:200] if truncated else full, "message_truncated": truncated}},
        request_evidence={"request_id": "request-a", "message": full})
    trace = Path(case["result"]["rounds"][0]["trace"])
    before = trace.read_bytes()
    model = Model()
    report = run(tmp_path, case, model)
    assert report["counts"] == {"no_issue_found": 1}
    assert json.loads(model.messages[0][1]["content"])["turns"][0]["user_request"] == full
    assert trace.read_bytes() == before


@pytest.mark.parametrize("evidence", [
    {"request_id": "other", "message": "原请求全文"},
    {"request_id": "request-a", "message": "另一条请求全文"},
    {"request_id": "request-a", "message": None},
])
def test_mismatched_request_projection_is_not_semantic_evidence(tmp_path, evidence):
    case = recorded(tmp_path, trace_extra={"trace_id": "request-a", "input": {
        "message": "原请求", "message_truncated": True}}, request_evidence=evidence)
    model = Model()
    assert run(tmp_path, case, model)["counts"] == {"not_evaluated": 1}
    assert not model.messages


def test_semantic_result_cannot_replace_deterministic_failure(tmp_path):
    case = recorded(tmp_path)
    case["status"] = "failed"
    result = run(tmp_path, case, Model())
    assert result["rows"][0]["deterministic_status"] == "failed"
    assert result["rows"][0]["status"] == "no_issue_found"
    assert "passed" not in result["counts"]


@pytest.mark.parametrize("failure", ["image", "truncated", "request", "missing_trace", "large"])
def test_incomplete_material_is_not_evaluated_and_does_not_spend_tokens(tmp_path, failure):
    case = recorded(tmp_path)
    result = case["result"]["rounds"][0]
    path = Path(result["trace"])
    trace = json.loads(path.read_text())
    if failure == "image":
        trace["input"]["has_image"] = True
    elif failure == "request":
        trace["input"]["message_truncated"] = True
    elif failure == "truncated":
        trace["events"][0]["result"] += truncation_mark(99999)
    elif failure == "large":
        result["text"] = "x" * online_judge.MAX_MATERIAL_CHARS
    path.write_text(json.dumps(trace))
    if failure == "missing_trace":
        path.unlink()
    model = Model()
    report = run(tmp_path, case, model)
    assert report["counts"] == {"not_evaluated": 1} and not model.messages


@pytest.mark.parametrize("error,finish", [(OpenAIError("unavailable"), "stop"),
                                         (None, "length")])
def test_judge_failure_does_not_retry_or_fall_back_to_a_pass(tmp_path, error, finish):
    model = Model(error=error, finish=finish)
    report = run(tmp_path, recorded(tmp_path), model)
    assert report["counts"] == {"not_evaluated": 1}
    assert len(model.messages) == 1


def test_judge_cannot_invent_its_own_evidence_quote(tmp_path):
    model = Model({"unsupported": ["根本不存在的说法"], "answered": True,
                   "verdict": "suspect", "reason": "无出处"})
    report = run(tmp_path, recorded(tmp_path), model)
    assert report["counts"] == {"not_evaluated": 1}


def test_history_is_included_but_cannot_be_quoted_as_current_output(tmp_path):
    case = recorded(tmp_path, text="先前错误")
    first = case["result"]["rounds"][0]
    case["result"]["rounds"].append({**first, "text": "本轮已改正", "confirm_payloads": []})
    text = online_judge.material(case, case["result"]["rounds"])
    data = json.loads(text)
    assert data["current_turn"] == 2 and len(data["turns"]) == 2
    with pytest.raises(ValueError, match="excerpts"):
        online_judge.validate_excerpts({"unsupported": ["先前错误"], "reason": "错误"}, text)


def test_model_override_changes_the_actual_client_not_just_report_label(monkeypatch):
    import models.llm

    captured = {}
    monkeypatch.setattr(models.llm, "get_llm", lambda **kwargs: captured.update(kwargs))
    llm_judge.make_llm(model="independent-judge", max_retries=0)
    assert captured["model"] == "independent-judge" and captured["max_retries"] == 0


@pytest.mark.parametrize("status,code", [("checks_passed", 0), ("needs_review", 1),
                                        ("failed", 1), ("not_evaluated", 2)])
def test_cli_does_not_report_success_for_failed_or_incomplete_assessment(monkeypatch, status, code):
    import sys

    monkeypatch.setattr(sys, "argv", ["online_judge", "--report", "unused", "--out", "unused"])
    monkeypatch.setattr(llm_judge, "make_llm", lambda **kwargs: None)
    monkeypatch.setattr(online_judge, "review", lambda *args, **kwargs: {
        "semantic_status": "no_issue_found", "counts": {"no_issue_found": 1},
        "assessment_counts": {status: 1}})
    assert online_judge.main() == code


@pytest.mark.parametrize("model,format_type", [("deepseek-v4.1-flash", "json_object"),
                                               ("qwen3.8-flash", "json_schema")])
def test_gateway_format_uses_the_actual_judge_model(monkeypatch, model, format_type):
    from models.llm import get_llm

    monkeypatch.setattr("config.settings.llm_provider", "opencode")
    # An override must select its own dialect even if the configured default differs.
    monkeypatch.setattr("config.settings.opencode_model", "other-default")
    requests = []
    verdict = {"unsupported": [], "answered": True, "verdict": "ok", "reason": "有依据"}

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == model and body["response_format"]["type"] == format_type
        return httpx.Response(200, json={"id": "fixture", "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(verdict)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})

    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        llm = get_llm(model=model, api_key="test-only", base_url="https://model.test/v1",
                      http_client=client, max_retries=0, streaming=False, enable_thinking=False)
        actual, degraded = llm_judge.judge_one("合成材料", llm, allow_fallback=False)
    assert actual == verdict and not degraded and len(requests) == 1


def test_repeated_catalogs_are_losslessly_shared_without_merging_changed_menus(tmp_path):
    contract = {"node": "native", "event": "tool_contract", "schema": {"function": {
        "name": "read_alpha", "description": "Authorized fixture. " * 100}}}
    case = recorded(tmp_path, trace_extra={"events": [contract]})
    first = case["result"]["rounds"][0]
    second_path = tmp_path / "second.json"
    second_trace = json.loads(Path(first["trace"]).read_text())
    second_trace["events"].append({"node": "native", "event": "tool_contract",
                                   "schema": {"function": {"name": "read_beta"}}})
    second_path.write_text(json.dumps(second_trace))
    rounds = [first, first, {**first, "trace": str(second_path)}]
    text = online_judge.material(case, rounds)
    data = json.loads(text)
    catalogs = data["tool_catalogs"]
    assert len(catalogs) == 2
    for source, turn in zip(rounds, data["turns"]):
        trace = json.loads(Path(source["trace"]).read_text())
        expected = online_judge.runtime_context(trace["events"])
        context = dict(turn["runtime_context"])
        context["available_tools"] = catalogs[context.pop("available_tools_ref")]
        assert context == expected
        assert turn["assistant_reply_to_review"] == source["text"]
    assert len(text) < len(json.dumps(data, ensure_ascii=False, indent=2))


def test_next_review_reuses_the_complete_prior_turn_before_variable_metadata(tmp_path):
    case = recorded(tmp_path, text="EARLIER_REPLY_" * 100)
    first = case["result"]["rounds"][0]
    before = online_judge.material(case, [first])
    next_path = tmp_path / "next.json"
    trace = json.loads(Path(first["trace"]).read_text())
    trace["input"]["message"] = "第二轮问题"
    trace["events"].append({"node": "native", "event": "tool_contract", "schema": {
        "function": {"name": "new_catalog_tool", "description": "Added on this turn."}}})
    next_path.write_text(json.dumps(trace))
    second = {**first, "trace": str(next_path), "text": "SECOND_REPLY"}
    after = online_judge.material(case, [first, second])
    first_turn = json.loads(before)["turns"][0]
    encoded_turn = json.dumps(first_turn, ensure_ascii=False, separators=(",", ":"))
    boundary = before.index(encoded_turn) + len(encoded_turn)
    assert len(commonprefix([before, after])) >= boundary
    data = json.loads(after)
    assert data["current_turn"] == 2 and data["turns"][0] == first_turn
    assert data["turns"][1]["assistant_reply_to_review"] == "SECOND_REPLY"
    assert len(data["tool_catalogs"]) == 2


def test_only_correlated_operation_facts_reach_semantic_review(tmp_path):
    fact = {"status": "cancelled", "scope": "operation", "executed": False,
            "business_state": "not_observed"}
    events = [
        {"node": "native", "event": "call", "name": "cancel_pending_operation",
         "kind": "cancelled", "call_id": "cancel", "round": 0},
        {"node": "native", "event": "operation_observation", "call_id": "cancel",
         "round": 0, "facts": fact},
        {"node": "native", "event": "operation_observation", "call_id": "unrelated",
         "round": 0, "facts": {"PRIVATE_CANARY": "not a matching result"}},
        {"node": "untrusted", "event": "operation_observation", "call_id": "cancel",
         "round": 0, "facts": {"PRIVATE_CANARY": "not runtime evidence"}},
    ]
    case = recorded(tmp_path, trace_extra={"events": events}, text="已取消，没有执行。",
                    confirm_payloads=[])
    text = online_judge.material(case, case["result"]["rounds"])
    turn = json.loads(text)["turns"][0]
    assert turn["validated_operation_facts"] == [events[1]]
    assert "PRIVATE_CANARY" not in text


def test_native_confirmation_preview_is_reviewed_without_becoming_supporting_evidence(tmp_path):
    preview = "仅标记本次确认的通知编号101；之后新到通知不包含在内。"
    case = recorded(tmp_path, text="请核对卡片。", confirmation_previews=[preview])
    model = Model()
    report = run(tmp_path, case, model)
    current = json.loads(model.messages[0][1]["content"])["turns"][0]
    assert current["confirmation_previews_to_review"] == [preview]
    assert preview not in str(current["tool_observations"])
    assert preview not in str(current["validated_preparation_facts"])
    assert report["rows"][0]["status"] == "no_issue_found"
