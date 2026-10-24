"""Only active runtimes execute; historical report identities remain readable."""

import pytest

from eval import golden_arm as arms


@pytest.mark.parametrize("value,expected", [("", "native"), (" native ", "native")])
def test_runtime_selection(monkeypatch, value, expected):
    monkeypatch.setenv(arms.ENV_ARM, value)
    assert arms.arm_name() == expected


@pytest.mark.parametrize("name", ["react", "tools", "capability", "matrix", "checkpoint",
                                  "checkpoint_random", "checkpoint_delta", "unknown"])
def test_retired_or_unknown_runtime_never_falls_back(monkeypatch, name):
    monkeypatch.setenv(arms.ENV_ARM, name)
    with pytest.raises(RuntimeError):
        arms.arm_name()
    with pytest.raises(RuntimeError):
        arms.build_agent(name)


def test_builds_only_selected_runtime(monkeypatch):
    import agent.runtime.native as native

    runtime = object()
    monkeypatch.setattr(native, "build", lambda: runtime)
    assert arms.build_agent("native") is runtime


def test_reports_keep_baseline_and_historical_runs_separate():
    assert arms.engine_for("native") == "native"
    assert arms.reports_dir("native") == "eval/report/runs"
    assert arms.is_baseline_arm("native")
