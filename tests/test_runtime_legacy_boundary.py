"""The native runtime has one explicit compatibility import into the old graph."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "agent" / "runtime"


def test_runtime_old_graph_import_is_concentrated_in_compatibility_bridge():
    offenders = []
    for path in sorted(RUNTIME.glob("*.py")):
        if path.name == "legacy_bridge.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "agent.graph" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_legacy_bridge_is_lazy_and_has_the_graph_checkpoint_adapter():
    source = (RUNTIME / "legacy_bridge.py").read_text(encoding="utf-8")
    assert "from agent.graph import graph_input as build_graph_input" in source
    assert "def graph_input(" in source
