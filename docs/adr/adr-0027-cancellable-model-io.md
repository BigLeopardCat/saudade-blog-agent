# ADR-0027：同步 Agent 通过标准异步模型 I/O 响应取消

- **状态**：已采纳，2026-10-28。
- **影响范围**：模型传输、应用资源生命周期与取消观测。
- **判据**：`tests/providers/test_cancellable_model.py`、`tests/providers/test_cancellable_socket.py`、`tests/runtime/test_model_io_recovery.py`。

## 背景

同步 SDK 等待响应时，停止标记只有在下一次回调才能检查。已有任务许可会等生产者
退出后释放，checkpoint 也可恢复，但停止后仍可能等待上游数据才能继续原任务。

## 决策

保留同一个 LangChain Agent、LangGraph checkpoint、同步业务工具和确认事务。
模型 I/O 通过 AnyIO 的 BlockingPortal 调用 LangChain 的标准异步模型方法。
这层只传递 SDK chunk 与返回值，不规划任务、不选择工具、不保存会话状态。

- 每个应用拥有一个延迟启动的 portal 与异步 HTTP 连接池，由现有 ExitStack 管理；
  请求工作全部退出后再关闭。取消单次调用不关闭共享客户端。
- 工作线程检查同一停止标记与绝对截止时间，取消对应异步任务，关闭本次响应并等清理完成。
  HTTP 响应关闭使用取消屏蔽，避免清理本身再次被打断；尚未取得响应头也可取消。
- SDK 回调、推理回放、usage、模型参数与会话路由保持原契约。异步任务继承调用者上下文，
  并发请求不共享身份、追踪或会话头。
- 模型和工具循环仍由框架调度；已提交读取与确认回执从原 checkpoint 恢复。
  被取消的模型调用可以重发，不把半条回复提交为完成结果，也不重复已完成读取。
- `model.transport_closed` 记录清理耗时、响应数量与关闭状态。取消缺少最终 usage 时保持未知。

## 取舍与边界

现有业务工具和持久化已经使用同步接口；为了取消模型网络等待而同时改写所有业务事务，
会扩大本批风险。采用库提供的跨线程互操作接口，不实现另一套 Agent 执行引擎或 SDK 协议。
标准库扩展点之外不访问私有 socket，不杀线程，也不通过缩短正常读取超时反复重发请求。

这只能证明本地请求与资源已关闭，不能保证服务商立即停止计算或计费。
同步业务工具的取消仍在已有操作边界检查；实际写入继续依赖确认绑定、版本和执行账。

参考：[AnyIO 跨线程调用与取消](https://anyio.readthedocs.io/en/stable/threads.html#running-code-from-threads-using-blocking-portals)、
[LangChain 模型接口](https://docs.langchain.com/oss/python/langchain/models)。
