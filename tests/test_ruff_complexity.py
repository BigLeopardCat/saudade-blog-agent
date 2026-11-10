"""Exercise Ruff's configured rules against source and ignored evaluation artifacts."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repository(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "pyproject.toml").write_text((ROOT / "pyproject.toml").read_text())
    (tmp_path / ".gitignore").write_text("eval/report/\n.venv/\n_parent/\n")
    return tmp_path


def scan(root):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "C901,PLR0915",
            "--output-format",
            "json",
            ".",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 1), result.stderr
    return result.returncode, json.loads(result.stdout)


def branching_source():
    return "def choose(value):\n" + "".join(
        f"    if value == {index}:\n        return {index}\n" for index in range(32)
    )


def test_ordinary_function_and_ignored_artifacts_are_not_rejected(repository):
    (repository / "app.py").write_text("def ordinary(value):\n    return value + 1\n")
    for folder in ("eval/report/frozen", ".venv", "_parent"):
        artifact = repository / folder / "generated.py"
        artifact.parent.mkdir(parents=True)
        artifact.write_text(branching_source() + "\ninvalid source(\n")
    assert scan(repository) == (0, [])


def test_complex_control_flow_is_rejected_by_official_mccabe_rule(repository):
    (repository / "app.py").write_text(branching_source())
    code, findings = scan(repository)
    assert code == 1 and {row["code"] for row in findings} == {"C901"}
    assert all(Path(row["filename"]).name == "app.py" for row in findings)


def test_large_straight_line_function_is_rejected_by_statement_rule(repository):
    source = "def build():\n    value = 0\n" + "    value += 1\n" * 160 + "    return value\n"
    (repository / "app.py").write_text(source)
    code, findings = scan(repository)
    assert code == 1 and {row["code"] for row in findings} == {"PLR0915"}
