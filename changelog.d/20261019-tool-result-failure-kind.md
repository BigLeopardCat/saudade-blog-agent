- **工具的失败路径不再伪装成成功（⚠️ 有对外行为变更）。** `execute_node` 那一行是
  `getattr(out, "kind", "ok")`——**"没声明 kind"与"声明了 ok"读出来逐字相同**。于是
  `device_oled_display` 的 8 条硬失败（身份取不到 / 屏文超 64 字 / 设备 id 非法 / 404 /
  409 离线 / HTTP 非 200 / 抛异常）与 `get_weather` 的 `except` 那条都是**裸字符串** ⇒
  判 PASS ⇒ 产回执 ⇒ 落 `execution_log` ⇒ 下一轮 `recent_executions` 注回提示词 ⇒
  planner/narrator 读成**系统确认的事实**。实测最刺眼的一格：设备离线时那条回执被 Rust
  渲染成「屏幕显示「你好」」——主人下一轮问"你真显示了？"，agent 会**据实转述**它。
  现在：404 判 `not_found`（查无此设备不是故障）、409 / HTTP 非 200 / 身份 / 异常判
  `unavailable`、参数不合法判**新 kind `invalid_args`**（与 `args_parse` 分开——那个是
  "解析不出"，这个是"解析出来了、值不合法"；过程行显示「参数不合法」而不是「服务不可用」）。
  这四种 kind 在 checker 眼里**全判 BLOCK ⇒ 不进回执**，真下发那条照常 PASS。
  判据：`tests/test_tool_kind_contract.py`（新——工具返回值契约的登记表：每件 `@tool` 的
  裸文本返回必须登记、台账双向、`failure` 必须带收口批次，外加"工具能声明的 kind ⊆
  `_check_spec` 认得的 kind"防下一个新 kind 静默落进 PASS）+ `tests/test_hardening.py`
  的五条 device 失败逐条判 BLOCK（带"真下发仍 PASS"的正控）。
