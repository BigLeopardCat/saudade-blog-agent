# ADR-0020：会话存储使用薄接口与框架 checkpoint

- **状态**：薄存储边界已采纳；旧图适配由 ADR-0024、轮次提交由 ADR-0031 承接。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/5e3d89e18f77ea0edf5c89811deb1346f02e80aa/docs/adr/adr-0020-conversation-store.md)。

## 决策与依据

会话持久化复用 LangGraph saver 和公开状态 API，应用提供薄的 `ConversationStore` 协议。
加载快照、删除屏障及受控历史维护属于存储适配，模型循环、审批与节点恢复交给框架。
配置后的存储故障向调用方传播，不静默换成内存后端。

快照提供标准消息、当前任务、待确认绑定和已知执行事实。`unfinished` 也可能表示子图已结束、
最终对话尚未提交；状态探针不触发恢复。显式恢复重新验证身份、会话和任务坐标。
维护操作不得偷偷结束活动任务。

## 当前承接与限制

`CheckpointConversationStore` 在 `agent/runtime/checkpoint_conversations.py` 实现协议。
持久轮次由父图提交节点完成，见 [ADR-0031](adr-0031-framework-turn-commit.md)；
审批及普通任务分别见 [ADR-0023](adr-0023-confirmation-continuation.md)、
[ADR-0025](adr-0025-interrupted-task-recovery.md)。

历史可见性受当前身份及来源过滤；删除屏障拒绝迟到写入。
没有 saver 时不能提供持久恢复或确认写入。checkpoint 保存失败不撤销已经发生的业务效果，
执行账和业务版本仍独立工作，见 [ADR-0012](adr-0012-write-idempotency-ledger.md)。
当前维护语义见[会话与记忆](../conversation-memory.md)。
