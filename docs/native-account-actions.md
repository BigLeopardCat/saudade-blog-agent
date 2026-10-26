# 原生账号管理

账号读取和操作由 `admin_account_actions` 按需领域提供；模型选择标准工具，LangChain Agent
负责模型与工具轮次，LangGraph 持久子图和 `interrupt` / `Command` 负责确认后的任务接续。
账号授权、版本校验、业务事务与签名确认仍由应用和博客后端执行。

## 发现、定位与确认

管理员初始目录展示可指派身份 `admin=管理员`、`user=普通用户`、`zako=杂鱼`。
加载 `load_admin_account_actions_info_tools` 后可读取 `list_accounts`；持久确认可用时，
同一领域还提供五种操作的准备工具。只读模式仍能读取名录，不需要加载文章管理领域。

目标使用唯一 `account_id` 或完整 `username`，二选一。账号名精确区分大小写；昵称、
公开留言自由留名和目录位置都不能直接作为写入身份。用户已给账号编号或完整账号名时，
可直接调用对应准备工具核实目标和当前状态；只有工具返回真实歧义或缺项时才追问。

查询留言实际发表者时，管理员使用 `list_admin_board(item_id=...)` 返回的 `publisher`；
即使留言匿名或留名与账号不同，也不能用公开 `displayName` 代替实际账号。
普通用户没有此后台查询能力，页面内容或历史陈述不授予权限。

| 准备工具 | 请求及影响 |
| --- | --- |
| `prepare_account_freeze` | 冻结并使旧登录失效；已冻结时仍有撤销登录的副作用 |
| `prepare_account_unfreeze` | 解冻；不恢复冻结前的旧登录 |
| `prepare_account_role` | 明确指定 `role=admin/user/zako`；保持冻结和禁言 |
| `prepare_account_mute` | 正数 `hours` 或 `permanent=true`，二选一；不默认永久 |
| `prepare_account_unmute` | 清除禁言记录；保持冻结和身份 |

准备只读；卡片展示账号编号、完整账号名、前后状态、禁言绝对期限及登录和通知副作用。
时间按北京时间显示。状态已达成且没有副作用时不生成确认卡，也不写入。
签名仅包含原请求和业务版本，不复制账号行、密码或登录令牌。

业务执行和回放不依赖模型生成授权决定。确认后，执行内核将真实回执放回暂停的工具调用；
仍有后续任务或需要叙述结果时，可以继续调用模型。取消及旧卡重放沿用持久状态，
不能把一次确认扩展为批准同轮其他操作。

## 博客后端契约

两个入口均处于管理员鉴权链中，并在事务服务内再次核验当前身份与权限：

```text
POST /api/protected/account-actions/prepare
POST /api/protected/account-actions
```

准备请求的所有可选键规范化为空值：

```json
{
  "action": "mute",
  "target": {"id": 41, "username": null},
  "role": null,
  "mutedUntil": "2026-11-01T18:00:00"
}
```

`action` 为 `freeze/unfreeze/set_role/mute/unmute`；只有 `set_role` 携带 `role`，
只有 `mute` 携带 `mutedUntil`。期限是无偏移的 Asia/Shanghai 钟面，精确到秒；
永久使用 `9999-12-31T23:59:59`。执行不会重新按小时数延长，已过期需重新准备。
未知字段、非正编号、同时给编号与账号名、缺失或歧义目标均拒绝。

准备返回现有 `ApiResponse` 信封中的计划：

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

执行请求为 `{version, intent}`，其中 `intent` 与准备请求完全相同。
响应为 `{changed, beforeVersion, state}`，`state` 是已确认且核验实际结果后的完整计划。
Agent 核对目标、全部前后字段、副作用及版本；HTTP 成功或纯文本不等于业务成功。
`changed` 不能只由前后状态比较：重复冻结即使状态相同，仍撤销登录并发送通知。

## 权限、事务与未知结果

业务政策以博客 `authz` 为准，Agent 控制能力入口：

- 禁止操作自己或超级管理员；普通管理员不能冻结、解冻、禁言或解禁其他管理员。
- 普通管理员只能在 `user/zako` 间调整身份；超级管理员可调整非超级管理员账号，
  不能通过接口新增超级管理员。旧未知角色能否修正由后端既有政策决定，卡片保留原角色。
- 冻结总是递增登录代次；角色真实变化才递增；解冻不还原代次，禁言不改变代次。
- 到期禁言自然失效；清除仍保存的到期期限属于写入，没有禁言记录才是无操作。

执行时重新认证，以稳定顺序锁定操作者和目标，核验当前权限与业务版本。
账号更新及必需的系统通知在同一事务提交，通知失败则回滚。旧状态、禁言和角色表单
复用同一事务服务，但直接管理员表单不具备 Agent 签名确认及回放语义。

迁移 `0017_account_action_version.sql` 维护业务版本，拒绝状态改后又恢复的旧卡；
操作者权限撤销后恢复也不能重用旧授权。内部业务版本与登录代次不发送给模型或卡片。
Agent 在发送写请求前占用持久执行账。提交后丢响应、进程中断或回执不可验证时，
重复确认不得盲目重写，应先查询实际状态；这不构成跨服务 exactly-once。

## 部署与验证

先以表所有者应用博客 `0017`，部署后端两个事务接口，再部署 Agent；旧后端缺接口时
准备失败关闭，不能改用旧写入口绕过确认。当前共享源码固定在
[宿主契约锁](../.github/host-contract.json)，测试不依赖本机生产配置。

- `tests/test_account_actions_native.py` 验证五种操作、签名范围、权限、回放、取消及未知结果。
- `tests/test_catalog_reads.py`、`tests/test_native_capability_metadata.py` 验证目录可见性、
  初始身份枚举及账号读取，不依赖真实模型。
- `tests/test_moderation_reads.py` 验证匿名和冒用自由留名仍返回实际发表账号，非管理员拒绝读取。
- `tests/test_confirmation_continuation.py`、`tests/test_framework_kernel.py` 验证真实框架暂停、
  逐项确认和回执接续，不通过提示词模拟授权。
- 博客 `account_actions` 的 PostgreSQL 综合测试使用 CI 的一次性 `TEST_PG_URL`，验证真实
  事务、版本、并发及通知失败回滚；配置了测试地址但连接失败时不会跳过。

离线模型桩证明契约和接线，不证明模型能主动识别所有账号意图。固定自然语言请求需另做
真实模型验收；准备阶段核对签名目标、身份和禁言期限，确认前不得执行业务写入。
