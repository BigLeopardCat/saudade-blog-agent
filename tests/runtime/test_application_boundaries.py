"""Business domains and the reusable runtime cannot import a product adapter."""

import ast
from pathlib import Path

import pytest

from agent.runtime.service import AgentRuntime


@pytest.mark.parametrize("package", ["domains", "runtime"])
def test_shared_layers_do_not_depend_on_products(package):
    root = Path(__file__).resolve().parents[2] / "agent" / package
    dependencies = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            dependencies.extend(
                (str(path.relative_to(root)), node.lineno, name)
                for name in names
                if name.startswith("agent.products")
            )
    assert not dependencies


def test_runtime_requires_an_explicit_application():
    with pytest.raises(TypeError, match="application"):
        AgentRuntime(object())
