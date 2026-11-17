# 配置导航

字段、类型、默认值和校验以 [Settings](../config/settings.py) 为准。
[开发模板](../.env.example)与[生产模板](../deploy/agent-runtime.env.example)用途不同，
模板选择的供应商也不一定等于代码默认值。本文维护参数职责与启用条件，不重复默认值表。

## 加载来源

开发由 pydantic-settings 读取进程环境和当前工作目录的 `.env`，进程环境优先。
`SAUDADE_IGNORE_ENV_FILE=1` 是忽略 `.env` 的进程级开关；普通测试已隔离部署配置。
生产由 systemd EnvironmentFile 或容器编排注入，不从开发 checkout 读取配置。
环境文件是数据，不用 `source` 执行，不把真实内容贴进 Issue、日志或 Wiki。

## 参数组

| 参数组 | 关键字段 | 条件与详细入口 |
| --- | --- | --- |
| 模型 | `LLM_PROVIDER`、对应前缀的 `*_API_KEY/MODEL/BASE_URL` | 只使用选定供应商，端点须匹配密钥；支持值见 Settings |
| 身份与确认 | `AGENT_ASSERTION_SECRET`、`AGENT_CONFIRM_SECRET` | 前者与宿主一致，后者只由 Agent 持有；[身份与写入](identity-and-permissions.md) |
| 业务地址 | `BLOG_API_BASE`、`BLOG_INTERNAL_API_BASE`、`AGENT_ADMIN_BASE` | 公开导航来源与私网传输分开 |
| 持久状态 | `CHECKPOINT_DSN`、`CHECKPOINT_FORBID_DB` | 专用 Agent PostgreSQL；[安装](deployment.md#2-建立独立-agent-数据库) |
| 输入与任务 | `AGENT_CONTEXT_TOKENS`、`AGENT_INPUT_TOKENS`、`AGENT_TASK_*`、`AGENT_MODEL_CALLS`、`AGENT_TOOL_CALLS` | 单次窗口和全任务累计量不同；[预算](conversation-memory.md#预算) |
| 输出与推理 | `AGENT_REASONING_EFFORT`、`AGENT_OUTPUT_TOKENS`、`AGENT_EXTENDED_OUTPUT_TOKENS` | 端点不支持档位时用 none；[生成预算](agent-architecture.md#生成预算) |
| 工具与压缩 | `AGENT_TOOL_SCHEMA_TOKENS`、`AGENT_COMPACTION_TRIGGER/TARGET` | schema 软目标不代替完整输入上限；[请求布局](conversation-memory.md#模型请求布局) |
| 并发与连接 | `AGENT_MAX_CONCURRENT`、`AGENT_MAX_REVIEW`、`AGENT_STREAM_IDLE_TIMEOUT/TOTAL_TIMEOUT` | 每进程限额；[流式协议](chat-stream-protocol.md#慢客户端与执行终态) |
| 检索 | `RAG_HYBRID_ENABLED`、`EMBEDDING_*`、`RAG_VECTOR_DSN/COLLECTION` | 向量可选；[RAG](rag-design.md)、[输入预算](embedding-inputs.md) |
| 后台计划 | `SCHEDULING_ENABLED`、`SCHEDULING_DSN`、`DELEGATION_DIR` | 独立 Worker 与有限委托；[调度](scheduling.md#部署) |
| 观测 | `LOG_LEVEL`、`TRACE_DIR` | 私有目录；[评测与观测](eval-observability.md) |

## 变更与核对

checkpoint 管会话与恢复，DBOS 管持久工作流，向量可以按源重建；三者不能按同一种缓存清理。
向量初始化见[向量存储](deployment.md#向量存储)。供应商、端点、预算、embedding 空间、
身份及宿主地址变化后，重新验证相关行为；不能只凭配置文件存在推断生效。

核对服务实际配置来源，重启后查 `/version` 与 `/health` 的非敏感诊断。
`/ready` 不执行付费模型请求，也不验证全部外部 API。按[分层验证](eval-observability.md#分层验证)
选择检查。生产账号、路径、备份与重启步骤只维护在[部署](deployment.md)。
