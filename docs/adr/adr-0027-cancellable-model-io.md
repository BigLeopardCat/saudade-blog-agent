# ADR-0027：同步 Agent 通过标准异步模型 I/O 响应取消

- **状态**：已采纳；清理期限和失败后的池状态由 ADR-0037 补充。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/5e3d89e18f77ea0edf5c89811deb1346f02e80aa/docs/adr/adr-0027-cancellable-model-io.md)。

## 背景与决策

同步 SDK 等待上游数据时，停止标记可能迟迟没有检查机会。
保留现有 LangChain Agent、LangGraph checkpoint、同步业务工具与事务，
模型网络 I/O 使用 AnyIO BlockingPortal 调用标准异步模型方法，
不为网络取消另写 Agent 循环或改造全部业务事务。

应用拥有延迟启动的 portal 和共享异步 HTTP 池，由资源生命周期管理。
每次调用使用同一取消信号与绝对截止时间；停止取消对应异步任务，
关闭本次响应并观察生产者退出，正常取消不关闭健康共享池。
上下文、回调、chunk、usage 和服务商参数保持原契约，不共享不同用户身份。

## 取舍与验证

取消 Future 不证明生产者退出，关闭响应也不保证服务商停止计算或计费；
缺少最终 usage 时保持未知。同步业务工具仍在安全操作边界检查取消，
写入继续受确认、版本和执行账保护。

响应清理的取消屏蔽必须有期限；失败停止复用连接池，
具体由 [ADR-0037](adr-0037-bounded-model-cleanup.md) 补充。
不访问私有 socket，不杀线程，不通过缩短正常超时不断重发请求。

回归见 `tests/providers/test_cancellable_model.py`、
`tests/providers/test_cancellable_socket.py` 和 `tests/runtime/test_model_io_recovery.py`。
