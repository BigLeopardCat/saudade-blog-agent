"""The evaluation factory shares native startup and closes its owned resources."""

import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from eval import golden_runtime as runtime


@pytest.mark.parametrize("value,expected", [("", "native"), (" native ", "native")])
def test_runtime_selection(monkeypatch, value, expected):
    monkeypatch.setenv("GOLDEN_ARM", value)
    assert runtime.check_environment() is None


@pytest.mark.parametrize(
    "name",
    [
        "react",
        "tools",
        "capability",
        "matrix",
        "checkpoint",
        "checkpoint_random",
        "checkpoint_delta",
        "unknown",
    ],
)
def test_retired_or_unknown_runtime_never_falls_back(monkeypatch, name):
    monkeypatch.setenv("GOLDEN_ARM", name)
    with pytest.raises(ValueError):
        runtime.check_environment()
    with ExitStack() as resources, pytest.raises(ValueError):
        runtime.build_agent(resources=resources)


def test_builds_only_selected_runtime(monkeypatch):
    import agent.runtime.native as native

    instance = object()
    received = []

    def build(*, resources):
        received.append(resources)
        return instance

    monkeypatch.setattr(native, "build", build)
    with ExitStack() as resources:
        assert runtime.build_agent(resources=resources) is instance
        assert received == [resources]


def test_current_reports_use_the_native_identity():
    assert runtime.ENGINE == "native"
    assert runtime.REPORTS_DIR == "eval/report/runs"


def test_runner_closes_owned_resources_and_can_restart(monkeypatch):
    from eval import golden_runtime as run_golden

    events = []

    def build(*, resources):
        runtime = object()
        resources.callback(events.append, runtime)
        return runtime

    monkeypatch.setattr(run_golden, "_runtime", None)
    with ExitStack() as resources:
        monkeypatch.setattr(run_golden, "_resources", resources)
        monkeypatch.setattr(run_golden, "build_agent", build)
        first = run_golden.ensure_agent()
        assert run_golden.ensure_agent() is first and events == []
        run_golden.close_agent()
        run_golden.close_agent()
        assert events == [first] and run_golden._runtime is None
        second = run_golden.ensure_agent()
        assert second is not first
        run_golden.close_agent()
        assert events == [first, second]


def test_runner_failed_build_releases_its_resources(monkeypatch):
    from eval import golden_runtime as run_golden

    events = []

    def fail(*, resources):
        resources.callback(events.append, "closed")
        raise ValueError("runtime fixture")

    monkeypatch.setattr(run_golden, "_runtime", None)
    with ExitStack() as resources:
        monkeypatch.setattr(run_golden, "_resources", resources)
        monkeypatch.setattr(run_golden, "build_agent", fail)
        with pytest.raises(ValueError, match="runtime fixture"):
            run_golden.ensure_agent()
        assert events == ["closed"] and run_golden._runtime is None
        run_golden.close_agent()
        assert events == ["closed"]


@pytest.mark.parametrize("entry", ["run_golden.py", "golden_full_run.py"])
def test_legacy_cli_uses_its_checkout_and_supports_help(entry, tmp_path):
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, str(root / "eval" / entry), "--help"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout and "--help" in result.stdout
