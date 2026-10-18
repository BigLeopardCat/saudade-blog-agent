#!/usr/bin/env bash
# 装上本仓的 git 钩子（20261018）。**每台机器跑一次**，不改仓库内容、不改全局配置。
#
#   bash scripts/dev/install-hooks.sh
#
# 做且只做一件事：把 `core.hooksPath` 指到 `.githooks/`。为什么需要这一步——钩子文件
# 本身会跟着 clone 走，但"去哪里找钩子"是 **git config**（`.git/config`，不入库），
# 所以它不会被 clone 带走，新机器/新克隆默认还是空的 `.git/hooks/`。
#
# 为什么不用 `.git/hooks/`（复制文件进去）：那份副本随仓库更新而漂——`.githooks/` 里的
# 规范改了，别人机器上那份还是旧的，而且没人会想起来重装。指路径就没有副本可漂。
#
# 与 CI 的关系：`.github/workflows/eval.yml` 对本次 push 的每条提交跑**同一个**
# `.githooks/commit-msg`。本机这一步只是让问题在你眼前炸（而不是等 CI 红）；
# **CI 那一步才是真正的闸门**（它不依赖任何人的本地配置）——所以别把"我本机装过钩子"
# 当成"这个仓有提交信息闸门"。
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
