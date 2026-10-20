#!/usr/bin/env bash
# L0 覆盖率**报告**（**非门禁**，20261013 起；20261020 换入口后仍是同一件事）。
#
# 为什么要它：CI 判的是"这套判据有没有红"，判不了"生产包有多少行压根没被任何判据碰过"。
# 后者是**报告**不是门禁 —— 179 个套件里大量是形状判据（读源码文本、查 AST、比台账），
# 不是行为覆盖，所以 70% 这种数字在这里没有意义，设 `fail_under` 只会逼着人写凑数的用例。
# 量出来的数记进 `docs/eval-observability.md` 那一节（同 L0 的其它读数）。
#
# 20261020 换了入口：原来是 `tests/run_all.py --coverage`（逐套件 `coverage run` 子进程 +
# 结尾 `coverage combine`），今天套件本身就是 pytest 用例，`pytest-cov` 直接干这件事。
# **顺带消失的两处麻烦**（都写在旧 run_all.py 的头注里，别以为它们没存在过）：
#   · 本机 `.venv` 是产线 venv、里面原本**没有 coverage** ⇒ 旧配方要临时 `uv venv /tmp/cov`
#     再把 site-packages 挂到 `PYTHONPATH` 上；今天 `coverage[toml]` 与 `pytest-cov` 都在
#     `pyproject.toml` 的 `[dependency-groups] dev` 里，一条 `uv sync` 就有（本仓不再需要
#     那三行"起不来时的配方"）。
#   · 旧配方要自己保证子进程 `import tests/` 下的助手能成功（`env["PYTHONPATH"] = str(TESTS)`）；
#     今天这件事由 `tests/conftest.py` 负责（收集前把它插进 `sys.path`），**一处**而不是两处。
#
# 落点 `.coverage-data/` 与 `pyproject.toml` 的 `[tool.coverage.run].parallel = true` 是一对：
# 那一条让每个进程各写各的数据文件（不并行收集会互相覆盖），这里指定它们写去哪。
# 这个名字**两处同源**（本脚本 + `.gitignore`），`tests/test_packaging.py` ⑤ 判着。
#
# 用法：
#   bash scripts/dev/coverage.sh                 # 全部套件 + 终端报告
#   bash scripts/dev/coverage.sh -n 4            # 并行跑（参数原样透传给 pytest）
#   bash scripts/dev/coverage.sh --cov-report=html   # 另出一份 HTML 到 htmlcov/
set -euo pipefail
# 路径从脚本自己的位置推（与 scripts/nightly_regression.sh 同一条纪律：写死家目录在别人
# 机器上会当场失败，而失败的样子是一个静默退出的 cron）。
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

COV_DIR=".coverage-data"
# 先清干净：上一次的 `.coverage.*` 残片会被 `combine` 一起合进来 ⇒ 报告里混进**上一版代码**
# 的行号（看着像"这些行被覆盖了"，其实那些行今天可能已经不在）。同族教训见
# `saudade-agent-trace-rotate-fix`（一次性产物没有轮转，只能整块摘除）。
rm -rf "$COV_DIR"
mkdir -p "$COV_DIR"

# `--cov` 不带值：射程取 `[tool.coverage.run].source`（= wheel 包列表，同一件事），
# 不在这里再抄一份。`COVERAGE_FILE` 指进 `COV_DIR`，与 `parallel` 那份数据落点对齐。
COVERAGE_FILE="$COV_DIR/.coverage" exec .venv/bin/python -m pytest -q \
  --cov --cov-report=term-missing "$@"
