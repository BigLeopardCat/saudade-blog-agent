# 原生运行时迁移记录

旧规划引擎已退役，当前默认构建入口是 `NativeRuntime`，模型直接选择标准 LangChain 工具；代码负责权限、确认、业务版本、幂等、取消和回执。

后续框架内核收敛遵循 [ADR-0024](adr/adr-0024-framework-kernel-and-domain-extensions.md)；
本记录只说明旧引擎退役后的在役状态，不表示框架复用和插件化已经完成。

## 当前边界

- `NativeConversationStore` 负责消息历史、待确认状态、完成轮次和删除屏障。
- `NativeRuntime.stream_events` 产出正文增量与一个最终 `TurnResult`；最终正文只保存和发送一次。
- `agent.transport` 与 `server.py` 共同维护既有 HTTP/SSE 协议。
- PostgreSQL checkpoint 和进程内 saver 都保留；未配置持久化时仅用于离线开发。

## 已删除的旧入口

旧 graph、planner 文本计划、workflow DSL、纠偏模块和 hybrid 适配层已物理删除。Git 历史保留旧实现，源码中不再提供隐藏回退开关。

## 接续与故障语义

读取轮次中断后可重新读取；系统不宣称每一步都能从崩溃点恢复。写入在领域事务中原子提交，响应丢失或结果未知时不自动重复写入。会话锁、删除屏障、确认卡替换/取消/过期和已完成回执继续有效。

## 验证

离线测试使用可控模型、saver 和 HTTP/SSE 桩，不连接生产数据库或真实模型。跨仓契约测试需显式提供固定的 Saudade-Blog checkout。
