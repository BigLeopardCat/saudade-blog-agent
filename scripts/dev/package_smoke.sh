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
uv export --frozen --no-dev --no-emit-project --output-file "$artifact_dir/runtime.txt" >/dev/null
uv venv --python "$python_bin" "$artifact_dir/venv"
uv pip sync --python "$artifact_dir/venv/bin/python" --require-hashes "$artifact_dir/runtime.txt"
uv pip install --python "$artifact_dir/venv/bin/python" --no-deps "${wheels[0]}"
cd "$artifact_dir"
"$artifact_dir/venv/bin/python" -I - "$project_dir" <<'PY'
import sys
from importlib.metadata import distribution
from pathlib import Path

import agent
from agent.main import app
from tools.base import get_all_tools

installed = distribution("saudade-agent")
imported = Path(agent.__file__).resolve()
assert imported == Path(installed.locate_file("agent/__init__.py")).resolve()
assert not imported.is_relative_to(Path(sys.argv[1]).resolve())
assert app.state.runtime is None
assert app.version == installed.version
tools = get_all_tools()
assert tools and len(tools) == len({tool.name for tool in tools})
print("Wheel installation, application import and tool registration passed")
PY
