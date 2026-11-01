# 管理员后台计划

计划通过 Agent 的按需领域 `admin_automations` 管理：创建、暂停、恢复、取消均使用
现有确认卡。只允许管理员操作自己的计划；后台执行和通知前重新读取账号当前权限。

## 支持范围

- 一次性绝对时间、确认创建后的相对延时、带 IANA 时区的五段 cron。
- 个人提醒直接发送提供的正文，不调用模型；只读后台分析复用现有 LangChain Agent。
- 一次性任务可等待指定文章公开，必须提供截止时间，最多等待七天，默认每分钟查询。
- 只读分析使用独立 checkpoint，没有原聊天历史、页面操作、设备操作或业务写入。
  提示中必须提供完整目标。首版不迁移正在交互的任务，不自动批准未来业务写入。
- 结果通知发给创建者本人，超过通知长度的内容截取预览；完整结果在执行记录中读取。

模型只准备计划。确认后服务端保存定义，到期由独立 DBOS Worker 执行。
等待不持有 SSE、聊天锁或模型上下文。DBOS 负责 PostgreSQL 持久队列、定时及步骤恢复。
它与 LangGraph 的职责不同：后者仍保存和执行 Agent 对话状态。

## 运行约束

每名管理员最多 20 个尚未结束的计划（含暂停项）；周期最短五分钟。
后台分析全局并发 1，最多 90 秒、6 次模型调用、12 次工具调用、32K 累计 token，
单次输出预留 1K。提醒使用独立并发 2 的队列，不排在模型任务后面。
同一周期计划不重叠执行，不补跑停机期间的所有历史时刻。默认迟到宽限一小时。
条件等待不反复调用模型；截止或权限变化会阻止后续执行。

暂停阻止尚未开始的执行；已开始的读取允许完成。取消同时阻止后续结果通知，
不能回滚已完成的通知。由于外部服务无法提供跨服务 exactly-once，通知接口以
`(owner_id, run_id)` 及内容摘要去重；相同编号不同正文返回冲突。
未知结果不会伪装为成功。记录区分框架执行状态与业务 `completed/blocked/expired/skipped`。

终结执行记录保留 30 天；夜间清理通过 DBOS 公开 API 删除执行树，保留未完成及暂停任务，
不直接操作第三方内部表。可先运行 `python -m agent.scheduling.retention` 查看候选，
`--apply` 才删除。通知投递去重键与通知正文分离，账号删除时级联清理；不能单独清掉去重键
后重放未知投递。

## 部署

1. 先在博客仓应用 PostgreSQL 编号迁移 `0021_automation_results.sql`，
   发布 `/api/protected/automation-actor` 和 `/api/protected/automation-results`。
2. 配置 `SCHEDULING_ENABLED=true`。`SCHEDULING_DSN` 为空时使用 `CHECKPOINT_DSN`。
   DSN 必须指向独立的 Agent PostgreSQL 数据库，不能是博客业务库。
   DBOS 在 `saudade_automations` schema 建表；它使用公开的 JSON 序列化，禁止 pickle。
3. 按实际用户及 release-root 修改 `deploy/scheduling/saudade-scheduler.service` 的路径，
   安装并启动 Worker。HTTP 进程仅创建 DBOSClient，不启动第二个调度器。
   Worker 先启动完成，再开放调度入口。账号验证及通知请求使用现有服务端短期签名。
4. 发布命令设置 `DEPLOY_SCHEDULER=1`，脚本同时重启 HTTP 和 scheduler，失败一起回滚。Worker 关闭最多等待 95 秒，systemd 需预留
   120 秒；挂起的定时器由 DBOS 在启动后恢复。不要从代码热切换修改持久工作流步骤顺序。
   工作流名及版本是持久契约；后续不兼容变更应新增版本并排空旧执行。

默认关闭，因此未部署 Worker 的现有安装不受影响。可通过聊天管理；本版没有新增后台页面。
HTTP 内部管理 API 位于 `/automations`，需要 `X-Agent-Assertion` 管理员断言：
列表/详情、创建、`POST /automations/control`、分页 `/{id}/runs` 及单条完整结果。
返回 `Cache-Control: no-store`。外部界面接入应经博客鉴权代理，不向浏览器暴露签名密钥。

## 验证

普通 `pytest` 使用隔离配置，不连接服务器。真实 PostgreSQL 测试必须显式指定
`SAUDADE_SCHEDULING_TEST_DSN`，自行建立随机 schema，启动子进程 Worker，结束后删除。
CI 提供临时 PostgreSQL 16；不依赖本机路径、服务或生产密钥。
测试包括管理员隔离、确认不执行、执行账重放、真实暂停恢复、权限变化，以及杀 Worker
后已完成步骤不重做、通知重试不重复。博客侧的通知事务由 `TEST_PG_URL` 显式启用验证。
