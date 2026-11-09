# 架构决策记录

| 决策 | 状态 |
| --- | --- |
| [ADR-0038：通用 Agent 长期架构与正式版优先级](adr-0038-general-agent-and-release-priorities.md) | 已采纳范围取舍；通用任务协调暂不实施 |

当前实现与契约见[架构](../agent-architecture.md)、[会话与记忆](../conversation-memory.md)、
[身份与写入](../identity-and-permissions.md)、[RAG](../rag-design.md)和[后台计划](../scheduling.md)。

旧 ADR 目录在文档整理提交 `b4e18338` 中移除，可从
[整理前的固定版本](https://github.com/BigLeopardCat/saudade-blog-agent/tree/538b84c56820979d8dbb49dcbf7ff0938a396c93/docs/adr)
查阅。历史决策记录当时的取舍，不能直接当作当前接口或配置说明。
新决策沿用编号；涉及取代或延期时明确状态，并保留可追溯入口。
