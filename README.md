# Saudade Blog Agent

面向博客的工具助手「泠月喵」：查找和解读文章、页面操作、管理员协作编辑，以及经过人工确认的业务写入。使用 FastAPI、LangChain `create_agent`、LangGraph 和 PostgreSQL；后台计划使用 DBOS。

项目当前为 **0.2 Beta**。发布目标与已取得的成绩分别管理，见[发布验收](docs/release-acceptance.md)。

## 能力

- 模型自主选择工具，按需加载领域工具；业务后端负责权限、数据及事务。
- 支持流式回复、任务进度、执行中补充要求、暂停和 checkpoint 接续。
- 写入前展示确认卡；绑定具体对象和版本，执行账记录结果，未知结果不自动重做。
- 会话记忆按容量压缩；身份、授权和确认凭据不由摘要决定。
- 可选混合检索、本地 embedding、管理员定时提醒和只读分析。
- 管理员可协作修改文章、选择图库封面、准备 AI 生图；付费生图及结果入库分别确认。

## 开发起步

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。默认测试无需模型密钥、博客或数据库。

```sh
git clone https://github.com/BigLeopardCat/saudade-blog-agent.git
cd saudade-blog-agent
uv sync --locked
uv run --frozen pytest -q
cp .env.example .env
```

运行服务前填写模型密钥、与博客一致的 `JWT_SECRET` 和部署地址。
`BLOG_API_BASE` 同时决定公开工具地址和页面导航的站点来源，部署时必须设为自己的站点。
需要可恢复任务和确认操作时配置独立 PostgreSQL 数据库 `CHECKPOINT_DSN`。
`AGENT_CONTEXT_TOKENS` 应匹配实际模型端点窗口，工作预算及压缩设置见会话文档。

```sh
uv run --frozen uvicorn agent.main:app --host 127.0.0.1 --port 8010
```

`/ready` 检查依赖就绪，`/version` 返回版本信息。聊天入口由[博客宿主](https://github.com/BigLeopardCat/Saudade-Blog)鉴权代理，不应直接暴露内部 Agent 接口。

## 文档

[文档索引](docs/README.md) · [架构](docs/agent-architecture.md) · [配置与部署](docs/deployment.md) · [会话与缓存](docs/conversation-memory.md) · [领域扩展](docs/product-extensions.md) · [评测](docs/eval-observability.md)

使用步骤见[博客使用指南](https://github.com/BigLeopardCat/Saudade-Blog/blob/cn_sora_blog/docs/user-guide.md)。版本相关能力和运维说明保存在仓库；Wiki 适合放更长的操作教程，引用这些文档即可，不另维护一套配置表。

## 参与与许可

提交规范及验证入口见 [CONTRIBUTING.md](CONTRIBUTING.md)，计划见 [ROADMAP.md](ROADMAP.md)，安全问题见 [SECURITY.md](SECURITY.md)。

代码按 [MIT](LICENSE) 发布。角色模型、贴图及美术资源使用单独的 [CC BY-NC-SA 4.0 许可](frontend/ASSETS-LICENSE.md)；第三方依赖见 [frontend/THIRD-PARTY.md](frontend/THIRD-PARTY.md)。
