# ADR-0021：原生轮次与会话存储脱离旧编排图

- **状态**：已采纳；原生核心、会话存储和传输消费者已接线；服务配置采用 native。
- **承接**：[ADR-0020](adr-0020-conversation-store.md)
- **范围**：NativeRuntime、NativeConversationStore、共享工具目录及原生确认接线。

## 决策

`NativeRuntime.stream_events(state, config)` 只产出标准 `AIMessageChunk` 与一个最终
`TurnResult`。文本块仅包含可见正文；最终结果的 reply 是完整、已保存的正文，不是再次追加的文本。
确认卡等结构化事实继续在 execution 中传递；进度仍走现有受限回调。
存储失败、取消或关闭生成器不会产出成功结果，关闭会传递到模型迭代器。

state 使用 messages 和可选的 confirm_grant；历史 plan、ledger、open_tasks 不成为执行依据。
原生入口不构建 create_agent，不调用 legacy.stream，不输出 planner/model/gate 等伪造节点更新。
模型菜单和提示词不再提供 delegate_to_site_agent；超限不会借另一引擎重置预算。
未知确认执行器拒绝执行。纯聊天角色不绑定任何工具 schema，授权仍由现有确定性代码复核。

## 存储与恢复

继续使用 ConversationStore 边界和现有 saver、会话锁、删除屏障、裁剪与执行账。
NativeConversationStore 用一个不可执行业务的 commit 节点，复用框架公开的 get_state/update_state
及消息 reducer；不自行实现 checkpoint 序列化、版本号或 SQL 存储。

会话状态只含 messages、pending_confirm、pending_action、receipts；更新指定 commit，
避免维护 API 推断旧 planner/gate 节点。线程编号不变，级联删除与跨进程锁继续定位同一会话。
旧 checkpoint 可投影读取这些数据，旧 next 和 done 不触发旧任务恢复。
底层 checkpoint 可能仍携带未知的旧 channel 数据；这些字段不暴露给原生状态或模型，
不在本批通过删除/重建会话强行清理。历史记录及业务执行账不做破坏性迁移。

仅保存完成轮次，未完成的纯读取可重试。确认写入使用独立执行账：完成后回放，
未知结果不重试；保存会话失败不能回滚已发生的业务副作用。
已完成旧操作的重放不能清掉后来的卡片；未知结果保留当前卡片以便核实。

可恢复的原生卡片继续跨聊天和进程重建保留；卡片凭据不进入模型提示词。
不可恢复的未过期旧卡保留，只开放读取与页面操作，不准备新的业务变更。
过期数据允许在完成轮次时清理，恢复准备能力；读取 token 的过期时间只用于清理数据，
不授予执行权，批准仍须完整验签、身份、有效期、参数和持久执行账验证。

## 接入与退役边界

服务默认入口已接入原生传输消费者，同步与 SSE 都消费正文块和最终 TurnResult，
不再解析旧节点更新。配置为 native；旧 graph/hybrid 配置在启动时明确拒绝，部署需同步更新。
共享 ToolCatalog 供兼容入口和新 Runtime 复用，避免维护两份授权菜单。
旧 graph/hybrid、planner DSL 和专用实验入口已经物理删除；Git 历史保留旧实现。

## 验证

测试使用真实 InMemorySaver、历史 checkpoint 夹具、模型流桩、MockTransport 业务接口及 SQLite
执行账，覆盖旧会话接续、流关闭、取消、存储失败、预算上限、零工具角色、确认恢复/重放/撤销、
未知结果不重复写、旧回执不覆盖新卡、过期卡清理、丢弃末轮和删除后迟到写入。
独立进程验证原生工厂不导入 agent.graph 或 agent.agent。
这些离线结果不等于 PostgreSQL 或真实模型的线上验收；全量线上基线仍挂起。
