# ADR-0020：原生会话存储采用薄接口，旧图细节留在兼容适配器

- **状态**：已采纳（20261024，本分支待合并）
- **背景文档**：[会话运行时审查](../session-runtime-review-20261024.md)、[迁移路线](../hybrid-runtime-migration.md)
- **影响范围**：runtime/conversation_store.py、graph_store.py、hybrid.py 及 confirmation.py 的存储接线；不改变默认运行模式
- **判据**：tests/test_conversation_store.py，以及现有 hybrid、memory、pending、turns 和 checkpoint 契约回归

## 背景

原生工具循环已覆盖现有业务工具，但历史和待确认保存在旧编排图的 checkpoint。
HybridArm 曾直接读取原始图状态，并在完成轮次时调用 graph_input、指定 gate 节点保存。
按钮取消另有一处相同的直接读写。存储语义和旧图字段初始化因此混在原生执行层。

## 决策

保留 LangChain/LangGraph 与现有 checkpoint，引入两个小模块：

| 模块 | 职责 |
| --- | --- |
| `agent/runtime/conversation_store.py` | ConversationSnapshot 数据及 ConversationStore 协议 |
| `agent/runtime/graph_store.py` | 现有图 checkpoint 的适配及原始维护 API |

快照只提供 messages、pending_confirm、pending_action、unfinished；不暴露 plan、cursor 等旧规划字段。
unfinished 表示原始图有 next 或明确 done=false。未能验签恢复的 pending 仍交回兼容路径，
不会当作普通聊天覆盖。`is_resuming` 仅保留原有 next 判据；两者用途不同，不合并口径。

接口限于现有需求：

- `load(config)`：读取会话数据；身份与角色的历史可见性继续由现有 memory 过滤。
- `save_turn(config, messages, reply, observations=..., execution=..., pending=...)`：
  合并最新历史、标记本轮来源身份、裁剪并保存完整轮次。pending 为执行层已验签且需保留的卡片，
  execution 中明确清除或替换的字段优先；普通聊天保留卡片而不重发确认帧。
- `clear_pending(config, jti)`：只清匹配卡片，不覆盖新卡、不改历史、回执或执行账。
- `guard(config)`、`prune(config)`：复用现有整轮串行锁、删除屏障和框架裁剪。
- `is_resuming(config)` 与 `saver`：兼容现有恢复探针及独立持久执行账；available 表示读写可用。

读改写与清卡由调用方在同一 guard 内完成；save 不再次获取非重入锁。
适配器写入口检查取消/时限与删除屏障，防止删除后的迟到保存重新创建会话。
无 checkpoint 时保持原有无持久化行为；配置后读写失败传播，不静默改用内存后端。

GraphConversationStore 内部继续用 graph_input 初始化兼容状态，并以 gate 写回。
这样既可读取已有会话，也不需要新增表、迁移或第二份旧状态初始化逻辑。
按钮取消仅替换存储调用，签名、撤销、业务执行及幂等账使用既有实现。

## 取舍与恢复边界

只保存已完成轮次，不为纯读取的每一步写 checkpoint。
模型中断、进程退出或保存失败后，本轮未持久化的读取可以重新执行。
这不代表可从每次工具调用的崩溃点恢复，也不构建事件溯源或通用状态 DSL。

业务写入执行账和会话 checkpoint 各有职责。写入结果先落执行账，再保存会话结果；
会话保存失败或请求取消不能回滚已记账的副作用。重复确认继续回放已有结果，
未知结果继续拒绝自动重写。已有读写失败回归与取消行为测试验证这条边界。

保留的兼容依赖：

- 默认工厂仍构造旧图，并用同一个图创建默认存储适配器；legacy.stream 的回退尚未退役。
- HybridArm 的原始 get_state/update_state、异步包装及 graph 别名仍供现有恢复、丢弃末轮和诊断调用。
  原始维护调用通过兼容适配器，不属于 ConversationStore 协议。
- 注入其他协议实现只验证原生轮次的依赖接缝，尚不提供整个服务的存储后端替换功能。
  原始维护 API 仍指向构造时的旧图；完整替换须同时迁移这些调用方，不能据此宣称生产可任意换后端。
- SSE 的 model/gate 等事件名保留现有产品协议，独立于适配器内部的存储节点选择。

memory、checkpoint_store、turns 未修改；请求控制、预算、服务入口、业务工具、授权规则、
确认签名和幂等账均未修改。后续旧图退役再移除这些兼容依赖，本次不扩充抽象范围。

## 复验

新增行为测试使用真实 InMemorySaver 重建图与运行时、MockTransport 和 SQLite 执行账，
验证接续、待确认恢复/保留/换卡、身份与角色隔离、删除后迟到保存、停止不覆盖历史，
以及按钮取消不破坏已完成回执和执行账。独立假存储验证原生轮次不访问旧图状态 API。
这些结果不是 PostgreSQL 或生产验收。

本机只读博客 checkout 固定为 `.github/host-contract.json` 指定的 `838dbee`，
用于完整离线套件的跨仓源码检查；不连接博客服务或生产数据库。
实际完整回归与现有静态门禁结果在本 PR 验证记录中给出。

2026-10-24 本分支实际结果：新增 11 项行为测试与 75 项相关既有回归通过；
最终完整离线套件 1125 passed、2 skipped（166.01 秒），启用固定博客 checkout 的强制跨仓检查。
F821、类型、lint、盲捕获、安全、词表、模块/复杂度及文档/ADR 索引门禁通过，未调整静态基线。
未连接真实 PostgreSQL、模型 API 或生产服务；未部署、重启或执行数据库迁移。
