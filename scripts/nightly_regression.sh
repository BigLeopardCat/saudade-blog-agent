#!/usr/bin/env bash
# Opt-in nightly checks. Missing optional host probes are skipped, never counted as evaluated.
# Required failures set the sentinel; diagnostic reports do not relax those checks.
set -u
for actor_key in GOLDEN_ADMIN_UID GOLDEN_USER_UID; do
  actor_id="${!actor_key:-}"
  if ! [[ "$actor_id" =~ ^[1-9][0-9]*$ ]]; then
    echo "未评估：请显式配置 $actor_key 为测试账号的正整数编号。" >&2
    exit 3
  fi
done
export GOLDEN_ADMIN_UID GOLDEN_USER_UID
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=.venv/bin/python
LOG="$HOME/agent_regression.log"
MARK="$HOME/agent_regression.failed"
TS=$(date '+%Y-%m-%d %H:%M:%S')

echo "=== nightly regression $TS ===" >> "$LOG"

export SAUDADE_PARENT_REPO="${SAUDADE_PARENT_REPO:-${PROJECT_DIR:-$(dirname "$PWD")}}"
export SAUDADE_REQUIRE_PARENT=1

fail=0
echo "--- pytest (全部离线套件, 按目录收集, 秒级, 无网络无 LLM) ---" >> "$LOG"
$PY -m pytest -q >> "$LOG" 2>&1 || { fail=1; echo "[$TS] pytest FAILED" >> "$LOG"; }
echo "--- 检索基准 recall@k / MRR (直接测线上 rag/search.py, 秒级, 非门禁) ---" >> "$LOG"
$PY eval/recall_eval.py >> "$LOG" 2>&1 || echo "[$TS] recall_eval 运行异常（不影响门禁）" >> "$LOG"
echo "--- 语料漂移哨兵 (词表型断言 vs 语料, 秒级, 非门禁) ---" >> "$LOG"
$PY eval/corpus_terms.py --drift >> "$LOG" 2>&1 || echo "[$TS] corpus_terms --drift 有 ORPHAN/MISBOUND 或运行异常（非门禁，看报告）" >> "$LOG"
echo "--- 真写夹具残留哨兵 (只读公开接口, 秒级, 非门禁) ---" >> "$LOG"
$PY eval/golden_fixture.py --verify >> "$LOG" 2>&1 || echo "[$TS] 真写夹具有残留或读不到公开接口（非门禁，见上面的 [fixture-leftover]/[fixture-check-failed] 行）" >> "$LOG"
echo "--- golden 趋势回归（实际规模、跳过与费用以本轮报告为准） ---" >> "$LOG"
$PY eval/golden_fixture_account.py --verify >> "$LOG" 2>&1 || echo "[$TS] 账号夹具有残留或读不到后台账号名录（非门禁，见上面的 [fixture-leftover]/[fixture-check-failed] 行）" >> "$LOG"
$PY eval/golden_fixture_board.py --verify >> "$LOG" 2>&1 || echo "[$TS] 留言夹具有残留或读不到后台留言清单（非门禁，见上面的 [fixture-leftover]/[fixture-check-failed] 行）" >> "$LOG"
$PY eval/golden_fixture_todo.py --verify >> "$LOG" 2>&1 || echo "[$TS] 待办夹具有残留或读不到后台待办列表（非门禁，见上面的 [fixture-leftover]/[fixture-check-failed] 行）" >> "$LOG"
# This host probe writes disposable fixture accounts; never enable unattended self-probes.
PROBE="$SAUDADE_PARENT_REPO/scripts/probe_token_revoke.py"
echo "--- 令牌收回/冻结 真链路探针 (写生产库: 只建删 probe_revoke_* 靶子账号, 门禁) ---" >> "$LOG"
if [ ! -f "$PROBE" ]; then
  echo "[$TS] SKIP 令牌收回探针不存在：$PROBE（未评估，不计为通过）" >> "$LOG"
else
  python3 "$PROBE" --admin-uid "$GOLDEN_ADMIN_UID" --no-self-probe >> "$LOG" 2>&1
  rc=$?
  if [ "$rc" -eq 3 ]; then
    fail=1
    echo "[$TS] 令牌收回探针**未评估**（前置不可用：管理员 uid 今天自己就不通——见上面 ⚠ 那行。不是「有回归」，别去查闸门）" >> "$LOG"
  elif [ "$rc" -ne 0 ]; then
    fail=1
    echo "[$TS] 令牌收回探针 FAILED（冻结/改密码/旁路收口有回归——先看上面逐条 ❌，再谈 golden）" >> "$LOG"
  fi
fi
$PY eval/run_golden.py >> "$LOG" 2>&1 || { fail=1; echo "[$TS] golden set FAILED (复审单 eval/report/review_*.md；回归组红 = 当天必修；采样层下界 < 地板 0.78 = 事故；未到档位不置红、只记；用户可见兜底轮次 ≥ 5% = 事故；退出码 3 = 前提不可用，不是通过率；企业落地三条见 landing_gate.py --readiness)" >> "$LOG"; }
echo "--- 回复出处评审 (LLM-as-judge, 约 10 分钟, 非门禁) ---" >> "$LOG"
PYTHONUNBUFFERED=1 $PY eval/llm_judge.py >> "$LOG" 2>&1 || echo "[$TS] llm_judge 运行异常（非门禁，看 eval/report/judge_*.md）" >> "$LOG"
echo "--- hybrid task outcomes (synthetic business API) ---" >> "$LOG"
$PY -m eval.native_contracts >> "$LOG" 2>&1 || {
  fail=1
  echo "[$TS] hybrid task outcomes FAILED; inspect _smoke_native_contracts reports" >> "$LOG"
}
$PY -m eval.daily_metrics --days 7 --json >> "$LOG" 2>&1 || \
  echo "[$TS] request metrics unavailable (diagnostic only)" >> "$LOG"

echo "--- trace 语义告警 (近 7 天真实对话, 巡检非门禁) ---" >> "$LOG"
$PY eval/trace_alert.py --days 7 >> "$LOG" 2>&1 || echo "[$TS] trace_alert 运行异常（不影响门禁）" >> "$LOG"
echo "--- golden 草稿回灌 (非门禁, 只产草稿供人审) ---" >> "$LOG"
$PY eval/golden_draft.py --days 1 >> "$LOG" 2>&1 || echo "[$TS] golden_draft 运行异常（不影响门禁）" >> "$LOG"
echo "--- 跨源对账 (trace ↔ agent.log ↔ monitor.log, 巡检非门禁) ---" >> "$LOG"
$PY eval/trace_reconcile.py >> "$LOG" 2>&1 || echo "[$TS] trace_reconcile 运行异常（不影响门禁）" >> "$LOG"
echo "--- 单帧预算哨兵 (最长文章帧 vs _DETAIL_FRAME_PER / 判官材料上限, 秒级, 非门禁) ---" >> "$LOG"
$PY eval/frame_budget.py >> "$LOG" 2>&1 || echo "[$TS] frame_budget 报「有文章超过单帧预算 / 判官材料被截断」或运行异常（非门禁，看上面逐篇明细）" >> "$LOG"
# Retention runs after readers finish and uses the same configured trace storage.
echo "--- trace 保留治理 (压缩 + 超 30 天删除; 明细即审计轨迹) ---" >> "$LOG"
$PY eval/trace_retention.py --apply >> "$LOG" 2>&1 || echo "[$TS] trace_retention 运行异常（不影响门禁）" >> "$LOG"
echo "--- 产物保留执行 (logs/archive + eval/report，明细即审计轨迹) ---" >> "$LOG"
$PY eval/artifact_retention.py --apply >> "$LOG" 2>&1 || echo "[$TS] artifact_retention 运行异常（不影响门禁）" >> "$LOG"
echo "--- PG 保留治理 (checkpoint 三张表 + 幂等账，超 30 天裁; 明细即审计轨迹) ---" >> "$LOG"
$PY eval/pg_retention.py --apply >> "$LOG" 2>&1 || echo "[$TS] pg_retention 运行异常（不影响门禁；明细见上方）" >> "$LOG"

# Only terminal DBOS execution trees expire; plans and pending tasks are retained.
$PY -m agent.scheduling.retention --apply >> "$LOG" 2>&1 || echo "[$TS] scheduling retention failed" >> "$LOG"

if [ "$fail" -eq 0 ]; then
  echo "[$TS] ALL PASS" >> "$LOG"
  rm -f "$MARK"
else
  echo "[$TS] FAILED — 见上方输出" >> "$LOG"
  touch "$MARK"
fi
