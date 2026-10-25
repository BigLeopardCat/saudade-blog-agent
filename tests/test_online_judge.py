"""Semantic review preserves evidence boundaries and never converts errors to passes."""

import json
from pathlib import Path

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


def test_unanswered_request_cannot_be_normalized_into_semantic_success(tmp_path):
    model = Model({"unsupported": [], "answered": False, "verdict": "ok", "reason": "没有办理"})
    result = run(tmp_path, recorded(tmp_path), model)
    assert result["rows"][0]["status"] == "suspect"
    assert result["rows"][0]["verdict"]["verdict"] == "suspect"


def test_semantic_result_cannot_replace_deterministic_failure(tmp_path):
    case = recorded(tmp_path)
    case["status"] = "failed"
    result = run(tmp_path, case, Model())
    assert result["rows"][0]["deterministic_status"] == "failed"
    assert result["rows"][0]["status"] == "no_issue_found"
    assert "passed" not in result["counts"]


@pytest.mark.parametrize("failure", ["image", "truncated", "missing_trace", "large"])
def test_incomplete_material_is_not_evaluated_and_does_not_spend_tokens(tmp_path, failure):
    case = recorded(tmp_path)
    result = case["result"]["rounds"][0]
    path = Path(result["trace"])
    trace = json.loads(path.read_text())
    if failure == "image":
        trace["input"]["has_image"] = True
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
