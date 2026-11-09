# ADR-0035：管理员持久调度

- **状态**：DBOS 调度边界已采纳；执行范围包含后来加入的显式批准定时公告。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/538b84c56820979d8dbb49dcbf7ff0938a396c93/docs/adr/adr-0035-background-scheduling.md)。

## 背景与决策

后台计划需要投递后的持久恢复，只有定时触发器还需要自行补队列和恢复状态机。
复用 DBOS 的 PostgreSQL 持久队列、动态 cron、等待及执行历史；
Agent HTTP 只使用公开客户端，独立 Worker 推进调度。
LangChain/LangGraph 仍负责 Agent 执行，不增加另一套规划 DSL。

计划管理、执行和通知核对管理员归属及当前权限，定义通过现有签名确认创建。
等待不占 SSE、聊天锁和模型上下文，条件检查不调用模型轮询。
后台分析使用独立身份、checkpoint 和持久预算，不迁移正在交互的聊天任务。

## 后续承接与正确性边界

原记录首版只有提醒和只读分析；当前增加创建时批准完整内容的定时公告。
该专用路径冻结标题、正文和时间，执行前复核权限，宿主事务按执行编号去重；
不允许模型到期时自由生成并批准任意写入。其他写入范围以[后台计划](../scheduling.md)为准。

暂停、取消是协作式控制，不能撤回已经发出的外部请求。
网络结果未知不视为成功，也不盲目重做副作用。
不保存登录令牌或确认凭据到 DBOS 定义；身份委托使用独立私有存储，
见[身份与写入](../identity-and-permissions.md)。

调度数据位于独立 Agent PostgreSQL 数据库，使用公开 JSON 序列化，
不操作第三方内部表或引入新的消息代理。
回归见 `tests/scheduling/` 和 `tests/integration/test_scheduling_pg.py`；
杀 Worker 恢复须在可丢弃实例验证，不能连接生产库。
