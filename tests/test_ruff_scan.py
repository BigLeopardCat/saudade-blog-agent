"""Scanner failures cannot pass a count ratchet or overwrite its baseline."""

import json
import subprocess
import sys

import pytest

from eval import except_gate, lint_gate
from eval.ruff_scan import ScanError, counts, scan


def diagnostic(path):
    return {"filename": str(path), "code": "F401", "message": "unused import",
            "location": {"row": 1, "column": 1}}


@pytest.mark.parametrize("module", [lint_gate, except_gate])
@pytest.mark.parametrize("code,body", [
    (2, ""), (-9, "[]"), (0, ""), (0, "not JSON"), (0, "{}"), (1, "[]"),
])
@pytest.mark.parametrize("update", [False, True])
def test_cli_rejects_unusable_scans_without_modifying_baseline(
    monkeypatch, tmp_path, module, code, body, update
):
    pin, *_ = module._ruff_cfg()
    baseline = json.loads(module.BASELINE.read_text())
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline))
    before = path.read_bytes()
    monkeypatch.setattr(module, "BASELINE", path)
    monkeypatch.setattr(module, "python_sources", lambda root: [root / "config/settings.py"])
    monkeypatch.setattr(sys, "argv", ["gate", *(["--update"] if update else [])])

    def execute(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, f"ruff {pin}\n", "")
        assert "--output-format=json" in command
        return subprocess.CompletedProcess(command, code, body, "scanner unavailable")

    monkeypatch.setattr(subprocess, "run", execute)
    assert module.main() == 3
    assert path.read_bytes() == before


@pytest.mark.parametrize("code,count", [(0, 0), (1, 1)])
def test_successful_scans_preserve_real_findings(monkeypatch, tmp_path, code, count):
    item = diagnostic(tmp_path / "source.py")
    monkeypatch.setattr(subprocess, "run", lambda command, **kw:
                        subprocess.CompletedProcess(command, code, json.dumps([item] * count), ""))
    assert scan(["ruff"], tmp_path, ["source.py"]) == ({"source.py": 1} if count else {})


def test_invalid_diagnostics_and_out_of_scope_files_fail_closed(monkeypatch, tmp_path):
    for item in ({}, {**diagnostic(tmp_path / "source.py"), "location": {"row": 0}},
                 diagnostic(tmp_path.parent / "outside.py")):
        with pytest.raises(ScanError):
            counts(json.dumps([item]), tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda command, **kw:
                        subprocess.CompletedProcess(command, 1,
                            json.dumps([diagnostic(tmp_path / "unselected.py")]), ""))
    with pytest.raises(ScanError, match="selected scan scope"):
        scan(["ruff"], tmp_path, ["source.py"])


@pytest.mark.parametrize("error", [FileNotFoundError("ruff"),
                                  subprocess.TimeoutExpired("ruff", 120)])
def test_scanner_launch_failure_is_not_an_empty_result(monkeypatch, tmp_path, error):
    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(ScanError):
        scan(["ruff"], tmp_path, ["source.py"])
