# 架构决策记录（ADR）

| 编号 | 标题 | 状态 |
|---|---|---|
| [0004](adr-0004-cross-language-guard-in-ci.md) | 公开契约源码固定版本，PR 检查无需私人凭据 | 已采纳 |
| [0017](adr-0017-versioned-rag-evidence.md) | 版本化公开语料、不可变向量代与正文证据约束 | 已采纳 |
| [0018](adr-0018-embedding-input-budgets.md) | 固定 tokenizer 的完整 embedding 输入预算与空间签名 | 提议中 |
| [0019](adr-0019-local-embedding-runtime.md) | 本地 embedding 共用成熟运行时、固定资产并保留 API | 提议中 |
| [0024](adr-0024-framework-kernel-and-domain-extensions.md) | 成熟执行内核、精简业务 harness 与领域能力包 | 已采纳（标准框架已落地，整体验收进行中） |
| [0030](adr-0030-task-tool-scope-and-resource-exit.md) | 工具按任务激活，资源耗尽直接报告受阻 | 已采纳 |
| [0034](adr-0034-context-task-budgets.md) | 工作上下文、分段摘要与持久任务预算 | 已采纳 |
| [0035](adr-0035-background-scheduling.md) | 管理员调度采用独立 DBOS Worker，Agent 保留 LangGraph | 已采纳 |
| [0036](adr-0036-explicit-product-composition.md) | 显式产品装配与共享层依赖方向 | 已采纳 |
| [0037](adr-0037-bounded-model-cleanup.md) | 模型连接清理有期限且失败后停止复用 | 已采纳 |
