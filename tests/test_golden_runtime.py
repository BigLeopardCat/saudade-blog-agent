"""The evaluation factory shares native startup and closes its owned resources."""

import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from eval import golden_runtime as runtime


def test_runtime_does_not_require_an_experimental_selector(monkeypatch):
    monkeypatch.delenv("GOLDEN_ARM", raising=False)
    assert runtime.check_environment() is None


@pytest.mark.parametrize(
    "name",
    [
        "native",
        "",
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
    import agent.composition as native

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
    from eval import golden_runtime as golden_evaluation

    events = []

    def build(*, resources):
        runtime = object()
        resources.callback(events.append, runtime)
        return runtime

    monkeypatch.setattr(golden_evaluation, "_runtime", None)
    with ExitStack() as resources:
        monkeypatch.setattr(golden_evaluation, "_resources", resources)
        monkeypatch.setattr(golden_evaluation, "build_agent", build)
        first = golden_evaluation.ensure_agent()
        assert golden_evaluation.ensure_agent() is first and events == []
        golden_evaluation.close_agent()
        golden_evaluation.close_agent()
        assert events == [first] and golden_evaluation._runtime is None
        second = golden_evaluation.ensure_agent()
        assert second is not first
        golden_evaluation.close_agent()
        assert events == [first, second]


def test_runner_failed_build_releases_its_resources(monkeypatch):
    from eval import golden_runtime as golden_evaluation

    events = []

    def fail(*, resources):
        resources.callback(events.append, "closed")
        raise ValueError("runtime fixture")

    monkeypatch.setattr(golden_evaluation, "_runtime", None)
    with ExitStack() as resources:
        monkeypatch.setattr(golden_evaluation, "_resources", resources)
        monkeypatch.setattr(golden_evaluation, "build_agent", fail)
        with pytest.raises(ValueError, match="runtime fixture"):
            golden_evaluation.ensure_agent()
        assert events == ["closed"] and golden_evaluation._runtime is None
        golden_evaluation.close_agent()
        assert events == ["closed"]


def test_current_http_cli_help_uses_the_repository_without_starting_resources():
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-m", "eval.online_baseline", "--help"],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "--report-dir" in result.stdout and "--expected-build" in result.stdout
