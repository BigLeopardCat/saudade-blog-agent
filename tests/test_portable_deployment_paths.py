"""Independent deployments share configured diagnostic storage without host path discovery."""

import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def environment(**values):
    return {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT),
            "SAUDADE_IGNORE_ENV_FILE": "1", **values}


def python_at(directory, code, **values):
    result = subprocess.run([sys.executable, "-c", code], cwd=directory,
                            env=environment(**values), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


WRITE_AND_READ = '''
import json
from pathlib import Path
from datetime import datetime
from config.settings import settings
from agent import hostinfo as host
from agent.reports import render_service_health
from utils import trace
trace.start_trace("portable-fixture", 7, "fixture", {"source": "evaluation"})
saved = trace.finish_trace("portable-fixture", "producer_done", 0.01)
health = host.parse_health_log(host.read_text_tail(host.HEALTH_LOG), datetime.min)
stats = host.trace_stats()
print(json.dumps({"trace": str(Path(saved).resolve()), "root": settings.trace_dir,
    "host": host.BLOG_ROOT, "health": bool(health), "sizes": host.log_sizes(), "stats": stats,
    "report": render_service_health(services=[], health=health, traces=stats, sizes=[])}))
'''


def test_default_storage_is_local_and_unconfigured_host_logs_stay_unknown(tmp_path):
    deployment = tmp_path / "independent-agent"
    deployment.mkdir()
    (deployment / "health.log").write_text("must not be discovered")
    result = python_at(deployment, WRITE_AND_READ)
    assert result["root"] == "data/traces"
    assert pathlib.Path(result["trace"]).is_relative_to(deployment / "data/traces")
    assert result["host"] == "" and not result["health"] and result["sizes"] == []
    assert "心跳探针：读不到" in result["report"]
    assert "心跳探针（近 24 小时）：无异常" not in result["report"]
    assert result["stats"]["readable"] and result["stats"]["rounds"] == 1


def test_explicit_trace_and_host_paths_are_independent_and_retention_uses_the_same_root(tmp_path):
    deployment, host, traces = (tmp_path / name for name in ("release", "host", "trace-state"))
    deployment.mkdir()
    (host / "logs").mkdir(parents=True)
    (host / "logs/health.log").write_text("2026-01-01 12:00:00 WARN fixture\n")
    result = python_at(deployment, WRITE_AND_READ, BLOG_ROOT=str(host), TRACE_DIR=str(traces))
    assert pathlib.Path(result["trace"]).is_relative_to(traces)
    assert result["host"] == str(host) and result["health"]
    assert [row["path"] for row in result["sizes"]] == ["health.log"]
    audit = subprocess.run([sys.executable, "-m", "eval.trace_retention", "--json"],
                           cwd=deployment, env=environment(TRACE_DIR=str(traces)),
                           capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr
    summary = json.loads(audit.stdout)
    assert summary["root"] == str(traces) and summary["files"] == 1
    assert not summary["applied"] and pathlib.Path(result["trace"]).exists()
    metrics = subprocess.run([sys.executable, "-m", "eval.daily_metrics", "--json"],
                             cwd=deployment, env=environment(TRACE_DIR=str(traces)),
                             capture_output=True, text=True, timeout=30)
    assert metrics.returncode == 0, metrics.stderr
    assert sum(row["requests"] for row in json.loads(metrics.stdout)["groups"]) == 1
    override = tmp_path / "explicit-cli-root"
    override.mkdir()
    audit = subprocess.run([sys.executable, "-m", "eval.trace_retention", "--json",
                            "--dir", str(override)],
                           cwd=deployment, env=environment(TRACE_DIR=str(traces)),
                           capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr
    assert json.loads(audit.stdout)["root"] == str(override)
    roots = python_at(deployment, '''
import json
from eval import daily_metrics, trace_alert, trace_metrics, trace_reconcile, trace_retention
print(json.dumps([daily_metrics.tcr.PROD_TRACE_DIR, trace_alert.TRACE_DIR,
 trace_metrics.TRACE_DIR, trace_reconcile.TRACE_DIR, trace_retention.DEFAULT_DIR]))
''', TRACE_DIR=str(traces))
    assert roots == [str(traces)] * 5


def test_missing_explicit_host_directory_does_not_become_zero_anomalies(tmp_path):
    result = python_at(tmp_path, WRITE_AND_READ, BLOG_ROOT=str(tmp_path / "missing-host"))
    assert not result["health"] and result["sizes"] == []
    assert "心跳探针：读不到" in result["report"]


def test_reconciliation_without_configured_host_logs_cannot_claim_a_clean_audit(tmp_path):
    result = subprocess.run([sys.executable, "-m", "eval.trace_reconcile", "--json"],
                            cwd=tmp_path, env=environment(), capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 2 and "host logs are unavailable" in result.stderr
