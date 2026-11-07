# 评测与可观测性

## 分层验证

| 层 | 目的 | 依赖 |
| --- | --- | --- |
| 离线回归 | 工具、权限、确认、记忆和预算契约 | 桩或隔离 loopback，无部署凭据 |
| 数据库集成 | 真实事务、锁、恢复和幂等 | CI 临时 PostgreSQL，显式测试 DSN |
| 浏览器集成 | 布局、卡片、图片、输入及暂停接续 | 真实组件、合成接口、禁外网 |
| 真实模型任务 | 语义理解、完成率、用量和时延 | 固定模型、配置、构建和数据集 |
| 部署验收 | 鉴权代理、端到端业务及资源释放 | 经过授权的目标环境 |

离线通过不证明模型理解正确，卡片出现不证明正文真实，准备成功不证明业务写入完成。每层分别报告通过、失败、跳过和未评估。

## 数据集与判据

回归集按完整场景组织，覆盖角色边界、短应答、多轮指代、工具发现、跨领域、写入选择/取消、目标版本变化、断连恢复、摘要后回忆及错误服务。真实故障经归因后加入开发集，独立验收集保持隔离并轮换。

校准和正式评审均支持 `--reasoning-effort none|low|medium|high`；报告记录实际推理开关、输出预留及温度。比较使用相同配置，推理评审也不替代人工复核。

确定性断言检查真实工具、目标、执行账和数据库/浏览器事实。LLM judge 检查语义、虚构规则及表达，须用人工标注集校准。疑点未经复核不能算通过；语义评审不能覆盖确定性失败。

视觉模型已知不支持的场景单列，不放入文本任务分母；宿主图片展示仍需浏览器测试。

跨仓检查固定 `.github/host-contract.json` 的公开源码提交，CI 必须检出并运行，不读取维护者的本机博客 checkout。更新契约版本须验证双方接口后提交锁文件。

## 运行入口

```sh
uv run --frozen pytest -q
python -m eval.dataset_audit --out /tmp/dataset-audit.json
python -m eval.native_contracts --help
python -m eval.online_baseline --help
python -m eval.online_judge --help
python -m eval.release_readiness --help
```

线上 HTTP 验收使用 `online_baseline`；隔离写入/回放使用 `native_contracts`，
旧语料夹具共用 `golden_evaluation`。语义复核只通过 `online_judge` 审评固定报告，
普通离线测试统一由 pytest 收集，不在生产 cron 中串联这些入口。

线上先 POC，后全量，命令和目标见[发布验收](release-acceptance.md)。不得在普通 CI 默认运行线上写入或付费生图；生产材料交给外部 judge 前需脱敏和授权。

## 追踪与指标

每条 trace 关联构建、模型、用户来源、任务、请求及执行阶段，保留工具参数的安全投影、结果状态、错误及退出原因。密钥、签名确认凭据、原始私有图片不写 trace。

区分首个可见事件、首个正文、端到端时间和人工等待。任务级聚合包含暂停、浏览器和确认接续；当前请求只计增量，摘要及收尾调用也纳入费用。

完整 usage 同时报告缓存输入、未缓存输入、输出、推理及缺失字段。以服务商口径归一化；不能把十几个字的用户输入等同于实际完整模型输入。缓存命中率按 token 加权，不平均每条请求百分比；实际费用累计服务商回执。

基线 `task_metrics.tasks` 用 `case_ids` 关联场景，保留跨请求的模型调用数、输入、缓存和覆盖率。
`provider_cost_usd` 是服务商回执中的余额扣费，`provider_list_cost_usd` 是标价，
`provider_allowance_usd` 是套餐额度扣减；三者分别报告，套餐余额扣费为0不等于没有消耗。
某字段未覆盖全部调用、有未知请求或任务预算历史不完整时，该合计为null；
原始 `*_sum` 和 `*_seen` 只表示已观测部分，不能用其或预算预留补齐任务总费用。
少量POC或脚本模型只验证接线、分页与判据，不能当作模型意图通过率、正式P95或生产SLA。
短指代展示用例不强迫重复读取：自动判据检查图片格式和禁止行为，跨轮链接来源仍需语义评审。

```sh
python -m eval.daily_metrics --days 7 --json
python -m eval.daily_metrics --days 7 --tasks
python -m eval.daily_metrics --days 7 --json --evaluation-report /private/run/report.json
python -m eval.token_cost_report --days 7
python -m eval.task_metrics --dir /path/to/traces --out /tmp/tasks.json
```

日报默认统计全部服务流量，包含线上评测；`source=production` 表示服务通道，不等于真人流量。
`daily_metrics` 和 `task_metrics` 可重复传入 `--evaluation-report`，按 canonical 报告中的精确请求 ID、构建及捕获摘要生成已知评测与排除视图。顶层总量和总成本保留，未标注流量不推断为真人；原始 trace 和报告不改写。运行服务的健康统计只展示含评测的全部请求范围，不读取私有评测报告。

`TRACE_DIR` 是默认根，CLI 显式目录优先；`BLOG_ROOT` 仅供宿主日志诊断。无宿主日志或用量缺失时标记不可用，不补零。

## 比较与发布

固定输入、身份、模型、价格配置和构建做配对比较，报告新增退化、样本数、Wilson 95% 区间及成本/时延变化。小样本不能证明生产 SLA，未显著不同也不等于等效。费用按提供商当时实际价格核算。

目标来自 `eval/release_profile.json`；失败明细和原始报告保留。禁止以修改 matcher、删除失败用例或补录成功标签代替修复。候选流程见[离线改进](offline-improvement.md)。

模型请求的 `request_layout` 记录 SDK 输入指纹，`wire_request` 记录实际 HTTP JSON
和会话路由头的指纹；两者均不保存消息、推理文本或凭据。超过 1 MiB 或不支持的
请求体显式记录 `wire_request_unavailable`，不影响发送，也不用于证明前缀稳定。
