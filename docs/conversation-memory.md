# 会话记忆

Agent 的 LangGraph checkpoint 是执行上下文的唯一持久化来源。博客保存原始聊天记录，用于界面展示、显式历史检索和首次导入；不维护第二套实时摘要。工具授权、确认凭据和业务执行账不由摘要决定。

## 上下文生命周期

1. 新请求先验证身份，在会话锁和删除屏障内读取 checkpoint。
2. 仅首次初始化时导入来源匹配的宿主历史。空消息列表与从未初始化是不同状态；丢弃最后一轮不会触发重新导入。
3. 正常任务保留完整的已提交消息，并追加本轮输入。完整预算同时计算系统指令、消息、工具定义、工具参数和输出预留。
4. 已完成历史达到 `COMPACTION_TOKENS`（当前 5,400 估算 token），或当前完整请求空间不足时，在新任务第一次模型调用前使用 LangChain `SummarizationMiddleware` 压缩较早的完整轮次，保留近期完整问答及工具调用/结果配对。
5. 摘要与被覆盖消息的替换结果随本轮结果原子提交。暂停后从子图恢复已完成的压缩，不重复生成摘要；确认和浏览器回执接续不重新压缩本任务。

摘要调用共享请求模型次数、token 总额和绝对截止时间，不启用独立重试。空白、拒绝、截断、未完成工具调用、超长输出或预算不足都保留原文。失败不登记虚假覆盖范围；必要时按完整请求预算缩小本次视图，记录 `memory.emergency_trim`，不删除 canonical 消息。

5,400 是压缩触发值，不是数据库硬删除上限。`FALLBACK_HISTORY_TOKENS`（当前 1,800）只用于首次导入和无法完整呈现的单轮长文本节选。超过运行时上下文安全上限的独立长轮次保存显式节选，原始正文仍在宿主，不能把节选标为已总结全文。

## 来源隔离

宿主提供的 `history_scope`、`summary_scope` 必须与本轮验签用户、角色、会话全部匹配。来源在记录产生时保存，读取时不能补造。来源未知的旧宿主记录可在本人历史界面查看，但不自动导入。

```json
{"history_scope":{"user_id":7,"role":"user","conversation_id":42}}
```

运行时产生的消息和摘要携带 `memory_actor`。角色或账号变化后，其他来源的派生上下文不再自动注入；工具消息还要重新核验当前读取权限。摘要仅保留目标、澄清、偏好和未完成事项，区分助手陈述与工具事实，不保留身份权限或授权凭据。

旧宿主的 `chat_summary` 只作为带来源的归档资料，在首次初始化时导入一次。它不再接受新摘要写回，也不驱动聊天原文删除。此表可能包含已删除原文的唯一摘要，因此不会为清理旧代码而删除用户资料。

## 长文与图片

历史工具 `get_chat_history` 先按消息 ID 定位；列表每条最多 300 字，指定 `message_id` 后每片最多 2,000 个 Unicode 字符，可用 `content_offset` 继续读取。`nextContentOffset=null` 表示到末尾；节选不能冒充已读全文。显式历史检索遵循宿主读取授权。

当前图片采用标准多模态内容块，文字问题在图片之前。完成轮次只保留文字、图片哈希引用和真实答复，不回灌 base64，也不额外生成图片描述。需要重读时由模型选择 `get_conversation_image`；固定宿主、会话归属、引用哈希、游标和下载上限共同约束访问。图片工具回执和临时多模态附件不会进入长期正文或 trace。

## 数据与部署

宿主原文保留不再依赖模型摘要成功；用户删除仍清理相应记录。checkpoint 的存储保留策略独立于提示词压缩，不允许通过压缩恢复已删除会话。

本次接口调整先发布 Agent，再发布博客。两仓共同删除 `summary_batch`、`new_summary`、`summary_coverage` 和私有 summary SSE；不维护旧摘要写回协议。公开宿主契约固定在 `.github/host-contract.json`。

## 验证

- `test_framework_compaction.py`：真实标准 Agent 与 saver 的压缩、原子提交、失败保留、来源隔离及取消恢复。
- `test_cache_window.py`：90 轮短对话，检查只在实际压缩时改变早期消息前缀。
- `test_history_recovery.py`、`test_memory_boundaries.py`：长文补读、图片预算、删除屏障及跨来源读取。
- 博客 `chat_context_tests.rs`：只读导入、来源隔离、删除与归档失效的真实 PostgreSQL 事务。

离线桩验证数据链路和边界，不代替真实模型任务验收。线上评测必须覆盖跨压缩事实回忆，并同时记录摘要 token、首个可见事件、首个正文和端到端延迟。

框架接口参见 [LangChain short-term memory](https://docs.langchain.com/oss/python/langchain/short-term-memory) 与 [SummarizationMiddleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in#summarization)。
