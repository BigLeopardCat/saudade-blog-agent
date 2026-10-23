# 原生账号管理契约与集成交接

本领域提供 freeze_account、unfreeze_account、set_account_role、account_mute、account_unmute
五项账号管理操作。旧 graph、默认配置与全局提示词尚未退役或改写。

## 模型入口与确认

模型选择 `load_admin_account_actions_info_tools` 后获得五个 `prepare_account_*` 工具。
准备要求已验签管理员、稳定用户与会话；目标为 `account_id` 或完整 `username`，二选一。
账号名精确区分大小写，不用昵称、模糊搜索或目录位置代替账号身份。

- `prepare_account_freeze`：冻结，并使旧登录状态失效；已冻结仍有撤销登录的副作用。
- `prepare_account_unfreeze`：解冻；不会恢复冻结前旧登录。
- `prepare_account_role`：指定 `role=admin/user/zako`；保持冻结和禁言。
- `prepare_account_mute`：指定正数 `hours` 或 `permanent=true`，二选一；不默认永久。
- `prepare_account_unmute`：清除禁言期限；保持冻结和角色。

卡片展示账号编号、完整账号名、前后状态、禁言绝对期限与登录/通知副作用。
时间明确为北京时间。准备只读，签名仅含请求及业务版本，不复制账号行或密码、令牌。
确认、重复确认与取消复用现有原生生命周期，不调用模型。

## 后端契约

两个入口必须置于完整管理员鉴权链，同时在服务内部独立核验业务权限：

```
POST /api/protected/account-actions/prepare
POST /api/protected/account-actions
```

只读准备请求（所有可选键规范化为 null）：

```json
{
  "action": "mute",
  "target": {"id": 41, "username": null},
  "role": null,
  "mutedUntil": "2026-11-01T18:00:00"
}
```

`action` 为 freeze/unfreeze/set_role/mute/unmute；只有 set_role 携带 role，
只有 mute 携带 mutedUntil。mutedUntil 是无偏移的 Asia/Shanghai 钟面，精确到秒；
永久沿用 `9999-12-31T23:59:59`。执行不会重新按小时数延长期限，已过期须重新确认。
未知字段、非正编号、同时给编号和账号名、歧义或缺失目标均拒绝。

准备返回现有 ApiResponse 信封中的计划：

```json
{
  "version": "64位小写十六进制摘要",
  "before": {"id": 41, "username": "fixture", "role": "user", "status": 0, "mutedUntil": null},
  "after": {"id": 41, "username": "fixture", "role": "user", "status": 0, "mutedUntil": "2026-11-01T18:00:00"},
  "changed": true,
  "revokeSessions": false,
  "notificationCount": 1
}
```

执行请求为 `{version, intent}`，intent 为同一个规范化准备请求。
响应为 `{changed, beforeVersion, state}`，state 是已确认且核验实际结果后的完整计划。
Agent 严格核验目标、全部前后字段、副作用与版本；不把 HTTP 成功或纯文本当成执行成功。
`changed` 不能只由 before/after 比较：重复冻结状态虽相同，仍撤销登录并发送一条通知。

## 权限、事务与未知结果

权限政策复用博客 `authz`，Agent 仅控制当前能力入口：

- 禁止操作自己或超级管理员；普通管理员不能冻结、解冻、禁言、解禁其他管理员。
- 普通管理员只能在 user/zako 间调整身份；超级管理员可调整非超级管理员账号，
  不能通过接口新增超级管理员。历史未知角色能否被修正仍由既有 `check_role_change` 决定；
  确认卡如实展示原角色，不把未知角色当成普通用户。
- 冻结总是递增内部 token_version；角色真实变化才递增；解冻不还原代次，禁言不动代次。
- 到期禁言自然失效；解除仍存有到期期限的记录是清理写入，无记录才是 no-op。

后端执行时重新认证、按稳定顺序锁定操作者与目标，核验权限与版本。
账号写入及必须发送的系统通知同一事务提交，通知失败则回滚账号变化。
旧 status/mute/role 表单入口复用同一事务服务。

`0017` 的相关字段版本用于拒绝状态改后又恢复的旧卡，操作者权限撤销后恢复同样失效。
版本与内部 token_version 只用于核验，不向模型或卡片暴露内部认证字段。

Agent 在发送业务写入前占用持久执行账。提交后丢响应、进程中断或无法验证回执时，
重复确认不得再次写入，应先查询实际状态。系统不声称跨服务 exactly-once。
旧表单入口是直接管理员操作，不具备 Agent 签名确认与持久回放语义。

## 集成顺序与验证边界

1. 审查双仓提交，合并共享注册文件时保留其他领域能力。
2. 在隔离数据库应用博客 `0017_account_action_version.sql`，验证新旧入口、事务失败回滚、
   权限变化、版本冲突及完整 Router 鉴权；不得用生产数据库运行旧整套集成测试。
3. 先部署含事务服务和新接口的博客后端，再部署 Agent。旧后端缺失时准备应失败关闭，
   原生领域不能绕过确认改用旧写入口。
4. 使用自有临时账号与会话验证自然语言准备、确认、取消、重复确认和响应丢失；
   确认阶段模型调用应为零。真实模型意图评测另行验收。

离线行为测试：`tests/test_account_actions_native.py`，使用模拟 HTTP 和本地持久执行账，
不依赖模型密钥或生产配置。完整回归及双仓实际验证结果在交付说明记录，
测试桩通过不能替代后端真实事务及 Router 验证。

## 初次交付验证（2026-10-24）

- 最终完整 Agent 离线套件：1074 passed、2 skipped；强制读取本分支博客源码进行跨语言契约检查。
- 新增账号行为用例：40 passed；五种原生往返均拒绝旧 planner，确认与回放零额外模型调用。
- F821、类型、lint、盲捕获及安全扫描门禁通过，未调整基线或质量阈值；模块/复杂度、索引及碎片检查通过。
- 博客完整离线 lib：281 passed、1 ignored；既有 API 测试 17 passed。
- 专属临时 PostgreSQL 领域综合测试：1 passed，实际覆盖新旧完整 Router、业务版本与权限变化、
  操作者及目标状态恢复、精确禁言期限、并发重复和通知/更新故障原子回滚；临时容器已清理。
- `0017` 新建、重复应用不回拨版本及 quota 不递增版本探针通过。

Agent 的提交后响应丢失通过 MockTransport 模拟；后端通过提交后丢弃返回再用旧版本重交模拟。
上述初次交付未做真实网络故障压测、生产模型意图评测或部署。

集成后数据库测试复用博客 CI 的一次性 `TEST_PG_URL`，随标准 `cargo test` 执行；
未配置时打印跳过原因，配置后连接或断言失败均使 CI 失败。
配套博客提交为 `b0566d26c999f34e9ad36a588902bd9e89a32a1c`，
[后端 PR #86](https://github.com/BigLeopardCat/Saudade-Blog/pull/86)。集成后的实际后端版本以 `.github/host-contract.json` 为准。

## 集成验证（2026-10-24）

保留通知、设备及空置知识库退役改动后，Agent 完整离线回归 1108 passed、2 skipped。
类型、lint、异常捕获、安全与静态词表门禁均通过；未修改质量阈值或词表登记。
博客离线库 284 项返回成功，前端 62 组脚本与 180 项单测通过。
本机未配置数据库测试 URL；实际事务由博客标准 PR CI 的临时 PostgreSQL 验证。
