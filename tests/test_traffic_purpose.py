"""Evaluation annotations preserve total cost and never rewrite original evidence."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent import hostinfo, reports
from eval import daily_metrics, request_metrics, traffic_purpose

BUILD = "a" * 40
IDENT = "1" * 32


def trace_doc(identity=IDENT, incoming=100):
    return {"trace_id": identity, "user_id": 7, "reply": "synthetic reply",
        "end_reason": "producer_done", "duration_s": 1,
        "input": {"source": "production", "principal_source": "assertion",
                  "principal_role": "user", "conversation_id": 42,
                  "runtime": {"name": "langchain", "build_sha": BUILD, "model": "fixture"}},
        "events": [{"node": "budget", "event": "task_restored", "task_id": "t_" + identity},
                   {"node": "native", "event": "llm_start"},
                   {"node": "native", "event": "llm_done", "input": incoming, "output": 5,
                    "cache_read": 80, "provider_cost_usd": incoming / 10000}]}


def evidence(tmp_path, *, captured=True):
    production = tmp_path / "production"
    production.mkdir()
    paths = []
    for identity, incoming in [(IDENT, 100), ("2" * 32, 200)]:
        path = production / f"20261107T000000_7_{identity[:8]}.json"
        path.write_text(json.dumps(trace_doc(identity, incoming)))
        paths.append(str(path))
    folder = tmp_path / "run"
    (folder / "traces").mkdir(parents=True)
    capture = folder / "traces" / (IDENT + ".json")
    capture.write_text(json.dumps(trace_doc(), ensure_ascii=False, indent=2) + "\n")
    report = folder / "report.json"
    row = {"request_id": IDENT, "transport": "blog_http",
           "trace": str(capture) if captured else "",
           "trace_sha256": hashlib.sha256(capture.read_bytes()).hexdigest() if captured else None}
    report.write_text(json.dumps({"phase": "full", "suite_sha256": "c" * 64,
                                 "service_version": {"sha": BUILD}, "requests": [row]}))
    return paths, report, capture


def amount(report, field):
    return sum(group[field] for group in report["groups"])


def test_exact_annotations_add_views_without_hiding_cost_or_changing_evidence(tmp_path):
    paths, report, capture = evidence(tmp_path)
    files = [Path(path) for path in paths] + [report, capture]
    before = {path: path.read_bytes() for path in files}
    measured = traffic_purpose.annotate(paths, [report, report], request_metrics.collect)
    assert measured["traffic_scope"] == "all_traffic" and measured["includes_evaluations"]
    assert amount(measured, "in") == 300 and amount(measured, "provider_cost_usd") == 0.03
    known = measured["views"]["known_evaluation"]
    other = measured["views"]["excluding_known_evaluation"]
    assert amount(known, "in") == 100 and amount(other, "in") == 200
    assert amount(known, "provider_cost_usd") + amount(other, "provider_cost_usd") == 0.03
    assert all(group["source"] == "production" for group in measured["groups"])
    assert measured["evaluation_annotation"]["canonical_reports"] == 1
    assert measured["evaluation_annotation"]["registered_request_ids"] == 1
    assert {path: path.read_bytes() for path in files} == before


@pytest.mark.parametrize("change", [
    {"trace_id": "1" * 8 + "f" * 24}, {"trace_id": [IDENT]},
    {"user_id": 99}, {"reply": "different evidence"},
    {"input": {"source": "production", "runtime": {"build_sha": "b" * 40}}},
])
def test_nonmatching_trace_is_not_silently_excluded(tmp_path, change):
    paths, report, _ = evidence(tmp_path)
    Path(paths[0]).write_text(json.dumps({**trace_doc(), **change}))
    measured = traffic_purpose.annotate(paths, [report], request_metrics.collect)
    assert not measured["views"]["known_evaluation"]["groups"]
    assert amount(measured["views"]["excluding_known_evaluation"], "in") == 300
    assert amount(measured, "provider_cost_usd") == 0.03
    assert measured["evaluation_annotation"]["conflicting_trace_files"] == int(
        change.get("trace_id", IDENT) == IDENT)


@pytest.mark.parametrize("fault", ["checksum", "capture_id", "capture_build", "report_build",
                                  "request_id", "duplicate_id", "not_canonical"])
def test_invalid_canonical_evidence_is_rejected_without_rewriting_it(tmp_path, fault):
    paths, report, capture = evidence(tmp_path)
    doc = json.loads(report.read_text())
    raw = json.loads(capture.read_text())
    if fault == "checksum":
        capture.write_text(" "+capture.read_text())
    elif fault in {"capture_id", "capture_build"}:
        if fault == "capture_id":
            raw["trace_id"] = "9" * 32
        else:
            raw["input"]["runtime"]["build_sha"] = "b" * 40
        capture.write_text(json.dumps(raw))
        doc["requests"][0]["trace_sha256"] = hashlib.sha256(capture.read_bytes()).hexdigest()
    elif fault == "report_build":
        doc["service_version"]["sha"] = "b" * 40
    elif fault == "request_id":
        doc["requests"][0]["request_id"] = IDENT[:8]
    elif fault == "duplicate_id":
        doc["requests"].append(doc["requests"][0])
    else:
        doc = {"requests": doc["requests"]}
    report.write_text(json.dumps(doc))
    before = report.read_bytes(), capture.read_bytes()
    with pytest.raises(ValueError):
        traffic_purpose.partition(paths, [report])
    assert before == (report.read_bytes(), capture.read_bytes())


def test_registered_attempt_without_capture_still_identifies_its_exact_request(tmp_path):
    paths, report, _ = evidence(tmp_path, captured=False)
    known, remaining, meta = traffic_purpose.partition(paths, [report])
    assert known == paths[:1] and remaining == paths[1:] and meta["registered_request_ids"] == 1
    report2 = tmp_path / "copy.json"
    report2.write_bytes(report.read_bytes())
    assert traffic_purpose.partition(paths, [report, report2])[2]["registered_request_ids"] == 1
    doc = json.loads(report2.read_text())
    doc["service_version"]["sha"] = "b" * 40
    report2.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="conflicting"):
        traffic_purpose.partition(paths, [report, report2])


def test_absent_annotations_and_unreadable_files_remain_all_traffic(tmp_path):
    paths, _, _ = evidence(tmp_path)
    broken = tmp_path / "broken.json"
    broken.write_text("not JSON")
    measured = traffic_purpose.annotate([*paths, str(broken)], [], request_metrics.collect)
    assert "views" not in measured and measured["unreadable"] == 1
    assert measured["traffic_scope"] == "all_traffic" and amount(measured, "in") == 300
    assert "not verified human" in measured["evaluation_annotation"]["limitation"]
    assert "含评测" in daily_metrics.render(paths, 1)


@pytest.mark.parametrize("mode", ["daily_json", "daily_tasks", "task_cli"])
def test_real_cli_accepts_repeated_reports_and_keeps_all_traffic(tmp_path, mode):
    paths, report, _ = evidence(tmp_path)
    output = tmp_path / "tasks.json"
    if mode == "task_cli":
        args = ["eval.task_metrics", "--dir", str(Path(paths[0]).parent), "--out", str(output)]
    else:
        args = ["eval.daily_metrics", "--dir", str(Path(paths[0]).parent), "--days", "0",
                "--json" if mode == "daily_json" else "--tasks"]
    completed = subprocess.run([sys.executable, "-m", *args,
        "--evaluation-report", str(report), "--evaluation-report", str(report)], check=True,
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
        env={**os.environ, "SAUDADE_IGNORE_ENV_FILE": "1"})
    measured = json.loads(output.read_text() if mode == "task_cli" else completed.stdout)
    assert measured["traffic_scope"] == "all_traffic"
    assert measured["evaluation_annotation"]["registered_request_ids"] == 1
    if mode == "daily_json":
        assert amount(measured, "provider_cost_usd") == 0.03
    else:
        assert measured["coverage"]["requests"] == 2
        assert measured["views"]["known_evaluation"]["coverage"]["requests"] == 1
        assert measured["views"]["excluding_known_evaluation"]["coverage"]["requests"] == 1


def test_host_statistics_are_explicitly_all_traffic_without_reading_reports(tmp_path, monkeypatch):
    paths, _, _ = evidence(tmp_path)
    monkeypatch.setattr(hostinfo, "TRACES_DIR", str(Path(paths[0]).parent))
    stats = hostinfo.trace_stats()
    assert stats["traffic_scope"] == "all_traffic" and stats["includes_evaluations"]
    assert stats["rounds"] == 2
    rendered = reports.render_service_health(services=[], health={}, traces=stats, sizes=[])
    assert "今日服务请求（含评测）" in rendered and "今日对话" not in rendered


@pytest.mark.parametrize("conflicting", [False, True])
def test_overlapping_captures_are_idempotent_or_conflicting(tmp_path, conflicting):
    import shutil

    paths, report, _ = evidence(tmp_path)
    copied = tmp_path / "second"
    shutil.copytree(report.parent, copied)
    second = copied / "report.json"
    if conflicting:
        capture = copied / "traces" / (IDENT + ".json")
        capture.write_text(json.dumps({**trace_doc(), "reply": "conflicting capture"}))
        doc = json.loads(second.read_text())
        doc["requests"][0]["trace_sha256"] = hashlib.sha256(capture.read_bytes()).hexdigest()
        second.write_text(json.dumps(doc))
        with pytest.raises(ValueError, match="conflicting"):
            traffic_purpose.partition(paths, [report, second])
    else:
        known, _, meta = traffic_purpose.partition(paths, [report, second])
        assert len(known) == meta["registered_request_ids"] == 1
        assert meta["canonical_reports"] == 2


@pytest.mark.parametrize("target", ["report", "capture", "production"])
def test_task_cli_cannot_overwrite_input_evidence(tmp_path, target):
    paths, report, capture = evidence(tmp_path)
    output = {"report": report, "capture": capture, "production": Path(paths[0])}[target]
    before = output.read_bytes()
    completed = subprocess.run([sys.executable, "-m", "eval.task_metrics", "--dir",
        str(Path(paths[0]).parent), "--out", str(output), "--evaluation-report", str(report)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
        env={**os.environ, "SAUDADE_IGNORE_ENV_FILE": "1"})
    assert completed.returncode != 0 and "must not overwrite" in completed.stderr
    assert output.read_bytes() == before


def test_unknown_cost_stays_unknown_in_all_traffic_and_evaluation_views(tmp_path):
    paths, report, capture = evidence(tmp_path)
    doc = trace_doc()
    doc["events"][-1].pop("provider_cost_usd")
    Path(paths[0]).write_text(json.dumps(doc))
    capture.write_text(json.dumps(doc))
    manifest = json.loads(report.read_text())
    manifest["requests"][0]["trace_sha256"] = hashlib.sha256(capture.read_bytes()).hexdigest()
    report.write_text(json.dumps(manifest))
    measured = traffic_purpose.annotate(paths, [report], request_metrics.collect)
    assert measured["groups"][0]["provider_cost_usd"] is None
    assert measured["views"]["known_evaluation"]["groups"][0]["provider_cost_usd"] is None
    assert amount(measured["views"]["excluding_known_evaluation"], "provider_cost_usd") == 0.02
