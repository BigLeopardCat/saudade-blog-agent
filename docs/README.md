# 文档索引

这里是本仓手写文档的**唯一入口**。根 [README.md](../README.md) 只讲"这是什么、怎么跑"，
细节见下表——按"你现在想知道什么"挑一份，不要按文件名猜。

## 目录

| 文档 | 一句话 | 类型 |
|---|---|---|
| [confirmation-groups.md](confirmation-groups.md) | 按事务分组确认、批量勾选与部分执行语义 | 现状型 |
| [p4-completion-report-20261103.md](p4-completion-report-20261103.md) | 标准框架与成熟库收敛、145场景冻结回归、语义分歧和成本缓存指标 | 记录型 |
| [agent-architecture.md](agent-architecture.md) | 系统现在是什么：节点拓扑、跨语言契约、目录与机制 | 现状型 |
| [产品与领域装配](product-extensions.md) | 博客产品边界、标准 middleware 与非博客库存示例 | 现状型 |
| [backend-capability-boundaries.md](backend-capability-boundaries.md) | 后端业务事实、Agent适配校验与计算职责边界 | 现状型（含后续收敛原则） |
| [deployment.md](deployment.md) | 固定提交版本目录、独立环境、探活与失败回滚 | 现状型 |
| [conversation-memory.md](conversation-memory.md) | 来源隔离、长文本接续、图片引用与按需回看，及框架摘要生命周期 | 现状型 |
| [chat-stream-protocol.md](chat-stream-protocol.md) | 具名 SSE 事件、正文隔离、持久化边界与发布衔接 | 现状型 |
| [release-acceptance.md](release-acceptance.md) | 当前发布的质量、安全、分任务成本和时延目标，以及冻结证据的机器验收入口 | 现状型（目标未自动视为达标） |
| [offline-improvement.md](offline-improvement.md) | 反馈复核、数据隔离、候选提案与一次独立验收的发布衔接 | 现状型 |
| [native-account-actions.md](native-account-actions.md) | 原生账号管理接口、权限语义、迁移与隔离验证 | 现状型（已生产验收） |
| [repository-layout.md](repository-layout.md) | 目录职责、依赖边界与按领域迁移的归属规则 | 现状型（含迁移方向） |
| [native-progress-events.md](native-progress-events.md) | 原生进度事件、答案流、取消围栏与服务端输出计量 | 现状型 |
| [native-event-transport.md](native-event-transport.md) | Runtime 原生事件到聊天/SSE 协议的薄传输边界与终态规则 | 现状型 |
| [native-context-cache.md](native-context-cache.md) | 原生工具目录、动态上下文顺序及缓存用量的验证口径 | 现状型 |
| [eval-observability.md](eval-observability.md) | 原生运行时指标、结果验收、旧评测边界与执行入口 | 现状型 |
| [rag-design.md](rag-design.md) | 检索设计与实现总结，§8 是触发条件驱动的升级预案 | 现状型 |
| [rag-library-migration.md](rag-library-migration.md) | 成熟检索库、标准Markdown解析、空间迁移与质量/资源对照 | 现状型（含专项验证） |
| [rag-production-debug.md](rag-production-debug.md) | 两机配置、缓存与引擎候选、增量建库、回退和生产验收记录 | 现状型（§6–§8 为待验收方案） |
| [embedding-inputs.md](embedding-inputs.md) | 可选 tokenizer 的完整输入预算、空间签名与验证方式 | 现状型（末节含验证记录） |
| [identity-and-permissions.md](identity-and-permissions.md) | 身份与权限落地记录及跨仓引用锚点；原生边界见当前架构说明 | 记录型 |
| [platform-roadmap.md](platform-roadmap.md) | 当前整改结果、后续维护事项及触发条件 | 方向型（含当前状态） |

| [scheduling.md](scheduling.md) | 管理员持久定时、只读后台分析、执行记录与部署 | 当前规范 |

> 这份表**刻意不写"最后修订"日期**：手抄的日期没有判据看着（CI 是浅克隆，读不到逐文件的
> 提交日期），注定漂。要看某份文档最近改了什么：`git log -1 --format=%ad -- docs/<文件>`。

架构决策记录另有索引：[adr/README.md](adr/README.md)（记录取舍与实施边界，长期执行内核方向见 ADR-0024）。

仓根还有几份不属于 `docs/`： [README.md](../README.md)（是什么、怎么跑）、
[CONTRIBUTING.md](../CONTRIBUTING.md)（判据规矩、能/不能在本机跑什么）、
[ROADMAP.md](../ROADMAP.md)（现在做什么 / 等触发条件 / 明确不做）、
[SECURITY.md](../SECURITY.md)（安全问题走私密通道）、[CHANGELOG.md](../CHANGELOG.md)（对外行为与判据的变更；
直接记录近期对外变更）。
