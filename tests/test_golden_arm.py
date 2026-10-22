"""Only active runtimes execute; historical report identities remain readable."""

import pytest

from eval import golden_arm as arms


@pytest.mark.parametrize("value,expected", [("", "graph"), ("graph", "graph"),
                                           (" HYBRID ", "hybrid")])
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
    import agent.agent as legacy
    import agent.runtime.hybrid as hybrid

    graph, migrated = object(), object()
    monkeypatch.setattr(legacy, "create_agent", lambda: graph)
    monkeypatch.setattr(hybrid, "build", lambda: migrated)
    assert arms.build_agent("graph") is graph
    assert arms.build_agent("hybrid") is migrated


def test_reports_keep_baseline_and_historical_runs_separate():
    assert arms.engine_for("graph") == "native"
    assert arms.reports_dir("graph") == "eval/report/runs"
    assert arms.is_baseline_arm("graph")
    for name in ("hybrid", "react", "matrix", "capability"):
        assert arms.reports_dir(name) == f"eval/report/runs_{name}"
        assert not arms.is_baseline_arm(name)
