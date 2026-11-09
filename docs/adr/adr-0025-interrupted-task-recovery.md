# ADR-0025：普通任务通过框架 checkpoint 显式续跑

- **状态**：已采纳；继续沿用任务累计预算，后台聊天范围由 ADR-0038 限定。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/5e3d89e18f77ea0edf5c89811deb1346f02e80aa/docs/adr/adr-0025-interrupted-task-recovery.md)。

## 决策与依据

停止取消当前 HTTP 流及协作式执行，保留框架已保存的进度。
继续通过绑定会话和任务的显式 `resume_task` 控制发起，不靠识别聊天中的“继续”路由，
也不重新发送原问题。task 是一次目标，thread 是会话框架坐标。

恢复先核对账号、角色、会话和当前任务，再读取公开子图状态继续未完成节点。
已提交工具响应按原调用 ID 复用，领域、来源和业务事实随 checkpoint 恢复。
子图结束但提交失败时仅重试提交，不能因此重调模型。

## 权限、资源与限制

继续不携带新问题、图片或批准凭据，不伪造用户发言。等待确认时只重展示原卡，
不执行写入。已结束、被替换、身份变化或已删除的任务拒绝旧控制。
同一签名操作仍由独立执行账处理重放，见 [ADR-0012](adr-0012-write-idempotency-ledger.md)。

每次继续有新的 HTTP 截止时间，但继承同一任务的持久用量和剩余额度，
见 [ADR-0034](adr-0034-context-task-budgets.md)；不能靠反复继续重置预算。
恢复从已保存节点重新执行，不是接回原模型请求的下一个 token。
未提交读取可以重试，缺少 ToolMessage 不能证明远端写入没有发生。

原记录不代表断开网页后自动后台续跑，近期范围见
[ADR-0038](adr-0038-general-agent-and-release-priorities.md)。
当前控制协议见[聊天协议](../chat-stream-protocol.md)，
回归见 `tests/runtime/test_task_recovery.py`。
