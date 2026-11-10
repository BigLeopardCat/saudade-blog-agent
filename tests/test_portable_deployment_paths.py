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
from config.settings import settings
from utils import trace
trace.start_trace("portable-fixture", 7, "fixture", {"source": "evaluation"})
saved = trace.finish_trace("portable-fixture", "producer_done", 0.01)
print(json.dumps({"trace": str(Path(saved).resolve()), "root": settings.trace_dir}))
'''



def test_default_trace_storage_is_relative_to_the_deployment(tmp_path):
    deployment = tmp_path / "independent-agent"
    deployment.mkdir()
    (deployment / "health.log").write_text("must not be discovered")
    result = python_at(deployment, WRITE_AND_READ)
    assert result["root"] == "data/traces"
    assert pathlib.Path(result["trace"]).is_relative_to(deployment / "data/traces")


def test_explicit_trace_root_is_shared_by_retention_and_metrics(tmp_path):
    deployment, traces = (tmp_path / name for name in ("release", "trace-state"))
    deployment.mkdir()
    result = python_at(deployment, WRITE_AND_READ, TRACE_DIR=str(traces))
    assert pathlib.Path(result["trace"]).is_relative_to(traces)
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


def test_reconciliation_without_configured_host_logs_cannot_claim_a_clean_audit(tmp_path):
    result = subprocess.run([sys.executable, "-m", "eval.trace_reconcile", "--json"],
                            cwd=tmp_path, env=environment(), capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 2 and "host logs are unavailable" in result.stderr
