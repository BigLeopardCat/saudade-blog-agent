# ADR-0028：普通工具使用框架 handler 执行

状态：接受。

模型和工具节点由 LangChain `create_agent` 调度。工具中间件通过 `handler(request.override(tool=...))` 注册本轮已授权的动态工具；调用、验证、`ToolRuntime`/状态注入、回调、工具异常和 `Command` 校验交给标准 `ToolNode`。应用不再从 Session 手工 `.invoke()` 工具。

按需加载领域仍是轻量的应用目录操作。未加载工具、身份拒绝、待确认后延迟的调用可以由中间件直接返回 `ToolMessage`，这属于框架支持的拦截行为。

业务工具中的 `ToolResult` 与待确认 `TurnResult` 是产品数据，需保留其结构信息。适配器保留原始工具 schema 和函数签名，将其输出转换为标准 `content_and_artifact`：可读内容进入模型，结构化数据供证据与确认策略使用。签名凭据仅属于待确认输出，不能进入模型正文或业务 trace。读取证据后移除临时数据副本，避免工具全文和 data 在 checkpoint 中重复保存。

通用工具可以直接返回标准 `ToolMessage` 或 `Command`，框架状态更新在后续工具轮可见。应用继续管理授权、确认、幂等执行账、结果规模和结构化用户事件。

此项不打开全批次并行。现有证据、确认和历史观察会修改共享状态，需要先消除这些依赖，再对独立读取开放有界并发。领域激活与会话预算策略也不由这个输出适配器决定。

验证覆盖实际标准 Agent 的动态工具、可信运行上下文注入、回调恰好一次、`Command` 跨轮状态、参数纠正、标准工具异常、确认接续、批次延迟和取消恢复。原测试直接调用 Session 私有方法的路径已迁移到真实执行路径。

参考：[LangChain custom middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom)、[Tools](https://docs.langchain.com/oss/python/langchain/tools)。
