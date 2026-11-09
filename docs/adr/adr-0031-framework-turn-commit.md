# ADR-0031：持久任务由框架节点提交轮次

- **状态**：已采纳；消息投影由当前框架 transcript 承接。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/5e3d89e18f77ea0edf5c89811deb1346f02e80aa/docs/adr/adr-0031-framework-turn-commit.md)。

## 决策与依据

父执行图通过 `agent → commit → approval/END` 提交轮次。
Agent 子图输出待提交结果，提交节点保存来源匹配的框架消息、展示正文、
执行事实和任务状态；提交成功后清除临时 turn，待审批任务保留恢复坐标。

提交失败后复用框架保存的数据，仅重试提交节点，不重复模型或已完成工具。
后端报错可能发生在部分持久写入之后，不能把异常直接解释为事务回滚；
恢复须检查实际 state、pending writes 和业务执行账。

## 当前承接与限制

原记录的应用消息投影已由 `agent/runtime/transcript.py` 规范化框架消息承接，
`agent/runtime/turn_commit.py` 提交同一份完成正文和已知结果。
内部控制文本、推理和内联图片不进入对话展示。
最终保存不依赖浏览器消费完最后一个 SSE 帧。

普通完成、审批接续和反馈不再通过图外维护方法结束任务。
维护写入必须拒绝覆盖活动任务；旧授权重放只能读对应执行事实。
身份变化后的回执不能恢复旧身份的上下文。

图内提交不提供跨服务 exactly-once，也不证明模型完成了用户所有事项。
当前控制及消息契约见[聊天协议](../chat-stream-protocol.md)、
[会话与记忆](../conversation-memory.md)。
