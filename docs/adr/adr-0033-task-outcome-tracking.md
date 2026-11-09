# ADR-0033：按需任务清单与准备工具的暂停契约

- **状态**：已采纳；原预算数值和验收进度不作为当前配置或质量结论。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/5e3d89e18f77ea0edf5c89811deb1346f02e80aa/docs/adr/adr-0033-task-outcome-tracking.md)。

## 决策与依据

复杂任务按需加载 LangChain TodoListMiddleware 的 write_todos，
清单、schema、Command 和重复更新检查复用框架。简单对话不强制规划或额外模型调用。
事项保留简短目标、状态和必要证据引用，不存长推理，也不由应用解释成执行 DSL。

清单由当前任务 checkpoint 持有，审批和恢复保留，新任务重置。
模型勾选完成既不授予写权限，也不证明任务真的成功；结果必须与业务回执对照。

准备工具返回后，同一 Agent 生成可交付回复再等待批准，
以 tool_choice=none 和服务端检查共同禁止这段回复新增调用。
回复可使用此前读取事实，不能把准备状态描述成已写入。
确认后追加真实执行结果，再继续剩余事项，不另建叙述 Agent。

## 当前承接与验证

原记录的 16K 窗口、64K 任务预算已被后续预算决策替代，见
[ADR-0034](adr-0034-context-task-budgets.md)；
默认值以 `config/settings.py` 为准。空答复和畸形答复的有限修复也受同一预算约束。

清单不能保证语义完整。总结正文、确认范围、实际效果、后续导航及旧授权回放分别验收，
不能以“弹出确认卡且没有写入”宣称复合任务完成。
当前证据要求见[发布验收](../release-acceptance.md)，
实现见 `agent/runtime/task_planning.py`。
