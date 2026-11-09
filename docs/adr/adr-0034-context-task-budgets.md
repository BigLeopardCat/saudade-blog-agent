# ADR-0034：工作上下文与持久任务预算

- **状态**：已采纳；容量与费用口径以当前配置和提供商归一化契约为准。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/538b84c56820979d8dbb49dcbf7ff0938a396c93/docs/adr/adr-0034-context-task-budgets.md)。

## 决策与依据

模型上下文窗口、单次工作输入与全任务累计额度分别限制，
使用 create_agent、标准 SummarizationMiddleware 和 LangGraph checkpoint。
完整输入计入系统消息、历史、工具 schema 和参数，输出另作预留，不能重复扣除。
端点有大窗口不意味着每次应填满，也不代表费用或检索质量达标。

摘要与接续共享任务预算。用量先持久预留，得到真实 usage 后结算；
进程失联或用量未知保留预留。暂停、确认、继续不能重置任务额度，
新 HTTP 只统计本次增量，不能重复计费历史累计量。

原始任务要求、执行中补充及待确认事实受保护。压缩不拆工具交换，
不授予权限或丢弃目标来绕过预算；超大当前任务明确拒绝。
人工等待不计活动执行时间，保守预留不能冒充实际墙钟延迟。

## 后续承接与限制

原记录的具体容量、摘要阈值和部署模型窗口属于当时配置，
当前唯一默认值见 `config/settings.py` 与 `.env.example`；
工具 schema 另有工作集软目标，见 [ADR-0030](adr-0030-task-tool-scope-and-resource-exit.md)。

提供商对 input 是否包含 cache_read 的定义可能不同，先归一化再计算总输入与命中率。
缺失 usage 保持未知，无价格不猜费用。请求指纹只记录哈希和规模；
首次观测不等于冷缓存，同请求也不保证命中。

当前记忆、预算和缓存契约见[会话与记忆](../conversation-memory.md)，
回归见 `tests/runtime/test_task_budget.py`、`tests/runtime/test_task_time_budget.py`。
长期用户偏好自动写入不属于此决策，需另行设计归属、来源及更正删除。
