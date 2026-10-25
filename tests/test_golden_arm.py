"""Only active runtimes execute; historical report identities remain readable."""

from contextlib import ExitStack

import pytest

from eval import golden_arm as arms


@pytest.mark.parametrize("value,expected", [("", "native"), (" native ", "native")])
def test_runtime_selection(monkeypatch, value, expected):
    monkeypatch.setenv(arms.ENV_ARM, value)
    assert arms.arm_name() == expected


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
    monkeypatch.setenv(arms.ENV_ARM, name)
    with pytest.raises(RuntimeError):
        arms.arm_name()
    with ExitStack() as resources, pytest.raises(RuntimeError):
        arms.build_agent(name, resources=resources)


def test_builds_only_selected_runtime(monkeypatch):
    import agent.runtime.native as native

    runtime = object()
    received = []

    def build(*, resources):
        received.append(resources)
        return runtime

    monkeypatch.setattr(native, "build", build)
    with ExitStack() as resources:
        assert arms.build_agent("native", resources=resources) is runtime
        assert received == [resources]


def test_reports_keep_baseline_and_historical_runs_separate():
    assert arms.engine_for("native") == "native"
    assert arms.reports_dir("native") == "eval/report/runs"
    assert arms.is_baseline_arm("native")


def test_runner_closes_owned_resources_and_can_restart(monkeypatch):
    from eval import golden_runtime as run_golden

    events = []

    def build(arm, *, resources):
        runtime = object()
        resources.callback(events.append, runtime)
        return runtime

    monkeypatch.setattr(run_golden, "_runtime", None)
    with ExitStack() as resources:
        monkeypatch.setattr(run_golden, "_resources", resources)
        monkeypatch.setattr(run_golden.golden_arm, "build_agent", build)
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

    def fail(arm, *, resources):
        resources.callback(events.append, "closed")
        raise ValueError("runtime fixture")

    monkeypatch.setattr(run_golden, "_runtime", None)
    with ExitStack() as resources:
        monkeypatch.setattr(run_golden, "_resources", resources)
        monkeypatch.setattr(run_golden.golden_arm, "build_agent", fail)
        with pytest.raises(ValueError, match="runtime fixture"):
            run_golden.ensure_agent()
        assert events == ["closed"] and run_golden._runtime is None
        run_golden.close_agent()
        assert events == ["closed"]
