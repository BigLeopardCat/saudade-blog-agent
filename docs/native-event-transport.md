# 原生事件传输边界

`agent.transport` 是 Runtime 与博客 HTTP 协议之间的薄适配层。Runtime 只需要实现：

```python
runtime.stream_events(state, config)
# Iterator[AIMessageChunk | TurnResult]
```

`state` 只携带 `messages` 和可选的 `confirm_grant`。`config` 负责验签身份、会话坐标、
停止信号、进度回调和当前请求预算（预算对象也由 `RequestJobs` 的请求作用域共享）。
Runtime 负责会话存储、授权、确认、取消、幂等和持久化；
传输层不读取 graph state，也不解析 planner、execute 或 gate 更新。

每个正常迭代器必须产生且只产生一个 `TurnResult`。`AIMessageChunk` 是已经允许展示的正文
增量；`TurnResult.reply` 是最终保存的完整正文。传输层只补齐还没有从增量中发出的后缀，
绝不会再次发送完整 reply。若两者前缀不一致，适配器失败关闭，避免重复或错正文。

`TurnResult.execution` 继续使用现有结构化回执：

- `receipts[*].cmd` 编码 `__CMD__`，并同步给非流式响应使用的 `AUTO_NAVIGATE`、
  `NAVIGATE`、`EFFECT`、`DARKMODE` 连线格式；不从正文猜命令；
- 回执整体编码 `__EXEC__`；
- 确认、取消和替换卡由 `agent.runtime.confirmation.frames` 统一生成
  `__PENDING__` / `__CONFIRM__`，传输层不把令牌、规格等额外字段直接暴露；
- no-op、空回复和保存失败分别由 Runtime 的终态、事件错误和 HTTP 现有兜底处理。

队列顺序是：正文增量、结构化执行帧、一个正常哨兵。Worker 异常先入队异常，再入队哨兵，
HTTP 层只发安全 `__ERROR__`，不会再发 `__END__`。取消使用独立哨兵，不伪装成成功结束。
并发许可由 `RequestJobs` 持有到 worker 真正完成；队列排空和客户端关闭不会提前归还许可。

当前分支已经接好传输适配器，但依赖 `codex/native-runtime-core` 提供的 `stream_events`。
在该分支未集成时，服务会明确报 Runtime 接口缺失，而不会把旧 graph 节点事件翻译成另一套
伪原生事件。旧 graph 仍由显式兼容入口负责；它不是原生传输层的依赖。

离线测试使用可控 fake Runtime，覆盖正文去重、命令顺序、确认字段边界、取消/no-op、空回复、
存储异常、worker 哨兵和同步适配器结果一致性；没有生产数据库、模型或网络调用。

## 服务切换

服务工厂现在只构建 NativeRuntime，默认及示例为 AGENT_ARM=native；旧 graph/hybrid 配置
会在启动时报错，部署时须同步更新。关闭传输迭代器会关闭上游 Runtime，队列写入有超时和
停止围栏。producer 的 stream_end 记录已入队正文，涵盖确认正文后缀；凭据不进入普通 trace。

旧四节点源码门禁已迁移为真实 HTTP/SSE 接续、持久化和无重复历史行为测试。
旧 RESET 和文本计划自动结算只保留尚在使用的历史库/前端读取测试，不作为原生 producer 契约。
全量线上模型基线仍挂起；离线结果不能代替真实模型的功能质量验收。
