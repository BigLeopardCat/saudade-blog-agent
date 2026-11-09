# ADR-0029：任务状态以框架 checkpoint 为准

- **状态**：已采纳；工具目录字段沿用当前有界工作集实现。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/5e3d89e18f77ea0edf5c89811deb1346f02e80aa/docs/adr/adr-0029-framework-task-state.md)。

## 决策与依据

任务执行状态由 LangGraph checkpoint 持有。Session 是节点内工作视图，
不能维护独立属性和另一份可自行恢复的状态机。
节点边界刷新工作视图，返回框架更新时复制可变数据，避免后续调用修改已返回的状态。

工具激活、输入应用记录、待确认、客户端操作引用和终态结果使用 ExecutionState。
实际字段以 `agent/runtime/execution_state.py` 为准，
旧记录中的 loaded 及结果计数位置不能作为当前 schema。
模型、工具实例、连接、取消信号和传输缓冲属于请求，不进入 checkpoint。

## 更新与恢复边界

同批工具有序执行，Command 明确更新需要反映到批次工作视图；
各中间件不重复提交其他所有通道。效果合并使用框架 reducer，
新任务使用对应重置语义，避免跨任务残留。

resume 是经过验证、消费后清除的控制输入。确认回执只更新匹配的当前操作；
旧消息投影不能覆盖更新的已提交 state。
任务终态、模型正文及会话展示不能替代独立业务执行账。

当前状态职责见[架构](../agent-architecture.md)，
回归见 `tests/runtime/test_task_state_authority.py`。
工具继承与容量规则见 [ADR-0030](adr-0030-task-tool-scope-and-resource-exit.md)。
