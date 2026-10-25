# 旧运行时兼容边界

当前主线只构建 `NativeRuntime`。`graph.py`、`hybrid.py`、`graph_store.py`、`legacy_bridge.py`、旧 planner 和 workflow DSL 已从源码删除，不能通过配置回退。

仍然受支持的兼容契约是业务契约：`thread_id`、会话历史、确认签名、撤销、幂等 receipt、SSE 帧和宿主公开字段。`NativeConversationStore` 通过 LangGraph saver 的公开状态接口保存这些数据；它没有业务图节点，也不保证普通读取可以从每个中间步骤恢复。

旧数据接续只接受可解析的消息、待确认事实和已完成轮次。无法恢复的未过期确认卡不会被覆盖或标记为已执行；未知写入结果必须先读已有回执。生产入口遇到未知 `AGENT_ARM` 直接拒绝。

过渡期仍可能收到旧 planner 字段。入口保留有业务意义的历史、页面、摘要、执行台账和待确认事实，其他字段忽略并记录；这些字段不会重新启用旧规划器。

宿主历史与摘要须携带生成时的来源范围，无来源的旧数据不自动注入模型。已有 checkpoint
继续接续，空 checkpoint 的宿主导入在锁内完成；详见[会话记忆契约](conversation-memory.md)。

验证方式：`tests/test_legacy_retirement.py` 在新进程中构建原生服务并触发工具路径，检查退役模块既无源码也未进入 `sys.modules`。兼容行为由 `tests/test_native_*`、确认、取消、存储和传输契约测试覆盖。
