#!/usr/bin/env bash
# Nightly regression: tests/test_skills.py 单测 + 检索基准 + golden set（语料 128 条，夜间实跑 127
# ——唯一的例外是那条会真写生产库的用例，它按设计不由任何无人看着的跑法触发）+ 巡检
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
# 20260925 补：回复出处评审（eval/llm_judge.py，非门禁）——紧跟 golden 之后跑，评的正是刚那
# 一轮的 trace（不传 --traces 时它取最新一轮）；只出报告 eval/report/judge_<ts>.md，可疑**不是**
# 失败（它挑可疑样本、不是判分器，见模块头注纪律 1），故不置 fail=1、也不写 health.log。
# 20260925 补：语料漂移哨兵（eval/corpus_terms.py --drift，非门禁）——见下方该节的注释。
# 同日补：真写夹具残留哨兵（eval/golden_fixture.py --verify，非门禁，只读公开接口）——
# 见下方该节的注释。**真写用例本身不在夜间跑**（needs_real_write 默认关，见 run_golden.py）。
# 20260924 补：跨源对账（eval/trace_reconcile.py，把 trace ↔ agent.log ↔ monitor.log
# 三个源对起来看，非门禁）——单源规则扫描看不见"两个源之间"的错（轮次自洽、前端只见
# 报错那类），报告进 eval/report/reconcile_<ts>.md，异常时另写一条 WARN 到
# logs/health.log（与一分钟心跳探针同一条通道）。
# 20260925 补：产物保留登记表 + 执行者（eval/retention_manifest.py + artifact_retention.py，
# 末尾一节）——先统一回答"盘上每一类产物归谁清"，再让它真的清。同族坑第四次
# （R2 --keep 3 / logrotate rotate 14 / logs/archive / eval/report/runs）。
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
echo "--- golden set (127 条真实对话, 约 25 分钟；另有 1 条真写用例按设计不在此处跑) ---" >> "$LOG"
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
# 20260925 起：回复出处评审（LLM-as-judge，非门禁）。确定性判据判"该出现的东西在不在"，
# 判不了"回复里有没有编出材料之外的事实"（工具只回了 3 条、回复写"共 5 条"）——这一格由它补。
# **刻意不进门禁**：同源模型评自己不构成 ground truth，"可疑"不等于"错了"（模块头注纪律 1），
# 置 fail=1 会让整夜门禁被一条观察性判据带红（哨兵一响就没人看了）。报告落
# eval/report/judge_<ts>.md，每条可疑条目都附材料原文供人核——**报告要人看**。
# 材料供给是它的前置条件：golden 跑法已把 trace 的工具返回上限放开（TRACE_TOOL_RESULT_LIMIT=40000，
# 生产是按工具分档），拿被截断的材料会把"文章里真有"的内容判成编造；判官认出截断会响亮警告。
# 跑在 golden 之后、不传 --traces（默认取最新一轮 = 刚那一轮）。
echo "--- 回复出处评审 (LLM-as-judge, 约 10 分钟, 非门禁) ---" >> "$LOG"
# PYTHONUNBUFFERED 必须留着（20260925 实测）：stdout 重定向到文件时 Python 默认**块缓冲**，
# 这一步要跑十分钟，块缓冲的表现是"日志里什么都没有、报告却已经写完"——中途想看一眼进度
# 只能看到空文件（实测过一次，误判成"跑了 0 字节"）。加它只影响缓冲、不改判据。
PYTHONUNBUFFERED=1 $PY eval/llm_judge.py >> "$LOG" 2>&1 || echo "[$TS] llm_judge 运行异常（非门禁，看 eval/report/judge_*.md）" >> "$LOG"
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
# 20260925 起：单帧预算哨兵（`agent/context.py::_DETAIL_FRAME_PER`，非门禁）。
# 起因：那个常数的注释原写「覆盖站内全部文章正文长度」，而实测最长那篇去重后 26,847 字
# ——**这句话早就不成立**，且没有任何东西会告诉你它不成立：超预算的帧会走按节节选
# （保底正确、planner 少看一截、多花一轮），日志与 golden 里都看不出来。
# 非门禁的理由同语料漂移哨兵：它报的是"该维护常数了"，不是"今天的改动坏了"。
# 有超时它退出 1，这里只留一行结论 + 它自己打的逐篇明细（就在上一行日志里）。
echo "--- 单帧预算哨兵 (最长文章帧 vs _DETAIL_FRAME_PER, 秒级, 非门禁) ---" >> "$LOG"
$PY eval/frame_budget.py >> "$LOG" 2>&1 || echo "[$TS] frame_budget 报「有文章超过单帧预算」或运行异常（非门禁，看上面逐篇明细）" >> "$LOG"
# 20260925 起：trace 保留治理（压缩 + 按保留期删）。**放在最后**：上面三步都要读 trace，
# 保留期 30 天远大于它们的窗口（7 天/1 天），所以删不到它们要的东西。
# 为什么必须有人执行：logrotate 那块的 `rotate 14` 对"文件名唯一"的 trace **从来无效**
# （`.2.gz`/`.3.gz` 各 0 个、最老文件 26 天）——保留策略写进配置不等于会执行，
# 这里才是真的执行者。删了什么会逐条落进本日志（这就是审计轨迹）。
echo "--- trace 保留治理 (压缩 + 超 30 天删除; 明细即审计轨迹) ---" >> "$LOG"
$PY eval/trace_retention.py --apply >> "$LOG" 2>&1 || echo "[$TS] trace_retention 运行异常（不影响门禁）" >> "$LOG"
# 20260925 起：产物保留执行（表 = eval/retention_manifest.py）。它管的是 trace 以外那几类：
# `logs/archive/` 的保留期（90 天，审计留档与 .sql 快照由 frozen 登记项排除）、
# `eval/report/` 每族报告保留最近 20 份、词图构建中间产物保留最近 10 套。
# **这里不写任何路径与天数**——表是唯一事实源，改保留期改表里的常量，命令行不动。
# 为什么必须有这一步：`logs/archive/` 曾零引用、最老到 2026-06-10，而 `runs/` 576 份零策略
# ——同族坑第四次（前三次：R2 `--keep 3`、logrotate `rotate 14`、traces 保留期）。
# 放在最末：上面几步产出的报告（review_/reconcile_/corpus_drift_）都还在"每族 20 份"内，
# 一天落不了 20 份。删了什么逐条落进本日志 = 审计轨迹。
echo "--- 产物保留执行 (logs/archive + eval/report，明细即审计轨迹) ---" >> "$LOG"
$PY eval/artifact_retention.py --apply >> "$LOG" 2>&1 || echo "[$TS] artifact_retention 运行异常（不影响门禁）" >> "$LOG"

if [ "$fail" -eq 0 ]; then
  echo "[$TS] ALL PASS" >> "$LOG"
  rm -f "$MARK"
else
  echo "[$TS] FAILED — 见上方输出" >> "$LOG"
  touch "$MARK"
fi
