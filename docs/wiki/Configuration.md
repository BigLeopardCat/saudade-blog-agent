# Agent 从零接入与配置

Agent 没有独立的网页配置面板：模型、数据库与预算通过环境文件设置，博客后台负责业务设置。
新手优先走[博客安装教程](https://github.com/BigLeopardCat/Saudade-Blog/wiki/Installing-and-Upgrading)，
首次安装选择 with-agent，需要计划任务再选择 with-scheduler。

## 1. 找到应该编辑的文件

| 安装方式 | 配置位置 | 生效方式 |
| --- | --- | --- |
| 博客 Compose 安装 | 私有安装目录的 agent.env，默认 ~/.local/share/saudade/compose/agent.env | 保留现有安装目录，按容器流程重新创建对应服务 |
| 原生 systemd | /etc/saudade/agent-runtime.env | 由服务 EnvironmentFile 注入，按发布流程重启 |
| 独立开发 | Agent 源码根目录自己的 .env，从 .env.example 复制 | 从该目录启动开发进程 |

不要改了开发 .env 却期待生产服务变化，也不要把生产配置软链接回开发目录。
安装器已生成的签名和数据库密码继续保留，不因换模型重新随机生成所有密钥。

## 2. 选择一个模型供应商

`LLM_PROVIDER` 选择一个值，只填写对应前缀的配置。密钥、端点和模型必须属于同一个实际账户/套餐。

| 选择值 | 对应字段 |
| --- | --- |
| deepseek | DEEPSEEK_API_KEY、DEEPSEEK_BASE_URL、DEEPSEEK_MODEL |
| qwen | QWEN_API_KEY、QWEN_BASE_URL、QWEN_MODEL |
| openai | OPENAI_API_KEY、OPENAI_BASE_URL、OPENAI_MODEL |
| opencode | OPENCODE_API_KEY、OPENCODE_BASE_URL、OPENCODE_MODEL |

例如沿用仓库开发模板的 DeepSeek 示例，编辑为自己的真实值：

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=替换为自己的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
AGENT_REASONING_EFFORT=low
```

这里的模型名来自版本化模板，不保证你的账户已开通；先核对供应商实际可用列表。
使用 OpenCode 套餐不要把其密钥填进 DeepSeek 直连端点。端点不支持推理档位时用 none，重新验收行为。
不要保留模板密钥，也不要把密钥放在 VITE_ 变量、截图或聊天中。

## 3. 地址和身份：最容易填错的部分

| 字段 | 作用 | 填写原则 |
| --- | --- | --- |
| BLOG_API_BASE | 公开 API 和浏览器导航来源 | 填自己的公开博客来源，包含 /api/public |
| BLOG_INTERNAL_API_BASE | 可选的容器内公开 API 传输覆盖 | Compose 可用 http://backend:3000/api/public；浏览器仍走公开来源 |
| AGENT_ADMIN_BASE | Agent 调用宿主受保护业务 API 的服务地址 | Compose 可用 http://backend:3000，原生用实际内部地址 |
| AGENT_ASSERTION_SECRET | 校验 Rust 宿主身份断言 | 与 Rust 同名配置完全一致，不是模型 API 密钥 |
| AGENT_CONFIRM_SECRET | 签署人工确认卡 | Agent 独立持有，不与前一个密钥复用 |

原生同机开发常用宿主 loopback 地址；容器里的 127.0.0.1 指本容器，不指另一个服务。
Agent 不持有 JWT_SECRET、BLOG_DELEGATION_SECRET 或设备签名密钥。
宿主通过有限工具委托让 Agent 读取和操作业务，不能靠复制登录签名来“修复 403”。

## 4. 持久状态和后台计划

`CHECKPOINT_DSN` 指向**专用 Agent PostgreSQL 数据库**，保存会话、预算、确认和恢复状态。
不要填博客业务库或测试库；`CHECKPOINT_FORBID_DB` 可填写实际博客库名作额外防错。
DSN 的用户名、密码、主机和库名必须对应真实安装，密码中的保留字符需按连接串规则编码。

Compose 安装器准备独立 Agent 库，不需要再导入博客业务 SQL。
独立安装的建库步骤见[生产部署](https://github.com/BigLeopardCat/saudade-blog-agent/blob/main/docs/deployment.md)。
未配置持久状态时，不要认为暂停、确认和恢复已经完整可用。

启用计划需 SCHEDULING_ENABLED=true、独立 Worker 和有限委托。SCHEDULING_DSN 空时按实现沿用 checkpoint 配置，
但 HTTP 进程不会因此自动变成 Worker。配置文件与 Worker 启动必须同时到位。

## 5. 预算和可选功能

| 想调整 | 看哪些字段 | 判断方式 |
| --- | --- | --- |
| 单次上下文 | AGENT_CONTEXT_TOKENS、AGENT_INPUT_TOKENS | 不超过端点实际窗口，包含工具与输出预留 |
| 完整任务用量和时间 | AGENT_TASK_TOKENS、AGENT_TASK_SECONDS | 接续沿用累计量，不把它当每轮额度 |
| 模型和工具次数 | AGENT_MODEL_CALLS、AGENT_TOOL_CALLS | 上限不等于目标，不为省调用截断证据 |
| 输出长度 | AGENT_OUTPUT_TOKENS、AGENT_EXTENDED_OUTPUT_TOKENS | 实际输出还受剩余上下文和任务预算限制 |
| 工具与历史 | AGENT_TOOL_SCHEMA_TOKENS、AGENT_COMPACTION_TRIGGER/TARGET | 工作集软目标与总输入上限不同 |
| 并发 | AGENT_MAX_CONCURRENT | 每进程，不代表已证明支持相同数量的用户 |
| trace | TRACE_DIR | 私有状态目录，可含用户内容，不能当公开图片目录 |

先保持 embedding、混合向量检索和设备关闭，验证基本聊天后再按
[配置导航](https://github.com/BigLeopardCat/saudade-blog-agent/blob/main/docs/configuration.md)启用。
不开向量服务仍有词法检索；RAG 只索引公开文章，不能用管理员凭据扩充私有语料。

## 6. 启动和验证

开发环境按[本地起步](https://github.com/BigLeopardCat/saudade-blog-agent/blob/main/docs/getting-started.md)
安装锁定依赖，再运行：

尚无 uv 时先按[官方安装步骤](https://docs.astral.sh/uv/getting-started/installation/)安装，
重新打开终端核对 `uv --version`。生产容器安装不要求你在宿主另建开发环境。

```sh
uv run --frozen uvicorn agent.main:app --host 127.0.0.1 --port 8010
curl --fail http://127.0.0.1:8010/health
curl --fail http://127.0.0.1:8010/ready
curl --fail http://127.0.0.1:8010/version
```

启动与 curl 在不同终端；生产使用服务/容器流程，不运行开发命令替换现有发布。
ready 检查必要配置而不调用模型，不证明全部业务与外部接口已通过。

最后在博客依次验证：公开文章查询 → 给出真实来源 → 管理员准备一张卡 → 取消 → 暂停与继续 → 可选提醒。
先用测试数据，核对真实结果；403、无正文和未知写入的处理见
[排障](https://github.com/BigLeopardCat/saudade-blog-agent/blob/main/docs/troubleshooting.md)。
