"""Dirty evaluation artifacts cannot become current source-quality inputs."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from eval.source_files import python_sources


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("eval/report/\n.venv/\n_parent/\n")
    (tmp_path / "app.py").write_text("def current():\n    return 1\n")
    git(tmp_path, "add", ".gitignore", "app.py")
    (tmp_path / "new.py").write_text("def fresh():\n    return 2\n")
    return tmp_path


def load_check(name):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_scope_uses_git_and_includes_new_source(repository):
    ignored = repository / "eval/report/old/evaluator/old.py"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("this is not valid Python")
    for folder in (".venv", "_parent"):
        (repository / folder).mkdir()
        (repository / folder / "unrelated.py").write_text("broken(")
    assert [p.name for p in python_sources(repository)] == ["app.py", "new.py"]
    # Explicitly versioned code remains source even when its directory is ignored.
    git(repository, "add", "-f", str(ignored.relative_to(repository)))
    assert ignored in python_sources(repository)


def test_explicit_type_roots_exclude_evaluation_and_keep_new_application_files(repository):
    (repository / "agent").mkdir()
    (repository / "agent" / "new.py").write_text("value = 1\n")
    (repository / "eval").mkdir()
    (repository / "eval" / "check.py").write_text("value = 2\n")
    selected = python_sources(repository, ["agent", "app.py"])
    assert [p.relative_to(repository).as_posix() for p in selected] == ["agent/new.py", "app.py"]


def test_size_scans_are_unchanged_by_ignored_snapshots(repository, monkeypatch):
    check = load_check("test_module_budget")
    monkeypatch.setattr(check, "ROOT", repository)
    original = check.scan()
    snapshot = repository / "eval/report/frozen/evaluator/oversize.py"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("invalid source(\n" * 1000)
    assert check.scan() == original
    (repository / "new.py").write_text("invalid source(\n")
    with pytest.raises(SystemExit, match="new.py"):
        check.scan()


def test_security_scanner_receives_only_repository_sources(repository, monkeypatch):
    from eval import security_gate

    snapshot = repository / "eval/report/frozen/evaluator/old.py"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("eval(input())")
    monkeypatch.setattr(security_gate, "ROOT", repository)
    commands = []
    original_run = subprocess.run

    def run(command, **kwargs):
        if command[0] == "git":
            return original_run(command, **kwargs)
        commands.append(command)
        Path(command[command.index("-o") + 1]).write_text(json.dumps({"results": []}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    security_gate._run_json(["bandit"])
    assert "app.py" in commands[0] and "new.py" in commands[0]
    assert "eval/report/frozen/evaluator/old.py" not in commands[0]


def test_enumeration_failure_cannot_report_an_empty_success(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        python_sources(tmp_path)
