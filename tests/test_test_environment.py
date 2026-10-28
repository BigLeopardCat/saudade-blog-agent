"""Default tests must not depend on deployment settings or discover local services."""

import pathlib
import runpy
from unittest.mock import Mock

import _parent_repo
import psycopg
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_dirty_deployment_environment_cannot_change_offline_defaults(isolated_pytest, monkeypatch):
    for key, value in {
        "LLM_PROVIDER": "not-a-provider",
        "AGENT_CONTEXT_TOKENS": "not-a-number",
        "AGENT_ARM": "graph",
        "OPENCODE_API_KEY": "synthetic-secret",
        "CHECKPOINT_DSN": "postgresql://127.0.0.1:1/unused",
        "AGENT_STREAM_IDLE_TIMEOUT": "invalid",
        "TRACE_DIR": "/nonexistent-deployment/traces",
        "IDEM_DB": "/nonexistent-deployment/idem.sqlite3",
    }.items():
        monkeypatch.setenv(key, value)
    (isolated_pytest.path / ".env").write_text("AGENT_CONTEXT_TOKENS=invalid\n")
    isolated_pytest.makepyfile(
        test_environment="""
import os
import pathlib
import tempfile
from config.settings import Settings, settings
from utils import trace

def test_defaults():
    for field in ("llm_provider", "agent_context_tokens", "opencode_api_key", "checkpoint_dsn",
                  "stream_idle_timeout"):
        assert getattr(settings, field) == Settings.model_fields[field].default
    assert "AGENT_ARM" not in os.environ
    assert "OPENCODE_API_KEY" not in os.environ
    assert settings.iot_enabled
    for field in ("trace_dir",):
        path = pathlib.Path(getattr(settings, field))
        assert pathlib.Path(tempfile.gettempdir()) in path.parents
        assert "saudade-pytest-" in str(path)
    trace.start_trace("isolation-probe", 7, "fixture", {"source": "evaluation"})
    saved = trace.finish_trace("isolation-probe", "producer_done", 0.01)
    assert pathlib.Path(saved).is_relative_to(settings.trace_dir)
"""
    )
    isolated_pytest.runpytest_subprocess("-q", "-n", "0").assert_outcomes(passed=1)


@pytest.mark.parametrize(
    "filename", ["test_checkpoint_setup_concurrent.py", "test_pg_retention_live.py"]
)
def test_collecting_postgres_without_explicit_dsn_never_connects(filename, monkeypatch):
    monkeypatch.delenv("SAUDADE_CKPT_TEST_DSN", raising=False)
    monkeypatch.delenv("SAUDADE_REQUIRE_CKPT_TEST", raising=False)
    connect = Mock(side_effect=AssertionError("collection must not connect"))
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(pytest.skip.Exception, match="SAUDADE_CKPT_TEST_DSN"):
        runpy.run_path(str(ROOT / "tests/integration" / filename))
    connect.assert_not_called()


def test_host_contract_is_never_discovered_in_the_deployment_parent(tmp_path, monkeypatch):
    monkeypatch.delenv("SAUDADE_PARENT_REPO", raising=False)
    monkeypatch.setattr(_parent_repo, "ROOT", tmp_path / "agent")
    sentinel = tmp_path / "src/routes/chat.rs"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("deployment source must not be selected")
    assert _parent_repo.parent_root() is None


def test_missing_optional_host_contract_is_reported_as_skipped(monkeypatch):
    monkeypatch.setenv("SAUDADE_PARENT_REPO", "/nonexistent-host-contract-fixture")
    monkeypatch.delenv("SAUDADE_REQUIRE_PARENT", raising=False)
    with pytest.raises(pytest.skip.Exception, match="host contract"):
        _parent_repo.read("src/routes/chat.rs")
