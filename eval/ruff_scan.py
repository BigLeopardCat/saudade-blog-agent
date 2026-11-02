"""Strict Ruff JSON execution shared by the lint and exception ratchets."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path


class ScanError(ValueError):
    """The scanner did not produce a complete, usable diagnostic result."""


def counts(output: str, root: Path) -> dict[str, int]:
    try:
        diagnostics = json.loads(output)
    except (ValueError, TypeError) as error:
        raise ScanError("Ruff did not return valid JSON") from error
    if not isinstance(diagnostics, list):
        raise ScanError("Ruff diagnostics must be a JSON array")
    result: Counter[str] = Counter()
    for item in diagnostics:
        if (not isinstance(item, dict) or not isinstance(item.get("filename"), str)
                or not isinstance(item.get("code"), str) or not item["code"]
                or not isinstance(item.get("message"), str) or not item["message"]):
            raise ScanError("Incomplete Ruff diagnostic")
        location = item.get("location")
        if (not isinstance(location, dict)
                or any(type(location.get(key)) is not int or location[key] < 1
                       for key in ("row", "column"))):
            raise ScanError("Invalid Ruff diagnostic location")
        path = Path(item["filename"])
        try:
            relative = (root / path).resolve().relative_to(root.resolve()).as_posix()
        except ValueError as error:
            raise ScanError("Ruff reported a file outside the source root") from error
        result[relative] += 1
    return dict(result)


def scan(prog: list[str], root: Path, sources: list[str], *, select=()) -> dict[str, int]:
    if not sources:
        raise ScanError("No repository Python sources selected")
    command = [*prog, "check", "--no-cache", "--output-format=json"]
    if select:
        command.extend(("--select", ",".join(select)))
    try:
        process = subprocess.run([*command, *sources], cwd=root, capture_output=True,
                                 text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ScanError(f"Ruff could not complete: {error}") from error
    if process.returncode not in (0, 1):
        raise ScanError(f"Ruff failed (exit {process.returncode}): {process.stderr[:1000]}")
    result = counts(process.stdout, root)
    if bool(result) != (process.returncode == 1):
        raise ScanError("Ruff exit status contradicts its diagnostic result")
    if not set(result) <= set(sources):
        raise ScanError("Ruff reported files outside the selected scan scope")
    return result
