#!/usr/bin/env bash
# Nightly regression: tests/test_skills.py 单测 + 检索基准 + golden set（127 条真实 LLM 用例）+ 巡检
# 由 crontab 触发（见仓库 README 或 crontab -l）。结果追加到 ~/agent_regression.log；
# 任一门禁项失败会在 ~/agent_regression.failed 留下标记（存在 = 上次运行失败）。
# golden 有 FAIL 时导出复审单 eval/report/review_<ts>.md（判据 vs 模型实际输出）——
# 复审规则（20260912）：假失败当轮修判据，真 FAIL 才允许挂着（否则门禁失去区分度）。
# 20260921 起 golden 分两层判：**回归组（tags 含 regression）硬判 100%**（不受
# --min-pass-rate 放宽），能力题仍按通过率——两类红的严重度不同，不许被平均数吸收。
# 20260924 起回归组红**先复跑一次再定论**（判据脆弱/采样波动不再直接废掉整夜门禁）：
# 复跑仍红=真 FAIL；复跑绿=按方差放行，但名单进报告 regression.flaked_ids 与复审单
# 置顶，日志里也单列——放行不等于静默宽恕。
# 同日补：trace_alert 抓到的现场回灌成 golden 草稿（eval/golden_draft.py，只产草稿
# 到 eval/report/ 供人审，不自动入库、非门禁）。
# 20260925 补：语料漂移哨兵（eval/corpus_terms.py --drift，非门禁）——见下方该节的注释。
# 同日补：真写夹具残留哨兵（eval/golden_fixture.py --verify，非门禁，只读公开接口）——
# 见下方该节的注释。**真写用例本身不在夜间跑**（needs_real_write 默认关，见 run_golden.py）。
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
echo "--- tests/test_skills.py (技能注册表/plan 契约, 秒级) ---" >> "$LOG"
$PY tests/test_skills.py >> "$LOG" 2>&1 || { fail=1; echo "[$TS] test_skills FAILED" >> "$LOG"; }
# 20260924 起：检索基准（recall@k / MRR，直接测线上 rag/search.py，秒级、无网）。
# README 里一直写着 nightly 跑 L1/L2 两项，实际只有 L2——这一节把它补齐。
# 非门禁：已知 FAIL 是词法表征的局限（脚本自己在报告里点名），不该让夜间任务变红。
echo "--- 检索基准 recall@k / MRR (直接测线上 rag/search.py, 秒级, 非门禁) ---" >> "$LOG"
$PY eval/recall_eval.py >> "$LOG" 2>&1 || echo "[$TS] recall_eval 运行异常（不影响门禁）" >> "$LOG"
echo "--- 语料漂移哨兵 (词表型断言 vs 语料, 秒级, 非门禁) ---" >> "$LOG"
# 20260925 起：扫 golden 里每个 text_contains 词——还在语料里吗（ORPHAN）/ 是不是满语料
# （GENERIC）/ 落点是不是该用例申报的那几篇（MISBOUND）；require_doc_terms 的派生集太小
# 报 THIN。动机就是 rag_ota_http：人抄的期望词随语料漂移，而漂移的表现是「用例继续红或
# 继续绿，没人知道判据已经不成立」。
# **非门禁**：退出码 1 只表示"有 ORPHAN/MISBOUND"，当前已知 9 条 ORPHAN（rag_fingerprint_*
# 的死支、rag_arch_check 的陈旧词、rag_python_copy 的深/浅拷贝）是**本轮范围外**的存量，
# 已进报告待点名——让它置 fail=1 只会让整夜门禁天天红（哨兵一响就没人看了）。
# 报告落 eval/report/corpus_drift_<ts>.md。
$PY eval/corpus_terms.py --drift >> "$LOG" 2>&1 || echo "[$TS] corpus_terms --drift 有 ORPHAN/MISBOUND 或运行异常（非门禁，看报告）" >> "$LOG"
echo "--- 真写夹具残留哨兵 (只读公开接口, 秒级, 非门禁) ---" >> "$LOG"
# 20260925 起：真写用例（golden_write_category_delete_exec）的目标是**夹具**
# （分类 agent_fixture_category_a，见 scripts/migration/golden_write_fixture_20260925.sql）。
# 那条用例跑完即把自己删掉，所以正常态是"公开分类列表里一行夹具都没有"。它没删掉
# （用例红在中途、或有人手工建了同族名字）时夹具会**留在生产库里，而且访客在分类页
# 看得见**——这件事不该靠"我记得跑过"来判断，要有只读的机械检查。
# 退出码：0 干净 / 1 有残留（[fixture-leftover] 逐行点名）/ 2 读不到接口（**无法确认**，
# 不是"没有"）。**非门禁**：真删不掉时金色的那条用例自己就是红的（fail=1 已经置位），
# 这里只是把"生产库里留了什么"讲清楚。
$PY eval/golden_fixture.py --verify >> "$LOG" 2>&1 || echo "[$TS] 真写夹具有残留或读不到公开接口（非门禁，见上面的 [fixture-leftover]/[fixture-check-failed] 行）" >> "$LOG"
echo "--- golden set (110 条真实对话, 约 20 分钟) ---" >> "$LOG"
# 20260924 起：给「需要真身份」的那类用例一个 uid，治那 5 条常年 SKIP（moderation_report_admin /
# user_report_admin / 三条 *_unresolved_target_honest）。721 是**测试专用管理员账号**（不是主人
# 的 uid=1——那条写用例会真改主人自己的数据），role=admin、口令已是不可知哈希，只为这条通道存在。
# 未设该变量时 run_golden 会**响亮跳过并打印**（跳过关乎通过率分母，不静默豁免）。
export GOLDEN_ADMIN_UID=721
# 20260924 起：普通用户那条通道（722，role=user）。作用是把「被 role 闸拦住」与「谁调不动」
# 分开——不给 uid 时 uid=0 是 agent 侧哨兵，`admin_write_denied_user` 会因为"谁都调不动"而
# 通过，通过的理由是错的。接线刻意排在 gate 能力否定误伤修好**之后**：误伤在时，这条用例
# 约一半概率被换成兜底道歉 ⇒ 夜间门禁 1.000 会间歇性变红（哨兵一响就没人看了）。
# 未设该变量时同样**响亮跳过并打印**（跳过关乎通过率分母，不静默豁免）。
export GOLDEN_USER_UID=722
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
