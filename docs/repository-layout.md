# 项目结构与依赖边界

## 当前结构

服务采用 Python flat layout。`server.py` 是现有 ASGI 入口，启动命令为
`uvicorn server:app`；入口文件的名字不决定模块是否合理。

| 位置 | 职责 |
| --- | --- |
| `server.py` | 应用生命周期、HTTP 路由、中间件、SSE 和运行资源接线；继续按边界拆分 |
| `agent/api/schemas.py` | 聊天请求、响应和字段限额，与宿主 HTTP 契约对应 |
| `agent/api/context.py` | HTTP 请求到标准消息的适配，处理宿主历史回放和图片 |
| `agent/api/host_context.py` | 有界的时间、额度、历史动作和开放任务数据投影 |
| `agent/runtime/` | 原生模型工具循环、请求预算、取消、会话存储和流式事件 |
| `agent/domains/` | 按业务归属组织读取、目标解析、确认准备和事务执行 |
| `agent/principal.py`、`agent/authz.py` | 身份与工具权限；旧自然语言判定仍待清理 |
| `agent/checkpoint.py`、`agent/idem.py`、`agent/idem_pg.py` | 会话与幂等基础设施 |
| `tools/` | 博客 API、设备等外部 I/O 适配；遗留反向依赖随领域收口移除 |
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

## 拆分顺序与目标

1. 先修复和固定请求上下文契约，提取请求模型与适配，保持 HTTP 和宿主契约稳定。
2. 提取独立的运维、检索管理和审核路由，再提取聊天路由及其运行资源依赖。
   使用 FastAPI `APIRouter`、生命周期和依赖注入；不增加自制路由或服务定位框架。
3. 当路由不再依赖入口模块的全局变量时，建立薄的 `agent/main.py` 应用装配入口（待建）。
   同一批更新部署命令、开发说明和安装后启动验证，再移除旧入口。
4. 清理不可达语义匹配与旧适配，按领域拆分共享大模块；每批删除原实现，不长期保留两份逻辑。

目标归属如下，标注“待建”的文件尚未接入：

```text
agent/
├── main.py                 # 待建：应用创建、生命周期和路由装配
├── api/
│   ├── schemas.py          # HTTP 模型和字段限额
│   ├── context.py          # 请求到消息的适配
│   ├── host_context.py     # 当前请求的宿主数据投影
│   └── routes/             # 待建：按接口领域拆分的 APIRouter
├── runtime/                # 单一原生模型工具循环
└── domains/                # 业务能力与确认事务
tests/                      # 确定性回归
eval/                       # 请求级真实模型评测与工程门禁
```

当前先完成职责拆分和依赖收敛。若后续采用 `src/` 与单一发行命名空间，单独迁移包路径、
安装配置和部署入口，并验证离开仓库目录后的安装运行，避免目录调整掩盖行为变化。

结构选择参考 [PyPA 的 flat/src layout 说明](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)
与 [FastAPI 多文件应用文档](https://fastapi.tiangolo.com/tutorial/bigger-applications/)。
