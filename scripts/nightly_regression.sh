#!/usr/bin/env bash
# Nightly regression: test_skills 单测 + 检索基准 + golden set（78 条真实 LLM 用例）+ 巡检
# 由 crontab 触发（见仓库 README 或 crontab -l）。结果追加到 ~/agent_regression.log；
# 任一门禁项失败会在 ~/agent_regression.failed 留下标记（存在 = 上次运行失败）。
# golden 有 FAIL 时导出复审单 eval/report/review_<ts>.md（判据 vs 模型实际输出）——
# 复审规则（20260912）：假失败当轮修判据，真 FAIL 才允许挂着（否则门禁失去区分度）。
# 20260921 起 golden 分两层判：**回归组（tags 含 regression）硬判 100%**（不受
# --min-pass-rate 放宽），能力题仍按通过率——两类红的严重度不同，不许被平均数吸收。
# 同日补：trace_alert 抓到的现场回灌成 golden 草稿（eval/golden_draft.py，只产草稿
# 到 eval/report/ 供人审，不自动入库、非门禁）。
# 20260924 补：跨源对账（eval/trace_reconcile.py，把 trace ↔ agent.log ↔ monitor.log
# 三个源对起来看，非门禁）——单源规则扫描看不见"两个源之间"的错（轮次自洽、前端只见
# 报错那类），报告进 eval/report/reconcile_<ts>.md，异常时另写一条 WARN 到
# logs/health.log（与一分钟心跳探针同一条通道）。
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
# 20260924 起：检索基准（recall@k / MRR，直接测线上 rag/search.py，秒级、无网）。
# README 里一直写着 nightly 跑 L1/L2 两项，实际只有 L2——这一节把它补齐。
# 非门禁：已知 FAIL 是词法表征的局限（脚本自己在报告里点名），不该让夜间任务变红。
echo "--- 检索基准 recall@k / MRR (直接测线上 rag/search.py, 秒级, 非门禁) ---" >> "$LOG"
$PY eval/recall_eval.py >> "$LOG" 2>&1 || echo "[$TS] recall_eval 运行异常（不影响门禁）" >> "$LOG"echo "--- golden set (78 条真实对话, 约 20 分钟) ---" >> "$LOG"
$PY eval/run_golden.py >> "$LOG" 2>&1 || { fail=1; echo "[$TS] golden set FAILED (复审单 eval/report/review_*.md；回归组红 = 当天必修)" >> "$LOG"; }
# 20260912 起：语义告警巡检（非门禁——只记录不置失败标记，避免与 golden 门禁混同）
echo "--- trace 语义告警 (近 7 天真实对话, 巡检非门禁) ---" >> "$LOG"
$PY eval/trace_alert.py --days 7 >> "$LOG" 2>&1 || echo "[$TS] trace_alert 运行异常（不影响门禁）" >> "$LOG"
# 20260921 起：trace_alert 命中现场 → golden 用例**草稿**（含真实用户文本，只写
# eval/report/ 下、不自动入库；人审后手抄进 eval/golden/basic.jsonl 并打标签）
echo "--- golden 草稿回灌 (非门禁, 只产草稿供人审) ---" >> "$LOG"
$PY eval/golden_draft.py --days 1 >> "$LOG" 2>&1 || echo "[$TS] golden_draft 运行异常（不影响门禁）" >> "$LOG"
# 20260924 起：跨源对账（非门禁）。默认窗口就是"刚过去这一天"——夜里跑，对的正是
# 刚过去的这一夜。异常时它自己写 health.log 的 WARN，这里只留一行结论在日志里。
echo "--- 跨源对账 (trace ↔ agent.log ↔ monitor.log, 巡检非门禁) ---" >> "$LOG"
$PY eval/trace_reconcile.py >> "$LOG" 2>&1 || echo "[$TS] trace_reconcile 运行异常（不影响门禁）" >> "$LOG"

if [ "$fail" -eq 0 ]; then
  echo "[$TS] ALL PASS" >> "$LOG"
  rm -f "$MARK"
else
  echo "[$TS] FAILED — 见上方输出" >> "$LOG"
  touch "$MARK"
fi
