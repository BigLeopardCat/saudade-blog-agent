- **服务限额配置双轨收敛**：`server.py` 里 6 个限额/超时常量改由 `config/settings.py`
  单轨供给，**写 `.env` 就生效**（⚠️ **默认行为零变更**，不设 env 时六个值与收敛前的
  字面量逐个相同）。收敛前是 README 自己列为坑的双轨形状：`AGENT_RECURSION_LIMIT` /
  `AGENT_MAX_BODY_BYTES` / `AGENT_MAX_CONCURRENT` / `AGENT_MAX_REVIEW` 四个在 server.py
  直读进程环境——`.env` 管不到它们，**写进去不生效也不报错**；`STREAM_IDLE_TIMEOUT` /
  `STREAM_TOTAL_TIMEOUT` 两个干脆硬编码，连拨的地方都没有。三件：
  ① **Settings 新增六字段，env 键名一个没换**（`Field(validation_alias="AGENT_*")`）——
  老部署的 systemd `Environment=` 零改动；两个 STREAM 超时是**新开的 env 通道**
  （`AGENT_STREAM_IDLE_TIMEOUT` / `AGENT_STREAM_TOTAL_TIMEOUT`）。`populate_by_name=True`
  同步打开：否则显式构造时 `Settings(recursion_limit=77)` 会被静默忽略（pydantic 默认
  只认 alias），那是"看着传了参、其实没传"的新缝。server.py 侧只保留模块级门牌
  （`RECURSION_LIMIT = settings.recursion_limit` 等），常量名不动 ⇒ `test_hardening` /
  `test_error_frame` 这些等值引用与 monkeypatch 零改动；`import os` 随之删除
  （那 4 处 `os.environ` 直读是它仅剩的用途）。
  ② **判据 `tests/test_settings_surface.py` ⑥（新）**：AST 钉两件事——六个常量右值逐个
  是 `settings.<同名字段>`（门牌在、真值源只有一处）、server.py 里 `os.environ` 字样清零
  （直读进程环境就是第二轨长回来的地方）。行为反控两条：不设 env 时默认值与收敛前字面量
  逐个相同（收敛 = 零行为变化）；setenv 后重新构造即生效（漏写 validation_alias 时这条
  当场红）。**反控实测**：把 `RECURSION_LIMIT` 一行改回 `os.environ` 直读 ⇒ 两条当场红，
  且第一条精确点名 `未单轨：RECURSION_LIMIT`。既有判据随收敛反转/对账：④ 从"README 必须写
  下这个坑"反转为"README 不许再教人走旧路"；②③ 的字段↔键对账改走 `_env_name`
  （validation_alias 感知，AST 读、注释里写别名不算数——同文件 20261017 `_consumers`
  换法的同源纪律），四个 AGENT_* 键随之移出 `_PROCESS_ONLY` 名单。
  ③ **文档同步**：README《环境变量》一节由"配置分两处"改写为单轨叙事——B 表（"写进
  `.env` 一点作用都没有"那张，连同一句"流式超时同样不通过环境变量调"）删除，六键并入
  A 表并注明键名与 systemd 时代逐个一致；`.env.example` 新增六键条目（对照 settings.py
  分节，判据 ② 的 57/57 全覆盖由此而来）+ 头部边界说明改写（仍只走进程环境的只剩
  `SAUDADE_IGNORE_ENV_FILE` / `TRACE_TOOL_RESULT_LIMIT` / `GOLDEN_ADMIN_UID` /
  `GOLDEN_USER_UID` 四个）。
