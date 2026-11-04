"""Keep the architecture overview accurate without duplicating every source file."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_package_overview_covers_shipped_packages():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    packages = project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    overview = (ROOT / "docs/agent-architecture.md").read_text()
    for package in packages:
        assert (ROOT / package).is_dir()
        assert f"`{package}/`" in overview, f"Missing package responsibility: {package}"


def test_documented_source_paths_exist():
    documents = [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]
    prefixes = ("agent/", "tools/", "models/", "rag/", "config/", "utils/", "tests/", "eval/")
    missing = []
    for document in documents:
        for path in re.findall(r"`([A-Za-z0-9_./-]+)`", document.read_text()):
            if path.startswith(prefixes) and (path.endswith("/") or path.endswith(".py")):
                if not (ROOT / path).exists():
                    missing.append(f"{document.name}: {path}")
    assert missing == []
