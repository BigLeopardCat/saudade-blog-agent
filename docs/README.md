# 文档导航

本目录描述当前源码契约；使用发行版时，在对应 tag 下阅读同版本文档。开发分支、发布包、运行进程和组件锁可能处于不同提交，不能相互代替。

## 从哪里开始

| 目标 | 阅读顺序 |
| --- | --- |
| 使用助手 | [Wiki 首页](wiki/Home.md) → [聊天与阅读](wiki/Using-the-Agent.md) → [确认与恢复](wiki/Approvals-and-Recovery.md) |
| 本地运行 | [本地起步](getting-started.md) → [配置导航](configuration.md) |
| 理解与修改代码 | [架构](agent-architecture.md) → [代码导读](code-reading.md) → [领域扩展](product-extensions.md) |
| 部署与排障 | [部署](deployment.md) → [排障](troubleshooting.md) → [评测与观测](eval-observability.md) |
| 准备发行 | [版本与附件核对](deployment.md#正式发行) → [候选回归与证据](release-acceptance.md#发布步骤) |

## 设计与契约

| 文档 | 覆盖范围 |
| --- | --- |
| [架构](agent-architecture.md) | 组件职责、依赖方向、状态及并发边界 |
| [代码导读](code-reading.md) | 沿读取、确认写入、恢复三条路径定位代码与测试 |
| [会话与记忆](conversation-memory.md) | checkpoint、上下文、摘要、缓存与任务预算 |
| [身份与写入](identity-and-permissions.md) | 逐次授权、签名确认、执行账与业务幂等 |
| [聊天协议](chat-stream-protocol.md) | SSE、客户端回报、在途输入和完成边界 |
| [RAG](rag-design.md) | 检索候选、正文证据、全文总结与覆盖范围 |
| [Embedding 输入](embedding-inputs.md) | 可选模型、完整输入预算和缓存 |
| [后台计划](scheduling.md) | 支持范围、DBOS、有限委托与 Worker |
| [领域扩展](product-extensions.md) | 工具目录、scope、执行器及接线测试 |
| [架构决策](adr/README.md) | 已采纳取舍与未来设计，状态分别标明 |

## 开发与运维

| 文档 | 覆盖范围 |
| --- | --- |
| [本地起步](getting-started.md) | 离线检查、开发宿主及启动核对 |
| [配置导航](configuration.md) | 配置来源、参数组和启用条件 |
| [部署](deployment.md) | 首次安装、升级、数据库、备份和回滚 |
| [排障](troubleshooting.md) | 就绪、版本、trace、授权和未知结果 |
| [评测与观测](eval-observability.md) | 分层验证、真实模型、trace 与指标 |
| [发布验收](release-acceptance.md) | 质量、安全、成本及证据要求 |
| [离线改进](offline-improvement.md) | 反馈复核、独立验收集和候选比较 |
| [教程截图](screenshots.md) | 图片来源、步骤对应、重拍与 Wiki 发布 |

## 文档归属

[README](../README.md)负责定位和起步；[CONTRIBUTING](../CONTRIBUTING.md)负责协作与测试规范；
[组件文档](../frontend/README.md)和[交互契约](../frontend/CHAT-UI.md)负责前端；
[评测资料](../eval/release-status.md)负责固定实验与发布证据；[路线图](../ROADMAP.md)只描述未来工作。

[Wiki 源稿](wiki/Home.md)按用户任务组织，与代码一起评审，再同步到 GitHub Wiki。
教程引用契约，不复制配置默认值和验收数字。接口、部署和兼容要求仍在仓库维护。
维护与发布方式见 [CONTRIBUTING](../CONTRIBUTING.md#文档)。
