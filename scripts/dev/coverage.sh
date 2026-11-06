#!/usr/bin/env bash
# Report pytest coverage without a pass threshold; reset only the coverage output directory.

set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

COV_DIR=".coverage-data"

rm -rf "$COV_DIR"
mkdir -p "$COV_DIR"

# `--cov` 不带值：射程取 `[tool.coverage.run].source`（= wheel 包列表，同一件事），
# 不在这里再抄一份。`COVERAGE_FILE` 指进 `COV_DIR`，与 `parallel` 那份数据落点对齐。
COVERAGE_FILE="$COV_DIR/.coverage" exec .venv/bin/python -m pytest -q \
  --cov --cov-report=term-missing "$@"
