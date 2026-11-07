"""Diagnostics and product prompt imports must work in a fresh interpreter."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]

IMPORT = '''
import importlib
import json
import sys

name = sys.argv[1]
module = importlib.import_module(name)
if name == "agent.reports":
    assert module.sanitize_untrusted(" a\\nb ") == "a b"
elif name == "agent.products.blog.prompt":
    assert callable(module.build_prompt)
from agent.hostinfo import SERVICES
print(json.dumps({"services": SERVICES, "tools_loaded": "tools" in sys.modules}))
'''


@pytest.mark.parametrize("name", ["agent.hostinfo", "agent.reports", "agent.products.blog.prompt"])
@pytest.mark.parametrize("enabled", [False, True])
def test_fresh_imports_keep_iot_configuration_without_registering_tools_for_diagnostics(
        tmp_path, name, enabled):
    (tmp_path / ".env").write_text("IOT_ENABLED=invalid-local-value\n")
    result = subprocess.run(
        [sys.executable, "-c", IMPORT, name], cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "HOME": str(tmp_path),
             "SAUDADE_IGNORE_ENV_FILE": "1", "IOT_ENABLED": "1" if enabled else "0"},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["services"] == ["saudade-rust", "saudade-agent"] + (
        ["saudade-device"] if enabled else [])
    if name in {"agent.hostinfo", "agent.reports"}:
        assert value["tools_loaded"] is False
