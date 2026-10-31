- 新增 `AGENT_INPUT_TOKENS` 单次请求输入预算（默认 64,000），与模型窗口 `AGENT_CONTEXT_TOKENS` 分离。冷启动没有可命中的提示缓存，历史选取、压缩阈值和目标都按单次输入预算计算，1M 窗口不再等于每次请求都发送近 1M 输入。

- 旧超大 checkpoint 首次恢复时不再把整个历史交给摘要模型：压缩记录 `summary_input_too_large` 并跳过，历史选择器按预算裁剪本轮模型视图；容量类跳过不把会话标记为摘要失败。`/health` 的 budget 拨盘新增 `input_tokens`。

- 判据：`tests/runtime/test_request_budget.py`（输入预算拒绝且窗口仍大）、`tests/runtime/test_history_budget.py`（大窗口下按输入预算裁历史）、`tests/runtime/test_context_capacity.py`（超大旧历史不触发摘要、冷启动视图受限）、`tests/test_summary_coverage.py`（摘要输入超预算不调用提供方）。
