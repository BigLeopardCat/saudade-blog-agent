"""The native runtime has no active compatibility import into the old graph."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "agent" / "runtime"


def test_runtime_has_no_old_graph_imports_or_compatibility_modules():
    offenders = []
    for path in sorted(RUNTIME.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "agent.graph" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
    assert not (RUNTIME / "legacy_bridge.py").exists()
    assert not (RUNTIME / "graph_store.py").exists()
    assert not (RUNTIME / "hybrid.py").exists()
