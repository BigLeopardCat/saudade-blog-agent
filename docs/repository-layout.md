# 目录职责与迁移边界

本轮已将混合运行时和收藏领域收拢到独立 Python 包。其余旧实现仍随能力迁移逐步拆除。

| 位置 | 职责 |
| --- | --- |
| `server.py` | HTTP、身份入口、请求与 SSE 协议适配 |
| `agent/runtime/` | 请求执行循环、工具加载、会话生命周期与输出协议适配 |
| `agent/domains/favorites/`、`notifications/`、`messages/`、`todos/` | 各自目标、权限、确认范围与真实执行结果 |
| `agent/runtime/admin_domains.py`、`agent/domains/admin_writes.py` | 管理员领域组装及稳定会话准入，业务判断留在各领域 |
| `agent/domains/public_*.py`、`article_metadata.py` | 公共读取的分页边界、领域投影及共享文章元数据 |
| `agent/checkpoint.py`、`agent/idem*.py` | 既有持久化与幂等基础组件，供运行路径复用 |
| `tools/` | 站点 API 与设备等外部 I/O 适配，不负责模型规划 |
| `models/`、`rag/`、`config/` | 模型访问、检索和配置 |
| `tests/` | 确定性回归，完整目录收集 |
| `eval/` | 请求级质量与成本验收、运行诊断与报告读取 |
| `docs/` | 当前设计、迁移边界及保留的实验结论 |

## 已建立的边界

旧图兼容边界的完整契约见 [legacy-compatibility-boundary.md](legacy-compatibility-boundary.md)。
本文件只记录目录职责；迁移 PR 必须同时遵守那份依赖方向和退役条件。

- `agent/runtime/hybrid.py` 负责准入、持久化与协议适配；模型和工具循环在 `session.py`，原生模型答案通过标准 `AIMessageChunk` 流出并在工具调用前完成聚合。
- 原生 runtime 如仍需读取旧图 checkpoint 形状，只能通过 `runtime/legacy_bridge.py`；迁移完成后删除这一处桥接即可收口依赖。
- `agent/runtime/prompt.py` 组装原生指令、可信身份与有界上下文；工具能力由 schema 描述，
  不在编排层叠加各领域的操作提示。`conversation_store.py` 定义完成轮次的存储边界。
- 收藏业务不进入通用循环。`preparation.py` 只读取目标与本人状态，再返回确认结果；
  `runtime/pending.py` 管理跨领域已签名待办的撤销和替换；`transactions.py` 只执行已核验的具体操作，复用签名、权限、幂等与回执机制。
- `runtime/confirmation.py` 适配签名执行入口和确认控制帧，SSE 入口不承担领域生命周期。
- 领域终结结果使用 `runtime/results.py` 的小型结果类型。确认令牌只进入持久化与控制帧，
  不回送模型，也不写入工具 trace。`runtime/writes.py` 共用持久化执行和重放。
- 包初始化文件不导入运行时或注册全部领域，避免隐藏的启动副作用。

## 后续收敛

目前 `agent/graph.py`、`agent/authz.py`、`agent/prompt_context.py` 与 `tools/base.py`
仍是较大的旧模块。新增领域先确定业务归属、外部 I/O 和验收方式；迁完一项就删除旧入口。
不会继续向旧图添加新领域。共享组件只在出现真实复用时提取，不为未来假设建立通用框架。

现有测试仍以平铺目录为主；目录拆分需要同步完整收集与跨套件隔离机制。生产模块和测试的
归类应随已验收的领域一起迁移，避免留下两套长期并行实现。

## Python 导入迁移

根包 `agent` 不再隐式加载图或重导出工厂。调用方使用明确入口：

```python
from agent.agent import create_agent
from agent.checkpoint import build_checkpointer
from agent.runtime.hybrid import HybridArm
from agent.runtime.turns import conversation_turn
from agent.domains.favorites import preparation, transactions
```

旧 `agent.hybrid_*`、`agent.turns` 与 `agent.favorite_transactions` 路径已随归类迁移，
仓内调用方与探针已同步更新。
