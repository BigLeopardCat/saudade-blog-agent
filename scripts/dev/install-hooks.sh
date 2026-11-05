#!/usr/bin/env bash
# Install this repository’s shared hooks in its local Git configuration.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"     # 在子目录里跑也能对

if [ ! -d .githooks ]; then
  echo "✗ 当前仓库里没有 .githooks/ 目录（跑错仓了？）" >&2
  exit 1
fi

git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true

echo "✓ core.hooksPath = $(git config --get core.hooksPath)"
echo "  已装上的钩子：$(ls .githooks | tr '\n' ' ')"
echo "  想撤掉：git config --unset core.hooksPath"
