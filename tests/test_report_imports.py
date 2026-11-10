"""Diagnostics and product prompt imports must work in a fresh interpreter."""

import json
import os
import subprocess
import sys
from pathlib import Path

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
print(json.dumps({"tools_loaded": "tools" in sys.modules}))
'''


@pytest.mark.parametrize("name", ["agent.reports", "agent.products.blog.prompt"])
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
    if name == "agent.reports":
        assert value["tools_loaded"] is False
