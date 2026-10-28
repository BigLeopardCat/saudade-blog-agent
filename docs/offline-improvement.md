# 离线反馈与候选发布

流程：冻结现场 → 复核反馈 → 隔离数据 → 生成提案 → 审查并提交 →
配对评测 → 一次独立验收 → 发布审查。它不进入在线 Agent 执行路径，
代码和部署继续由 Git 与人工审查管理。

## 复核反馈

`golden_draft`、用户纠错和 LLM judge 都只提供信号。使用原 trace 或单条草稿 JSON
作为 `--source`，冻结现场，保留原始字节摘要；源文件清理后仍可复核。
`--label` 为 JSON，必要字段如下：

```json
{
  "decision": "confirmed_issue",
  "category": "persona",
  "severity": "medium",
  "method": "code_audit",
  "reviewer": "实际复核者",
  "source_group": "同一会话或事件的匿名分组",
  "rationale": "依据是什么，哪些原因仍不确定",
  "evidence_quotes": ["现场中确实存在的原文"],
  "model_export_approved": false
}
```

`decision` 还支持 `not_issue`、`insufficient_evidence`；`method` 支持 `human`。
代码审计不得假称人审，judge 不得自行生成已复核标签。引用检查只证明出处，
不能自动证明复核结论正确。每次复核生成新记录，不覆盖旧判断。

```bash
python -m eval.improvement --registry /private/improvement review \
  --source /private/trace.json --label /private/label.json
```

调用优化模型还须提供匿名 `case`、`optimization_goal`，并将
`model_export_approved` 明确设为 `true`。`case` 使用当前 HTTP 用例格式，须有结果断言。
模型只收到这两项和指定源文件；原 trace、引用和复核备注不会自动导出。
脱敏由复核者负责，布尔字段不是自动识别隐私的保证。

## 冻结数据与候选

两个 JSONL 每个场景都需标明 `source_group`。同一事件的不同表述及同一会话片段共用分组。
封存检查编号、归一化原问和来源组重叠；语义近似和真实血缘仍需独立策展者核查。

```bash
python -m eval.improvement --registry /private/improvement seal \
  --development /private/development.jsonl --holdout /private/holdout.jsonl \
  --curator '策展者' --independence-review '来源、去重与未参与调优的复核说明'
python -m eval.improvement --registry /private/improvement prepare \
  --datasets DATASET_ID --review FEEDBACK_ID --base BASE_SHA --max-candidates 1
python -m eval.improvement --registry /private/improvement generate --input INPUT_ID
```

命令返回内容寻址的 artifact ID。注册目录权限须为 `700`，文件为 `600`；现场、私有用例
与产物不得提交。初版只生成博客目录说明与人设文件的提案，每批最多三个候选，
一次模型调用、最多2048输出 token、无自动重试。非法输出也保留尝试证据与已取得的 usage。
`generate --response /private/proposal.json` 可离线导入同格式提案。

提案不会被执行或自动应用。审查、修改并在分支提交后冻结实际 Git diff：

```bash
python -m eval.improvement --registry /private/improvement freeze \
  --proposal PROPOSAL_ID --index 0 --head CANDIDATE_SHA --reviewer '审查者'
```

允许审查者改进提案，但提交须从基线演进且只修改支持的文件。授权、确认、预算、
评测集与门槛不在自动提案范围内。文件范围限制不能代替代码审查。

## 评测与发布衔接

对 seal 返回的 `development_suite`，分别运行现有
`eval.online_baseline --phase full --suite ... --expected-build SHA`，
再用 `eval.online_judge` 评审。沿用真实测试账号、首跑不重采样、准备操作不批准写入。
两侧必须使用相同评测器、rubric 和 judge 模型，并附相同 judge 的正负样本校准：

```bash
python -m eval.improvement --registry /private/improvement development \
  --candidate CANDIDATE_ID --before /private/before/report.json \
  --after /private/after/report.json --before-semantic /private/before-judge/report.json \
  --after-semantic /private/after-judge/report.json --calibration /private/calibration/report.json \
  --reviewer '审查者' --comparison-conditions '缓存预热、网关负载和校准误差复核'
python -m eval.improvement --registry /private/improvement reserve-holdout \
  --development DEVELOPMENT_GATE_ID
```

开发门禁复用 `release_readiness` 和配对比较。确定性与语义结果不得出现已通过场景的退化；
未缓存输入与调用数的配对 bootstrap 区间上界须不大于零。样本不足、缺 usage、
缺语义复核、版本变化或校准不匹配均不能开放验收。校准结果不代表生产 judge 准确率。

reserve 会先校验覆盖和样本政策，返回 `holdout_suite`、`expected_build`。
通过以下入口调用现有线上基线，绑定预留编号并在模型调用前核验部署 SHA；
同一预留只能执行一次，中断也不能重试。运行后用现有 `eval.online_judge` 做语义评审：

```bash
python -m eval.improvement --registry /private/improvement run-holdout \
  --reservation RESERVATION_ID --report-dir /private/holdout --host \
  --admin-uid ADMIN_UID --user-uid USER_UID --zako-uid ZAKO_UID
python -m eval.improvement --registry /private/improvement release-packet \
  --reservation RESERVATION_ID --report /private/holdout/report.json \
  --semantic /private/holdout-judge/report.json --reviewer '发布审查者'
```

独立验收须达到当前政策的最低完整场景数，覆盖三种身份、聊天、读取、写入准备、
记忆与关键安全标签，所有结果及语义复核通过才得到 `ready_for_review`。
材料保留原报告与 trace、候选及回滚 SHA、统计和仍需独立验证的范围。
`blocked` 返回非零退出码；不会自动合并、部署或批准业务写入。

开放即消耗验收数据，中断和失败也一样。原问或来源组重叠的下一批同样拒绝，
不能改文件名或案例编号重用。失败后将暴露材料转入开发回归，收集新的独立场景。
此约束依赖操作者共用同一注册目录并遵守策展流程，不是防恶意篡改系统。

旧 golden 与 judge 开发校准集已经参与优化，不能改名冒充独立验收集。
合成测试仅验证流程；当前尚无独立策展者封存的生产验收集。
浏览器实际执行、批准写入、故障恢复和运行可靠性仍按
[发布验收](release-acceptance.md) 提供另外的证据。
