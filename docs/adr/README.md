# 架构决策记录

这里保存仍影响维护的决策理由、替代方案与正确性边界。操作步骤和接口以当前专题文档为准；
ADR 保留编号，并在实现变化时明确哪些原则继续有效、哪些局部方案已被取代。

[ADR-0038：通用 Agent 长期架构与正式版优先级](adr-0038-general-agent-and-release-priorities.md)
继续限定近期交付范围；恢复旧决策不重新启动已延期的通用任务协调或退役实验。

## 执行内核与产品边界

| 决策 | 当前状态 |
| --- | --- |
| [ADR-0024：成熟执行内核、精简业务 harness 与领域能力包](adr-0024-framework-kernel-and-domain-extensions.md) | 已采纳；近期范围和长期任务协调优先级以 ADR-0038 为准。 |
| [ADR-0028：普通工具使用框架 handler 执行](adr-0028-framework-tool-dispatch.md) | 已采纳。 |
| [ADR-0029：任务状态以框架 checkpoint 为准](adr-0029-framework-task-state.md) | 已采纳；工具目录字段沿用当前有界工作集实现。 |
| [ADR-0031：持久任务由框架节点提交轮次](adr-0031-framework-turn-commit.md) | 已采纳；消息投影由当前框架 transcript 承接。 |
| [ADR-0033：按需任务清单与准备工具的暂停契约](adr-0033-task-outcome-tracking.md) | 已采纳；原预算数值和验收进度不作为当前配置或质量结论。 |
| [ADR-0036：显式产品装配与共享层依赖方向](adr-0036-explicit-product-composition.md) | 已采纳；跨产品夹具不代表第二个真实产品已交付。 |

## 身份、写入与持久恢复

| 决策 | 当前状态 |
| --- | --- |
| [ADR-0012：写操作先记执行意图，未知结果禁止盲目重放](adr-0012-write-idempotency-ledger.md) | 已采纳；旧图接线及键格式已由当前签名操作执行路径替代。 |
| [ADR-0014：身份断言使用 PyJWT 并保留跨语言声明约束](adr-0014-assertion-verify-via-pyjwt.md) | 验签库与声明校验已采纳；原记录中的 Agent 自签业务和设备令牌已被宿主委托替代。 |
| [ADR-0020：会话存储使用薄接口与框架 checkpoint](adr-0020-conversation-store.md) | 薄存储边界已采纳；旧图适配由 ADR-0024、轮次提交由 ADR-0031 承接。 |
| [ADR-0022：会话历史按来源裁剪，图片按需回看](adr-0022-conversation-memory-boundaries.md) | 已采纳；取代 ADR-0013 中历史图片仅展示的范围限制。 |
| [ADR-0023：签名确认与框架持久审批恢复](adr-0023-confirmation-continuation.md) | 已采纳；批量选择和卡片展示以当前确认协议为准。 |
| [ADR-0025：普通任务通过框架 checkpoint 显式续跑](adr-0025-interrupted-task-recovery.md) | 已采纳；继续沿用任务累计预算，后台聊天范围由 ADR-0038 限定。 |
| [ADR-0032：审批反馈复用原任务并撤销旧预览](adr-0032-approval-feedback.md) | 已采纳。 |

## 工具、预算与资源

| 决策 | 当前状态 |
| --- | --- |
| [ADR-0030：工具生命周期与明确资源终止](adr-0030-task-tool-scope-and-resource-exit.md) | 预算退出与框架边界已采纳；原跨任务全部清空规则已由有界热工作集替代。 |
| [ADR-0034：工作上下文与持久任务预算](adr-0034-context-task-budgets.md) | 已采纳；容量与费用口径以当前配置和提供商归一化契约为准。 |
| [ADR-0027：同步 Agent 通过标准异步模型 I/O 响应取消](adr-0027-cancellable-model-io.md) | 已采纳；清理期限和失败后的池状态由 ADR-0037 补充。 |
| [ADR-0037：模型连接清理有期限且失败后停止复用](adr-0037-bounded-model-cleanup.md) | 已采纳。 |

## 检索与 embedding

| 决策 | 当前状态 |
| --- | --- |
| [ADR-0017：版本化语料、完整索引与正文证据约束](adr-0017-versioned-rag-evidence.md) | 语料与证据约束已采纳；原文件向量代和 manifest 已由 PostgreSQL/pgvector 替代。 |
| [ADR-0018：以固定 tokenizer 约束完整 embedding 输入](adr-0018-embedding-input-budgets.md) | 已采纳；原记录的提出状态已由实现承接，向量持久化沿用当前 PostgreSQL 方案。 |
| [ADR-0019：本地 embedding 复用 llama.cpp，并保留 API](adr-0019-local-embedding-runtime.md) | 本地运行时选择已采纳；其他引擎仍为候选，本文不声明当前部署状态。 |

## 跨仓验证与后台调度

| 决策 | 当前状态 |
| --- | --- |
| [ADR-0004：公开契约源码固定版本，PR 检查无需私人凭据](adr-0004-cross-language-guard-in-ci.md) | 已采纳。 |
| [ADR-0035：管理员持久调度](adr-0035-background-scheduling.md) | DBOS 调度边界已采纳；执行范围包含后来加入的显式批准定时公告。 |

## 保留与更新

- 清理文档时保留仍有效的设计理由、权限与事务边界、故障语义及明确取舍，去掉一次性执行流水账。
- 变更具体实现时补充当前承接和取代关系；原记录通过固定提交保留，不把旧配置、试点读数或未完成验收写成当前事实。
- 新的重要决策沿用编号。普通修复、测试结果或部署记录不机械新增 ADR；参数默认值和操作步骤只在对应配置与专题文档维护。
- 已被取代的阶段设计不恢复为当前方案，仍保留固定版本入口，以便核查当时背景。

## 阶段记录与取代关系

旧技能模板、双执行臂、自建图与路由实验保留在
[首次清理前的固定版本](https://github.com/BigLeopardCat/saudade-blog-agent/tree/5e3d89e18f77ea0edf5c89811deb1346f02e80aa/docs/adr)；
第二次清理前仍存在的决策见
[后续固定版本](https://github.com/BigLeopardCat/saudade-blog-agent/tree/538b84c56820979d8dbb49dcbf7ff0938a396c93/docs/adr)。
每篇恢复记录另有原文链接，当前承接已单独说明。

| 历史编号 | 当前查阅入口 |
| --- | --- |
| ADR-0001、0003、0008、0009、0015、0016、0021 | 技能模板、自建循环及实验运行时由 [ADR-0024](adr-0024-framework-kernel-and-domain-extensions.md)、[ADR-0028](adr-0028-framework-tool-dispatch.md)、[ADR-0036](adr-0036-explicit-product-composition.md) 承接 |
| ADR-0002、0010 | 旧意图表及累加器方案参照 [ADR-0029](adr-0029-framework-task-state.md)、[ADR-0033](adr-0033-task-outcome-tracking.md)，不恢复旧状态机 |
| ADR-0005、0006、0007 | 当时延期的检索、权限规则及旧重试问题只作历史；当前范围分别见 [RAG](../rag-design.md)、[身份与写入](../identity-and-permissions.md)、[发布验收](../release-acceptance.md) |
| ADR-0011、0013 | checkpoint 与图片历史由 [ADR-0020](adr-0020-conversation-store.md)、[ADR-0022](adr-0022-conversation-memory-boundaries.md) 承接 |
| ADR-0026 | 工具激活与跨任务范围由 [ADR-0030](adr-0030-task-tool-scope-and-resource-exit.md) 及当前有界热工作集承接 |

当前实现与契约见[架构](../agent-architecture.md)、[会话与记忆](../conversation-memory.md)、
[身份与写入](../identity-and-permissions.md)、[RAG](../rag-design.md)和[后台计划](../scheduling.md)。
