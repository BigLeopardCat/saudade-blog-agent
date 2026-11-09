# ADR-0023：签名确认与框架持久审批恢复

- **状态**：已采纳；批量选择和卡片展示以当前确认协议为准。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/5e3d89e18f77ea0edf5c89811deb1346f02e80aa/docs/adr/adr-0023-confirmation-continuation.md)。

## 决策与依据

模型与工具循环使用 LangChain `create_agent`，作为持久子图接入 LangGraph 会话审批图。
准备工具冻结具体提议，`interrupt` 保存等待点，`Command(resume=...)` 恢复同一任务；
不序列化另一套业务计划或工具游标。

签名绑定账号、会话、有效期和具体操作，执行时再次检查身份、目标版本及权限。
框架恢复信号不能批准写入。取得真实回执后先持久提交事实，再继续后续工具或模型；
已完成调用复用结果，未知写入不能自动重做。

## 后续承接与限制

原记录“同一时间展示一张卡”不是当前 UI 上限。同类同事务目标可在一张卡内选择子集，
不同事务可有多张卡；每项执行仍须落在签名允许范围。当前协议见[身份与写入](../identity-and-permissions.md)。
准备状态不等于写入成功，多项变更不构成跨服务原子事务。

审批期间的新文本经原等待点作为反馈，撤销旧签名后继续，见
[ADR-0032](adr-0032-approval-feedback.md)。旧卡重放只能返回对应执行事实，
不能推进新卡、恢复其他任务或重复页面/设备操作。模型正文不包含确认凭据。

确认后的同一 Agent 根据回执回复，模型失败不重做业务写入；预算沿用同一任务。
原记录的旧 continuation 清空字段仅属滚动升级背景，不是可恢复接口。
回归见 `tests/test_confirmation_continuation.py` 和 `tests/test_pending_favorite.py`。
