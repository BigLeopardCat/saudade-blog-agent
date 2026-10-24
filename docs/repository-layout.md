# 项目结构与依赖边界

## 当前结构

服务采用 Python flat layout，入口为 `agent/main.py`，启动命令为
`uvicorn agent.main:app`。`create_app()` 装配同一套独立路由，资源仅在 lifespan 中创建。
根目录 `server.py` 只导出同一个 app，使既有 `server:app` 部署继续运行。

| 位置 | 职责 |
| --- | --- |
| `agent/main.py` | 应用创建、版本和路由装配 |
| `server.py` | 同应用的 ASGI 导出，无运行逻辑 |
| `agent/api/lifecycle.py` | 创建与退出运行时、准入和工作线程资源 |
| `agent/api/dependencies.py` | 由请求解析应用资源，管理聊天准入 |
| `agent/api/middleware.py` | 请求大小与追踪上下文 |
| `agent/api/turns.py` | 显式接收运行时，适配原生结果与队列 |
| `agent/api/authentication.py` | 宿主断言验签、身份解析及管理员端点权限 |
| `agent/api/routes/` | 聊天、会话维护与确认取消、运维、审核、知识图谱接口 |
| `agent/api/events.py` | 具名 SSE 事件、正文编码和响应头 |
| `agent/api/schemas.py` | 聊天请求、响应和字段限额，与宿主 HTTP 契约对应 |
| `agent/api/context.py` | HTTP 请求到标准消息的适配，处理宿主历史回放和图片 |
| `agent/api/host_context.py` | 有界的时间、额度、历史动作和开放任务数据投影 |
| `agent/runtime/` | 原生模型工具循环、请求预算、取消、会话存储和流式事件 |
| `agent/domains/` | 按业务归属组织读取、目标解析、确认准备和事务执行 |
| `agent/principal.py`、`agent/authz.py` | 身份与工具权限；旧自然语言判定仍待清理 |
| `agent/checkpoint.py`、`agent/idem.py`、`agent/idem_pg.py` | 会话与幂等基础设施 |
| `tools/` | 博客 API、设备等外部 I/O 适配；遗留反向依赖随领域收口移除 |
| `tools/notifications.py` | 最近通知窗口、未读筛选和单条内容；保留原始定位链接 |
| `models/`、`rag/`、`config/` | 模型客户端、检索与配置 |
| `tests/` | 不依赖真实模型的行为回归和跨仓契约检查 |
| `eval/` | 真实请求评测、成本统计及质量门禁 |
| `frontend/`、`deploy/`、`scripts/`、`docs/` | 前端、部署资源、运维命令和设计文档 |

## 依赖规则

- API 层适配传输和调用应用服务；运行时、领域和工具不导入 API 层或 `server`。
- 运行时处理模型循环与生命周期，业务目标、确认范围和参数校验留在领域内。
- 工具适配外部服务。共享业务值归属对应领域，通用 I/O 原语归属适配层，避免工具反向导入编排模块。
- `schemas.py` 只描述 HTTP 契约，不堆入运行时状态、数据库模型或模型规划 IR。
- 包初始化文件不注册领域、不创建客户端、不隐式加载应用。
- 页面和宿主摘要、历史记录都是数据；身份、权限和确认凭据由独立的可信通道提供。

## 按需推进的优先级

1. 优先处理能复现的任务失败、权限与生命周期问题，固定真实输入输出契约。
2. 在同一批修改中把实现归回对应模块，删除原入口中的逻辑；保持一个注册入口和一个实现。
3. 独立、稳定的 HTTP 路由可单独提取，最后再收敛薄的应用装配入口。
4. 全仓包路径和 `src/` 调整单独实施，连同安装、导入及部署检查完成。

通知读取已按这一顺序收敛。通知 API 提供最近100条窗口，模型默认只接收5条，
支持窗口内分页、未读筛选和单条读取；窗口完整性与全局未读数分开表达。
读取不标记已读，业务变更继续走 `agent/domains/notifications/` 的签名确认事务。

## 入口与路由边界

应用工厂只装配标准 `APIRouter` 与中间件。路由通过 `Request.app.state` 获取本应用的
运行时、工作线程和聊天准入信号量，不导入 `agent.main` 或 `server`，不借用模块全局运行时。
后台调用显式接收运行时对象。确认取消与会话维护共用普通路由，不再由入口传入回调装配。

```text
agent/
├── main.py                 # 应用工厂与 ASGI 导出
├── api/
│   ├── lifecycle.py        # 启动与退出
│   ├── dependencies.py     # 请求资源与准入
│   ├── middleware.py       # HTTP 边界
│   ├── turns.py            # 原生调用与队列适配
│   ├── schemas.py          # HTTP 模型和字段限额
│   ├── context.py          # 请求到消息的适配
│   ├── host_context.py     # 当前请求的宿主数据投影
│   └── routes/             # 按接口领域划分的 APIRouter
├── runtime/                # 单一原生模型工具循环
└── domains/                # 业务能力与确认事务
```

当前先完成职责拆分和依赖收敛。若后续采用 `src/` 与单一发行命名空间，单独迁移包路径、
安装配置和部署入口，并验证离开仓库目录后的安装运行，避免目录调整掩盖行为变化。

结构选择参考 [PyPA 的 flat/src layout 说明](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)
与 [FastAPI 多文件应用文档](https://fastapi.tiangolo.com/tutorial/bigger-applications/)。

## 当前服务资源生命周期

`agent.api.lifecycle.lifespan` 创建本次启动的聊天准入信号量与 `RequestWorkers`，在
`app.state.workers` 持有资源。模块导入不创建聊天 executor；重复启动创建新资源。

关闭顺序固定为：停止准入 → 请求协作取消、取消排队工作 → 异步等待实际线程退出和
追踪/许可收尾 → 清空运行时引用 → 关闭 checkpoint 连接池。关闭调用被取消时仍先完成
工作线程清理，再传播取消。同步上游请求依靠已有 SDK 超时与请求截止时间结束，不能杀线程。

聊天、摘要、确认取消、会话删除和丢弃操作使用同一套工作跟踪。会话丢弃直接调用
同步状态接口，在会话锁内完成更新与读回校验；不再在线程中创建临时事件循环。
启动预热也交给该 executor，已运行的预热会在退出前完成，尚未运行的可取消。
建表或 saver 构造失败立即关闭并清空连接池缓存，后续启动不会复用关闭的池。

图谱与审核路由由 FastAPI 管理同步请求线程。聊天运行时和请求资源已脱离入口全局变量。
每个生产 worker 仍运行一个应用；checkpoint 工厂复用的连接池目前仍为进程范围，
不承诺同一进程内同时启动多套持久化应用。对外 HTTP 路径与确认凭据契约未变。
