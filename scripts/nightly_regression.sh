#!/usr/bin/env bash
# Nightly regression: skill-routing 单元测试 + golden set (13 real-LLM cases)
# 由 crontab 触发（见仓库 README 或 crontab -l）。结果追加到 ~/agent_regression.log；
# 任一项失败会在 ~/agent_regression.failed 留下标记（存在 = 上次运行失败）。
set -u
cd /home/ubuntu/memory_blog_rust/saudade-blog-agent
PY=.venv/bin/python
LOG="$HOME/agent_regression.log"
MARK="$HOME/agent_regression.failed"
TS=$(date '+%Y-%m-%d %H:%M:%S')

echo "=== nightly regression $TS ===" >> "$LOG"

fail=0
echo "--- test_skills (技能注册表/plan 契约, 秒级) ---" >> "$LOG"
$PY test_skills.py >> "$LOG" 2>&1 || { fail=1; echo "[$TS] test_skills FAILED" >> "$LOG"; }
echo "--- golden set (13 条真实对话, 约 1-2 分钟) ---" >> "$LOG"
$PY eval/run_golden.py >> "$LOG" 2>&1 || { fail=1; echo "[$TS] golden set FAILED (详见下方报告)" >> "$LOG"; }

if [ "$fail" -eq 0 ]; then
  echo "[$TS] ALL PASS" >> "$LOG"
  rm -f "$MARK"
else
  echo "[$TS] FAILED — 见上方输出" >> "$LOG"
  touch "$MARK"
fi
