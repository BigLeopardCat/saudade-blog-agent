#!/usr/bin/env bash
set -euo pipefail
release_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$release_dir"
export DEPLOY_SHA="$(cat REVISION)"
exec "$release_dir/.venv/bin/python" -m uvicorn agent.main:app \
  --host 127.0.0.1 --port "${AGENT_PORT:-8010}" \
  --workers "${AGENT_WORKERS:-4}" --no-access-log
