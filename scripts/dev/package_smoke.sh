#!/usr/bin/env bash
# Build and import the wheel outside the source checkout and its virtualenv.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
artifact_dir="$(mktemp -d)"
trap 'rm -rf -- "$artifact_dir"' EXIT
cd "$project_dir"
python_bin="$(uv python find)"
uv build --out-dir "$artifact_dir"
wheels=("$artifact_dir"/saudade_agent-*.whl)
[[ ${#wheels[@]} -eq 1 && -f "${wheels[0]}" ]]
UV_PROJECT_ENVIRONMENT="$artifact_dir/venv" uv sync --locked --no-dev --no-editable \
  --no-install-project --python "$python_bin"
uv pip install --python "$artifact_dir/venv/bin/python" --no-deps "${wheels[0]}"
cd "$artifact_dir"
SAUDADE_IGNORE_ENV_FILE=1 "$artifact_dir/venv/bin/python" -I - "$project_dir" <<'PY'
import sys
import json
import logging.config
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import agent
from agent.main import app
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.capabilities import ToolCatalog
from tools.base import get_all_tools

installed = distribution("saudade-agent")
imported = Path(agent.__file__).resolve()
assert imported == Path(installed.locate_file("agent/__init__.py")).resolve()
assert not imported.is_relative_to(Path(sys.argv[1]).resolve())
assert app.state.runtime is None
assert app.version == installed.version
for name in ("pytest", "ruff", "mypy"):
    try:
        distribution(name)
    except PackageNotFoundError:
        continue
    raise AssertionError(f"Development package leaked into production environment: {name}")
log_config = Path(installed.locate_file("config/logging.json"))
assert log_config.is_file()
logging.config.dictConfig(json.loads(log_config.read_text()))
logging.getLogger("uvicorn.error").info("Installed framework logging configuration ready")
tools = get_all_tools()
assert tools and len(tools) == len({tool.name for tool in tools})
domains = ToolCatalog().domains({"configurable": {
    "principal": Principal(1, "superadmin", SOURCE_ASSERTION), "user_id": 1,
    "conversation_id": 1, "thread_id": "conv_1",
}}, True)
assert "get_image_generation" in domains["admin_image_generation"]
assert all(tool.name == name for domain in domains.values() for name, tool in domain.items())
assert app.state.runtime is None
print("Production-only wheel installation, application import and domain registration passed")
PY
