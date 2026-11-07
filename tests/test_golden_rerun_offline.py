"""Diagnostic retries retain first-attempt results, history scope and credential redaction."""

import json
import sys
import types

import pytest

from eval import run_golden as rg


def mkcase(cid, tags):
    return {"id": cid, "tags": tags, "user_input": f"问题-{cid}", "context": {},
            "gold": {"text_contains": ["回答"]}}


def approval_case(cid):
    return {"id": cid, "tags": ["write"], "context": {"conversation_id": 4242},
            "user_input": "删除指定测试分类", "rounds": [
                {"round": 1, "gold": {"text_contains": ["回答"]}},
                {"round": 2, "confirm_message": "确认删除", "gold": {"text_contains": ["回答"]}},
            ]}


def _stub_run_one(plan: dict, calls: list, tokens: dict | None = None):
    """桩：按 `plan[case_id]` 顺次弹出这一跑判绿(True)/判红(False)，并记录收到的参数。"""

    def _run(req, principal=None, trace_ctx=None, *, confirm_token="", confirm_pick="",
             thread=""):
        cid = (trace_ctx or {}).get("case", "")
        base = cid[: -len("__rerun")] if cid.endswith("__rerun") else cid
        calls.append({"case": cid, "message": req.message, "confirm_token": confirm_token,
                      "confirm_pick": confirm_pick,
                      "conversation_id": req.conversation_id,
                      "principal": (principal.uid, principal.role) if principal else None})
        seq = plan.setdefault(base, [])
        ok = seq.pop(0) if seq else False
        tseq = (tokens or {}).setdefault(base, [])
        tok = tseq.pop(0) if tseq else ""
        frame = "" if not tok else "__CONFIRM__:" + json.dumps(
            {"token": tok, "q": "要不要执行？"}, ensure_ascii=False)
        return {"text": "回答" if ok else "跑偏", "commands": [], "tool_calls": [],
                "frames": [frame] if frame else [],
                "exec_rows": [], "exec_tools": [], "tool_rounds": 0,
                "trace": None, "resets": 0 if ok else 1,
                "resets_reasons": [] if ok else ["stub 判红"],
                "reset_scopes": [] if ok else ["all"],
                "fallback_reasons": [], "error": None,
                "confirm_tokens": [tok] if tok else [],
                "confirm_payloads": ([{"workflow": "tag_delete",
                                       "specs": [{"tool": "delete_tag"}]}] if tok else [])}

    return _run


@pytest.fixture
def drive(tmp_path, monkeypatch):
    corpus = tmp_path / "eval/golden/basic.jsonl"
    corpus.parent.mkdir(parents=True)

    def run(cases, plan, *, argv=(), tokens=None):
        corpus.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases))
        calls = []
        fake = types.ModuleType("corpus_check")
        fake.presence_check = lambda: {}
        with monkeypatch.context() as patch:
            patch.chdir(tmp_path)
            patch.setitem(sys.modules, "corpus_check", fake)
            patch.setenv("GOLDEN_NO_TRACE", "1")
            patch.setenv("TRACE_TOOL_RESULT_LIMIT", "8000")
            patch.delenv("GOLDEN_ALLOW_REAL_WRITE", raising=False)
            patch.setattr(rg, "ensure_agent", lambda: None)
            patch.setattr(rg, "judge_corpus", lambda: None)
            patch.setattr(rg, "run_one", _stub_run_one(plan, calls, tokens))
            patch.setattr(sys, "argv", ["run_golden.py", *argv])
            with pytest.raises(SystemExit) as stopped:
                rg.main()
        paths = sorted((tmp_path / "eval/report/runs").glob("*.json"))
        report = json.loads(paths[-1].read_text()) if paths else None
        baseline = tmp_path / "eval/report/last_run.json"
        reviews = sorted((tmp_path / "eval/report").glob("review_*.md"))
        return {"code": stopped.value.code, "report": report, "calls": calls,
                "paths": paths,
                "baseline": json.loads(baseline.read_text()) if baseline.exists() else None,
                "review": reviews[-1].read_text() if reviews else ""}

    return run


@pytest.mark.parametrize("retry", [False, True])
def test_failed_diagnostic_attempt_cannot_be_replaced_by_a_successful_retry(drive, retry):
    result = drive([mkcase("regression", ["regression"])],
                   {"regression": [False, True]}, argv=["--rerun-failures"] if retry else [])
    report = result["report"]
    assert result["code"] == 1 and report["failed"] == report["failed_first_run"] == 1
    assert report["passed"] == 0 and report["regression"]["all_passed"] is False
    row = report["cases"][0]
    assert row["ok"] is row["final_ok"] is False and row["text"] == "跑偏"
    assert len(result["calls"]) == (2 if retry else 1)
    if retry:
        assert row["rerun"]["ok"] is True and row["rerun"]["text"] == "回答"
        assert report["regression"]["flaked_ids"] == ["regression"]
        assert result["calls"][0]["principal"] == result["calls"][1]["principal"]
        assert result["calls"][0]["message"] == result["calls"][1]["message"]
    else:
        assert row["rerun"] is None
    assert "不得放行" in result["review"]
    assert "landing" not in report


def test_small_mixed_sample_with_a_failure_is_never_reported_as_success(drive):
    result = drive([mkcase("good", ["chat"]), mkcase("bad", ["chat"])],
                   {"good": [True], "bad": [False]})
    assert result["code"] == 1 and result["report"]["pass_rate"] == 0.5
    assert len(result["calls"]) == 2


def test_successful_run_has_no_retry_and_preserves_complete_baseline(drive):
    result = drive([mkcase("one", ["chat"]), mkcase("two", ["regression"])],
                   {"one": [True], "two": [True]})
    assert result["code"] == 0 and result["report"]["failed"] == 0
    assert all(row["rerun"] is None for row in result["report"]["cases"])
    assert result["baseline"]["total"] == 2 and result["baseline"]["full_run"] is True
    assert result["review"] == ""
    partial = drive([mkcase("one", ["chat"])], {"one": [True]}, argv=["--only", "one"])
    assert partial["code"] == 0 and partial["report"]["full_run"] is False
    assert partial["report"]["total"] == 1 and partial["baseline"]["total"] == 2
    assert len(partial["paths"]) == 2


def test_custom_corpus_does_not_replace_default_baseline(drive, tmp_path):
    case = mkcase("custom", ["chat"])
    custom = tmp_path / "custom.jsonl"
    custom.write_text(json.dumps(case) + "\n")
    result = drive([case], {"custom": [True]}, argv=["--golden", str(custom)])
    assert result["code"] == 0 and result["baseline"] is None
    assert result["report"]["golden_file"] == str(custom) and not result["report"]["full_run"]


def test_approval_uses_the_previous_rounds_token_and_redacts_persisted_frames(drive):
    result = drive([approval_case("write")], {"write": [True, True]},
                   tokens={"write": ["synthetic-confirmation-token"]})
    assert result["code"] == 0 and len(result["calls"]) == 2
    assert [row["confirm_token"] for row in result["calls"]] == ["", "synthetic-confirmation-token"]
    assert [row["conversation_id"] for row in result["calls"]] == [4242, 4242]
    rounds = result["report"]["cases"][0]["rounds"]
    assert "synthetic-confirmation-token" not in json.dumps(rounds)
    assert "<redacted>" in json.dumps(rounds)


def test_missing_approval_token_never_executes_following_round(drive):
    result = drive([approval_case("write")], {"write": [True, True]})
    assert result["code"] == 1 and len(result["calls"]) == 1
    assert any("确认令牌" in reason for reason in result["report"]["cases"][0]["fails"])


def test_real_writes_require_explicit_opt_in_and_are_reported_separately(drive):
    approved = mkcase("allowed", ["chat"])
    write = {**mkcase("write", ["write"]), "needs_real_write": True}
    result = drive([approved, write], {"allowed": [True]})
    assert result["code"] == 0 and result["report"]["total"] == 1
    assert result["report"]["skipped_ids"] == ["write"]
    assert result["report"]["skipped_real_write_ids"] == ["write"]
    assert [row["case"] for row in result["calls"]] == ["allowed"]
