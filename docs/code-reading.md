# 代码导读

先读[架构](agent-architecture.md)，再沿一次真实请求追踪身份、状态、证据与结果。
这里只列关键入口，不逐文件解释整个目录；行为由当前代码与测试核对。

## 聊天主链

| 顺序 | 入口 | 要回答的问题 |
| --- | --- | --- |
| 应用建立 | [main](../agent/main.py)、[lifecycle](../agent/api/lifecycle.py)、[composition](../agent/composition.py) | 谁创建与关闭资源？导入模块会不会启动模型或 Worker？ |
| 请求进入 | [chat](../agent/api/routes/chat.py)、[authentication](../agent/api/authentication.py) | 身份如何验证，谁取得执行许可，断连后谁持有资源？ |
| 产品装配 | [BlogApplication](../agent/products/blog/application.py) | 人设、工具目录和产品 middleware 如何注入？ |
| 执行 | [AgentRuntime](../agent/runtime/service.py)、[FrameworkExecution](../agent/runtime/execution.py) | create_agent、审批父图、checkpoint 与提交如何配合？ |
| 交付 | [events](../agent/api/events.py)、[transport](../agent/transport.py) | 正文、进度、卡片、命令和回执分别交给谁？ |

运行时负责执行与状态，产品负责业务装配；依赖约束见
[边界测试](../tests/runtime/test_application_boundaries.py)。

## 发现工具与读取文章

从 [ToolCatalog](../agent/products/blog/capabilities.py)、[领域说明](../agent/products/blog/catalog.py)
看初始菜单与领域发现，再看[工具域状态](../agent/runtime/tool_state.py)的加载、顺序与工作集。
按需加载指模型可见 schema，不等于 Python 插件热加载，也不必经过独立的分类模型节点。

[BlogToolMiddleware](../agent/products/blog/tool_policy.py)与 [authz](../agent/authz.py)检查调用准入，
宿主继续核对真实业务权限。目录可见不能代替执行授权。

文章检索从 [search](../rag/search.py)产生候选，[articles/reading](../agent/domains/articles/reading.py)
提供读取接入，[articles/summary](../agent/domains/articles/summary.py)处理全文覆盖与汇总。
追踪 revision、章节与字符范围，区分相关片段、原文交付和派生摘要。
全文总结如何遍历完整源并选择汇总方式见 [RAG](rag-design.md)，覆盖通过不等于语义忠实。

## 公告确认写入

1. [preparation](../agent/domains/announcements/preparation.py)冻结对象、参数与版本，返回签名卡片，不写公告。
2. [transactions](../agent/domains/announcements/transactions.py)批准后核对绑定、权限及规格。
3. [writes](../agent/runtime/writes.py)计算稳定键，认领、登记派发并保存结果。
4. [idem_pg](../agent/idem_pg.py)以主键和条件更新处理竞争；[recovery](../agent/domains/announcements/recovery.py)按同一编号核对业务回执。
5. Rust 宿主原子提交公告、个人通知与业务回执，边界见[隔离与重试](identity-and-permissions.md#隔离与重试)。

调用工具、HTTP 送达、业务提交和本地回执保存是不同事实。
对照[公告测试](../tests/domains/test_announcements_native.py)、[派发测试](../tests/runtime/test_write_dispatch.py)
和[数据库故障注入](../tests/integration/test_confirmed_write_pg_failure.py)。
公告的安全恢复不能推导为所有工具都可以重复调用。

## 暂停与恢复

读 [task_inputs](../agent/runtime/task_inputs.py)、[task_pause](../agent/runtime/task_pause.py)、
[recovery](../agent/runtime/recovery.py)和[execution_state](../agent/runtime/execution_state.py)。
分别找出补充已接收、实际应用、请求暂停和持久暂停的依据。新 HTTP 请求不重置累计预算。

对照[会话](conversation-memory.md)、[协议](chat-stream-protocol.md)与
[取消测试](../tests/runtime/test_native_cancellation.py)。checkpoint 恢复图状态，不是任意指令位置继续，
也不会撤销已发生的外部效果。

最后按[trace 定位](troubleshooting.md#定位一次请求)验证自己的理解。在隔离测试中改变角色、
目标版本或连接结果，观察拒绝发生在哪个边界；不要为读代码在生产制造写入。
