# ADR-0024：成熟执行内核、精简业务 harness 与领域能力包

- **状态**：已采纳；近期范围和长期任务协调优先级以 ADR-0038 为准。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/538b84c56820979d8dbb49dcbf7ff0938a396c93/docs/adr/adr-0024-framework-kernel-and-domain-extensions.md)。

## 背景与决策

自建执行内核尚无证据证明能在成本、延迟或可靠性上优于成熟框架。
维护投入优先用于业务工具、上下文、交互及任务结果，通用模型循环和恢复复用现有生态。

| 层次 | 职责 |
| --- | --- |
| 框架执行内核 | LangChain 模型/工具循环，LangGraph state、checkpoint、interrupt 与恢复 |
| 应用适配 | 可信身份、预算、权限接入、事实来源和结构化事件 |
| 领域能力 | 业务接口、目标与版本、审批需求、真实回执 |
| 产品 | 人设、站点规则、卡片和客户端交互 |

框架审批负责等待和接续，业务仍负责签名、授权、幂等与未知结果；
checkpoint 不提供跨服务 exactly-once。领域通过标准工具和公开 middleware 接入，
新增领域不另建 planner、executor、DSL 或调度状态机。

## 当前承接与取舍

`AgentRuntime` 和 `ToolSession` 适配服务协议，产品从应用入口显式装配，见
[ADR-0036](adr-0036-explicit-product-composition.md)。DBOS 负责后台调度，见
[ADR-0035](adr-0035-background-scheduling.md)。

原分阶段迁移不是当前待办清单；当前循环及持久恢复已经由框架承接。
通用任务协调仍延期，具体取舍由 [ADR-0038](adr-0038-general-agent-and-release-priorities.md) 补充。
小规模跨产品夹具证明装配边界，不能宣称已完成第二个真实产品迁移。

替换框架须保留授权、确认、恢复、取消、预算、真实回执与界面行为，
并以同一模型和固定任务比较质量、延迟与完整成本。接入成功或测试数量不能代替产品效果，
样本不足也不能声称稳定成功率。当前验收要求见[发布验收](../release-acceptance.md)。
