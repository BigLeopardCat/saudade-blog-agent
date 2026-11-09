# ADR-0036：显式产品装配与共享层依赖方向

- **状态**：已采纳；跨产品夹具不代表第二个真实产品已交付。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/538b84c56820979d8dbb49dcbf7ff0938a396c93/docs/adr/adr-0036-explicit-product-composition.md)。

## 背景与决策

通用运行时即使接受应用参数，若默认构造博客应用或经博客目录检查通用权限，
复用仍受产品依赖限制。运行时必须显式接收 application，
由 `agent/composition.py` 装配博客、checkpoint、调度和模型资源。

AgentRuntime 与 CheckpointConversationStore 表达职责，避免用已结束的迁移阶段命名。
领域直接使用共享权限与提案接口，领域和通用 runtime 不导入具体 products；
人设、博客上下文和展示留在产品层。此类装配调整不改变持久消息、确认或执行账的语义。

## 取舍与验证

新增领域经标准工具及 middleware 接入，继续使用 LangChain、LangGraph 和 DBOS。
共享层不默认选择博客，也不增加容器、插件框架、planner 或执行 DSL。

静态 Python 导入不等于所有工具 schema 都进入模型输入。
只有实际出现启动瓶颈、可选依赖隔离或独立分发需求，才评估延迟导入与 entry points。
模块调整依据职责和依赖，不能只以文件数量衡量维护性。

依赖检查和跨产品行为夹具防止产品依赖回流，见
`tests/runtime/test_application_boundaries.py`、
[产品扩展](../product-extensions.md)。
这些验证仅证明覆盖到的边界，真实跨业务迁移仍需第二个产品的接入证据，
近期范围见 [ADR-0038](adr-0038-general-agent-and-release-priorities.md)。
