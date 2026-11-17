# 本地起步

Agent 仓提供 Python 服务和聊天组件；账号、业务 API 与博客页面来自独立的
[Saudade-Blog](https://github.com/BigLeopardCat/Saudade-Blog)。单独启动 Python 不会得到完整博客。
生产安装与升级走[部署](deployment.md)，本页用于独立开发环境。

## 离线检查

需要 Git、Python 3.12+ 和 uv。默认回归不需要模型密钥、博客或数据库：

```sh
git clone https://github.com/BigLeopardCat/saudade-blog-agent.git
cd saudade-blog-agent
uv sync --locked
uv run --frozen pytest -q tests/test_docs_links.py tests/runtime/test_application_boundaries.py
```

默认 pytest 忽略部署 `.env` 并禁止外网。完整套件、可选依赖与前端检查见
[CONTRIBUTING](../CONTRIBUTING.md)。共享生产主机只运行定向检查，完整矩阵交给 CI。

## 连接开发宿主

```sh
cp .env.example .env
```

编辑自己的 `.env`，完整参数导航见[配置](configuration.md)：

1. 选择 `LLM_PROVIDER` 并填写对应前缀的密钥、端点和模型；端点须匹配密钥套餐。
2. 设置本站 `BLOG_API_BASE` 和 Agent 可访问的 `AGENT_ADMIN_BASE`。容器私网覆盖使用 `BLOG_INTERNAL_API_BASE`，公开来源仍保留给导航。
3. Rust 与 Agent 配置相同 `AGENT_ASSERTION_SECRET`；Agent 另配独立 `AGENT_CONFIRM_SECRET`。不复制宿主登录或工具委托签名密钥。
4. 持久会话、确认及暂停恢复需要专用 Agent PostgreSQL 的 `CHECKPOINT_DSN`，不能使用博客业务库。数据库权限见[部署](deployment.md#2-建立独立-agent-数据库)。
5. 核对实际模型窗口与任务预算；embedding、设备和调度先保持关闭，再按需接入。

宿主签发短期身份并代理聊天，浏览器不自行签名。开发、测试与生产使用各自数据库和凭据。

## 启动与验收

```sh
uv run --frozen uvicorn agent.main:app --host 127.0.0.1 --port 8010
```

另开终端核对：

```sh
curl --fail http://127.0.0.1:8010/health
curl --fail http://127.0.0.1:8010/ready
curl --fail http://127.0.0.1:8010/version
```

`health` 表示进程可响应；`ready` 检查运行时与必要凭据，失败返回 503 和原因；
`version` 标识加载的构建。它们不调用模型，也不验证全部外部连通性或业务事务。
没有 checkpoint 时，不能仅凭 ready 推断暂停恢复与确认能力完整。

通过开发博客登录后，先验证一条只读请求的真实来源，再测试确认和取消。
遇到问题按[排障](troubleshooting.md)检查，不绕过签名测试宿主接入。
源码阅读见[代码导读](code-reading.md)，修改聊天界面见[组件开发](../frontend/README.md)。
