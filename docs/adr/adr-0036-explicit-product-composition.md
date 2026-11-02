# ADR-0036：显式产品装配与共享层依赖方向

- **状态**：已采纳（2026-11-02），实现与验证随本轮交付。
- **承接**：[ADR-0024](adr-0024-framework-kernel-and-domain-extensions.md)。
- **影响范围**：运行时构造、会话适配器命名、领域权限和确认提案依赖。
- **判据**：`tests/runtime/test_application_boundaries.py`、跨产品装配和框架持久恢复测试。

## 背景

运行时已有显式应用参数，却仍在缺省路径构造博客应用；领域模块通过博客工具目录
取得通用权限检查，通过博客展示模块创建通用确认提案。这些依赖使复用边界不完整。
`NativeRuntime`、`NativeConversationStore` 名称也继续表达已结束的迁移阶段。

## 决策

运行时类为 `AgentRuntime`，模块为 `agent/runtime/service.py`；
LangGraph 会话适配器为 `CheckpointConversationStore`，模块为
`agent/runtime/checkpoint_conversations.py`。保留现有 `ConversationStore` 协议与快照。
不保留旧类和导入路径别名，不改变已持久化消息、任务、审批或执行账结构。

运行时必须接收显式的 `application`，不接受产品工具目录参数，也不默认选择博客。
`agent/composition.py` 负责服务的博客装配、checkpoint、调度及模型传输资源。
后台博客任务仍由其应用装配选择只读博客产品；通用运行时不据此增加特殊分支。

领域模块直接使用已有 `agent/runtime/access.py` 权限接口。
通用签名提案移入 `agent/proposals.py`，保持原有凭据、卡片和待确认状态契约；
博客展示与执行接入保留在产品层。领域和通用运行时都不得导入 `agent.products`。
使用跨产品行为测试及依赖检查防止回流，不新增依赖注入容器或插件框架。

## 范围与后续

继续使用 LangChain `create_agent`、LangGraph checkpoint/interrupt 和 DBOS。
此次重构不替换框架循环，不重建规划器，不创建执行 DSL。
已有 trace 事件和评测材料中的 `native` 标识属于已发布数据契约；类的命名清理
不重写历史报告或改变这些事件的含义。

注册表静态导入并不等于工具全部进入模型 schema。真正需要独立安装能力包、
可选依赖隔离或观测到启动瓶颈时，再评估显式延迟导入或 Python entry points。
模块整合依据重复职责和依赖关系；不以文件数量少为目标。

本轮使用标准库静态依赖检查遍历runtime内导入（含函数内导入），未发现该包内部的直接依赖环。
该检查不覆盖动态插件导入，不将其外推为全仓无环或全部业务通过。
