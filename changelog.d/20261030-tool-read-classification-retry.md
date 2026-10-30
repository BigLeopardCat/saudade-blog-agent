- **读取失败不再用一句 `unavailable` 与异常原文打发模型（⚠️ 有对外行为变更）。**
  公共读取（`_get`）与身份读取（`_principal_get` / `_admin_read_post`）此前任何失败
  都落成同一条“服务暂时不可用”，`_principal_get` 还把 `str(exc)` 拼进工具结果——模型
  分不清“参数错了 / 查无此物 / 没权限 / 上游抖了一下 / 响应坏了”，运维也可能在结果里
  看到异常原文。现在统一走 `tools/http_channel.py`：`ToolResult.meta` 带闭集
  `error`（`invalid_args` / `not_found` / `forbidden` / `upstream_unavailable` /
  `invalid_response` / `upstream_rejected` / `write_unknown`）与 `attempts`，文本只保留
  固定措辞或截断后的后端业务消息，不再拼异常字符串。
  读取默认 **两次总尝试**，只重试明确的瞬时时形（连接/读/池超时、协议错误、
  HTTP 408/429/502/503/504）；参数拒绝、权限拒绝、404、坏信封与业务报错一律不重试。
  写入**只发一次**：传输失败、5xx、无法解析的 200、409 等结果无法确认时明确报
  “结果未知”，不自动重放；400/422 明确“本次未改动”。
  只读 POST 仍由调用方显式选择（`payload=` / `_admin_read_post`），不按 URL 猜读写。
  取消不被吞（`asyncio.CancelledError` 继续上抛），运行时 `stop_event` 已置位时不再发起
  第二次尝试；非 HTTP 异常也不再被宽 `except` 转成 `unavailable`。
  判据：`tests/test_http_channel.py`（23 条：瞬时恢复/耗尽、权限/无匹配/参数拒绝不重试、
  坏响应、取消与停止信号、写入只发一次与未知结果、异常原文不外泄、消息截断、
  读写动词由调用方决定）。`tools/base.py` 的 BLE001 盲捕获由 5 条清零，基线已收紧。
