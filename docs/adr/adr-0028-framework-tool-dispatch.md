# ADR-0028：普通工具使用框架 handler 执行

- **状态**：已采纳。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/5e3d89e18f77ea0edf5c89811deb1346f02e80aa/docs/adr/adr-0028-framework-tool-dispatch.md)。

## 决策与依据

LangChain `create_agent` 调度模型与工具节点，middleware 通过
`handler(request.override(tool=...))` 接入本轮已授权的动态工具。
参数校验、ToolRuntime、回调、异常和 Command 检查交给标准 ToolNode，
普通工具不从 Session 手工 invoke。

未加载、未授权或因确认等待而延迟的调用可以被中间件拦截并返回标准 ToolMessage。
工具目录可见性不能代替每次执行授权。

## 产品适配与限制

业务 ToolResult 映射为标准 content_and_artifact，保留原 schema 和函数签名；
正文供模型阅读，artifact 提供结构化事实。凭据和确认状态不能泄露到模型正文或 trace，
也不因保存 artifact 重复持久化工具全文。

通用工具可以返回 ToolMessage 或 Command；应用继续负责授权、确认、
业务执行账、结果规模和用户事件。此项不自动打开整批并行，
共享证据、审批和预算依赖尚须由有序执行保障。

回归应经过真实 ToolNode/middleware 接线，覆盖动态工具、运行上下文、一次回调、
Command 状态、异常、确认延迟及取消恢复，见
`tests/runtime/test_framework_tool_dispatch.py`。只单测工具函数不能证明框架实际允许调用。
