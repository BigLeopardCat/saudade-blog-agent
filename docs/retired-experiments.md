# 已退役实验

2026-10-24 主线收敛为单一 `native` Runtime；旧图与混合臂的历史实现不再是受支持入口。
实验源码保留在分支 `codex/archive-experiments-20261022`，基点为 `563c8a5`。
历史源码可通过该分支或提交恢复，主线不再提供这些运行入口。

| 退役入口 | 原实验问题 |
| --- | --- |
| `react` | 技能菜单与另一套规划、执行、叙述循环 |
| `tools` | 一次暴露全量工具菜单的对照 |
| `capability` | 能力索引及按能力切换工具菜单 |
| `checkpoint`、`checkpoint_random`、`checkpoint_delta` | 持久化、线程复用与历史注入的对照 |
| schema 索引档 | 关闭的 `index=True` 与 `describe_skill` 原文取回实验 |
| `matrix` | 旧参数矩阵运行与独立留档身份 |

专属模块、测试、驱动脚本和生产图中的 checkpoint 实验钩子同时撤除。
生产共享的鉴权、帧协议、状态 reducer、持久化、幂等与用量统计继续保留。
PostgreSQL 接续验收改由 `eval/native_checkpoint_probe.py` 使用公开
NativeConversationStore 接口完成；旧探针随混合臂退役。
历史报告保留原目录身份，不覆盖生产基线，也不因入口退役而自动删除。

## 保留的结论

- 工具菜单应按领域和当前需求加载，避免每轮重复传递整个能力表及工具 schema。
- 模型负责理解意图、选择工具与读取结果；代码落实权限、目标校验、确认、幂等和真实回执。
- checkpoint 验收需要跨轮、跨进程、并发、取消与清理，不能只验证“能写入”。
- 架构对照按同一组用户请求的结果、成本和延迟比较；内部节点或模板通过率不能直接互比。

这些结论不代表真实模型或生产 PostgreSQL 已验收。已有实验评估文档保留为历史记录，不再作为当前运行方式。新探索在短期分支中完成，
结论和验收证据明确后选择合入或退役。

## 单运行时入口清理（2026-10-26）

- 撤除 `golden_arm` 单选选择器以及 `AGENT_ARM`、`GOLDEN_ARM` 的选臂用途。
  旧值 `native` 只作为部署迁移输入接受；其他值拒绝，避免旧配置被静默解释成另一套运行时。
- 删除无消费方的 `AGENT_RECURSION_LIMIT`、`PLANNER_NATIVE_THINKING`、`AGENT_FASTPATH`。
  循环受请求级 token、模型和工具次数预算控制，当前参数见 README。
- 删除依赖旧 planner/gate/task 帧的 `arm_capability.py`、`dial_matrix.py`、
  `agent_metrics.py`、使用 SDK 私有方法打桩的 `usage_probe.py` 及其专属测试；旧实现可从提交 `a999e2f` 恢复。
  当前验证使用原生契约夹具、HTTP/SSE 行为测试及 `request_metrics.py`、`trace_metrics.py`。
- PostgreSQL 诊断移到 `eval/native_checkpoint_probe.py`；用量统计复用 trace 和 `eval/usage.py`。
  历史报告、ADR 和旧 trace 的只读解析不改写。
- PostgreSQL 会话锁的历史命名空间属于存量协议，保持稳定以支持部署期间的新旧进程互斥。

本轮只统一预算口径和输出配置。摘要仍由宿主提交历史，覆盖水位与主任务预算优先级需要后续
跨服务处理；不能据此声称记忆压缩已完整验收。
