# 秘书类功能：框架与前置需求（20260920 起）

> 状态：**框架已落地、默认不拦（shadow）**。本文件是这套东西的唯一事实源：
> 已建成什么、为什么这样建、还差什么才能真的上线一个"秘书"。
> 代码：`agent/principal.py`（身份）、`agent/authz.py`（权限）、`agent/graph.py::execute_node`
> （唯一判据点）、`server.py::_resolve_principal`（身份来源）、Rust 侧 `src/authz.rs`（角色取值域）。

## 1. 秘书是什么（先把词定清楚）

**秘书 = 以某个人的名义、在那个人的授权范围里办事的 agent。** 两个要件缺一不可：

1. **身份**：它得知道"我现在代表谁"——不是"有个 uid 在请求体里"，而是系统确认过的调用者；
2. **范围**：它得知道"这个人允许我做什么"——而且这个范围要能**被表达、被检查、被审计**。

现在的看板娘两件都不具备（只是"谁在跟我说话"有一个裸 uid）。所以本轮做的是这两件地基，
不是做"发文章/管日程"这类具体秘书功能——**没有地基的功能只会长成又一个散落的 if**。

### 为什么不做成"sub agent"

用户问过的那个问题在这里有答案：秘书**不是**一个新的 LLM 子代理，而是**同一张图上多了一个
判据输入**。理由与全仓架构一致（见 `docs/agent-architecture.md`）：能力与边界都属于
"确定性层"，把一个"秘书 sub agent"塞进叙述层只会再制造一个"决策漂移"的地方
（影子系统事故的教训：两条并存的决策路径必然漂移）。秘书的"聪明"由 planner 的选技能
承担，秘书的"边界"由这里的判据承担。

## 2. 现状测绘（改动前的事实，逐条有代码依据）

| 事实 | 依据 |
|---|---|
| agent 只拿到裸 `uid`，角色在到达图之前就被剥离 | `src/auth_jwt.rs` 的 `auth_uid()` 只返回 `sub` |
| 角色是单值字符串，取值域事实上只有 `admin`/`user`，DB 无 ENUM/CHECK | `src/entity/user.rs` `role: String`；`src/middleware.rs` 与 `src/routes/temp_user.rs` 各硬编码一处 |
| 全站只有一个鉴权中间件，判据是 `role == "admin"` | `src/middleware.rs::auth_guard`（挂 `src/routes/mod.rs` 的 `protected_routes`） |
| 前端角色判定是客户端解 JWT、再硬编码一次 `'admin'` | `frontend/src/components/AuthRouter.tsx` |
| agent → Rust 全是公开只读 GET，**一个 admin 接口都不调** | `tools/base.py` 的 `API_BASE` 只拼 `/api/public/*` |
| agent 唯一的物理写操作是设备屏显，凭证是自己签的用户 JWT（固定 `role: "user"`） | `tools/base.py` 的 `device_oled_display` / `_sign_user_jwt` |
| 执行事实有事后记录（回执），**无事前授权** | `execution_log` + `__EXEC__` 帧链路 |

## 3. 已落地的框架

### 3.1 身份：`Principal`

```python
Principal(uid=7, role="secretary", source="assertion")
```

- **唯一构造点**是 `server.py::_resolve_principal`，两条来源：
  - `source="assertion"`：Rust 签的 60s 断言，**role 取自 DB**（`chat.rs::prepare_chat` 单次
    `find_by_id`，与 `middleware::auth_guard` 同一条纪律：不信登录 token 里可能 7 天前的角色）；
  - `source="body"`：回退信任请求体（`AGENT_REQUIRE_ASSERTION=0` 时的旧路径）——
    **这条路径上 role 恒为 `None`**，绝不因为"读不到角色"而默认授予。
- 取不到 principal（老调用方、直调图的单测）→ `UNKNOWN`（uid=0、role=None）。
- 角色名与取值域：`admin` / `secretary` / `user`，两侧同名（`agent/principal.py` ↔ `src/authz.rs`）。

### 3.2 范围：scope manifest（`agent/authz.py`）

scope 词汇表（`<动作>.<对象>`）：`read.public` / `read.own` / `read.any` /
`write.page` / `write.device` / `write.content` / `admin.console`。

| 角色 | 授予 | 与今天的关系 |
|---|---|---|
| `user`（访客/体验号） | read.public、read.own、write.page、write.device | **= 今天的行为**（设备归属由 device-service 按 uid 校验，是既有事实） |
| `secretary` | 上面全部 + read.any、write.content | 新增档：能读他人数据、能代写；**进不了后台管理面** |
| `admin`（博主） | 全部 | = 今天的行为 |

工具级的 `TOOL_SCOPE`（22 个工具一个不漏，**完备性由 `test_authz.py` 在 CI 层锁死**：
新增工具忘了声明会红，不靠运行时宽容）。`write.*` 三档单独成集（`WRITE_SCOPES`），
留给"人在回路确认"挂钩（前置需求 ③）。

### 3.3 判据：只有一个点

`graph.py::execute_node` 在**调用工具之前**执行 `authz.check(principal, tool)`，与断连检查、
参数引用解析同一层（确定性、无 LLM、无一例外）。**拒绝在结构上不可能被绕过**：执行层没有
第二条路径能碰到工具（这是 20260903 架构裁决的红利——执行器本来就没有自由意志）。

- **shadow（默认）**：决策照算，只把**拒绝**记进 trace（`execute.authz_shadow` 事件），
  行为完全不变。先跑一段真实流量，看谁会撞上授予表边界——用证据校准，而不是拍脑袋配表。
- **enforce（`AGENT_AUTHZ_ENFORCE=1`）**：拒绝产 `__ERROR__: 权限不足[<原因码>]` 帧 →
  checker 判 BLOCK（`reason=scope_denied`）→ 走既有受阻链路（planner 如实收尾 / reflector
  受限复盘）。**不新增决策分支**，也不静默吞掉。
- 失败取向：未知角色、未声明工具一律**拒绝**（fail-closed）。从不默认放行、从不默认管理员。

### 3.4 人在回路：权限之后还有一次"同意"（前置需求 ③ 的 agent 侧）

**权限回答"这个人能不能做"，确认回答"这一次他到底要不要做"**。两者都在同一个确定性点上，
但判的是不同的事——`authz.check()` 通过之后，`execute_node` 还会问一次：

```
requires_consent(principal, tool)         # 只看 scope 是否在 CONSENT_SCOPES —— 声明驱动
  └─ 命中 → consent_granted(principal, tool, 用户本轮消息)   # 确定性正则，无 LLM
        └─ 未获确认 → 产 __ERROR__: 待确认[consent_required] 帧，**不调用工具**
```

- **只对"离开用户眼前"的写入要确认**：今天是 `CONSENT_SCOPES = {write.content}`（代用户
  发留言/说说/文章——对外可见、收不回）。`write.page` / `write.device` 的效果就发生在用户
  眼前（看得见、也改得回来），既有行为一条不动。
- **确认 = 用户本轮消息里说了才算**（`_CONSENT_PATTERNS`，刻意收窄到"确认发布"这类明确
  说法）。fail-closed：一个需确认的 scope 若没配确认语表 → **一律不放行**（不默认同意）。
- **拒绝形态复用既有 blocked 链路**：`__ERROR__` 帧 + `consent_required` 原因码 → checker
  判 BLOCK → planner 去问用户。用 `__ERROR__` 而不是普通文本是有意的：gate 分支 5a
  （错误帧 + 完成式声称 → fallback）因此自动生效，**叙述侧无法把"没执行"说成"已发布"**
  （需要"已经帮你发布好啦"这类句子被判据认出来，见 `_WRITE_CONTENT_CLAIM_RE` 三支的取舍）。
- **今天是空转的**：现有 22 个工具里**没有一个是 `write.content`**，所以这条闸现在一次也
  不会触发（`test_authz.py` ⑨ 的用例就是这条事实的锁）。它等的是第一个写工具——**新增
  写工具时不需要改这段代码**，声明表里给它 `write.content` 就自动落在闸下。

### 3.5 观测：怎么读 shadow 的结果

```bash
# trace 里的 shadow 拒绝事件（一轮一文件，见 docs/eval-observability.md）
grep -l authz_shadow /home/ubuntu/memory_blog_rust/logs/agent/traces/*.json | wc -l
```

判据读法：**拒绝只应来自"身份不明"**（Rust 还没发 role 的过渡期，`reason=unknown_role`）。
若出现 `reason=denied`（角色已认、scope 未授予），说明既有角色撞上了授予表——那要么是
授予表配错了（改表），要么是真越权（保持拒绝）。**这就是打开 `AGENT_AUTHZ_ENFORCE` 前
必须拿到的那份证据。**

## 4. 布线图（一次带角色的对话）

```
浏览器 ──JWT──▶ Rust /api/chat/stream
                    │ auth_jwt::auth_uid → uid
                    │ user::Entity::find_by_id(uid) → role   ← 查 DB，非读 token
                    ▼
        X-Agent-Assertion: {sub: uid, role, aud:"agent", exp:+60s}
                    ▼
              agent /chat/stream
                    │ _verify_assertion_claims → {uid, role}
                    │ _resolve_principal → Principal(uid, role, source="assertion")
                    ▼
        config.configurable.principal ──▶ graph.execute_node
                                              │ authz.check(principal, tool)
                                              ▼
                                   放行 → tool.invoke ／ 拒绝 → __ERROR__[scope_denied]
```

## 5. 前置需求（还差什么才能真的上线一个秘书）

**这些都需要用户拍板，本文件只列事实与建议，不预先做任何一件。**

| # | 需求 | 为什么是前置 | 建议 |
|---|---|---|---|
| ① | **秘书账号与角色落库** | `user.role` 无 ENUM/CHECK，新建一个 `secretary` 角色就是一行 UPDATE/INSERT——**生产库写入，须用户点名「库名+迁移文件」** | **20260920 已执行**（用户点名「memory_blog 库 + `scripts/migration/secretary_role_20260920.sql`」+ 账号名）：该账号(id 17) `role: user → secretary`，幂等 UPDATE、`must_be_1 = 1`、迁移标记 `secretary_role_20260920` 已写。**真实账号名只在本机私有记录里，本文件（公开仓库）一律不写**——同 §7 的占位符纪律。**迁移文件在父仓只留 `REPLACE_ME` 模板**（父仓是公开仓库，真实账号名不入库）。角色生效仍需 agent 侧收 shadow 证据后再开 `AGENT_AUTHZ_ENFORCE` |
| ② | **`AGENT_REQUIRE_ASSERTION` 收口** | ~~断言已落地快照但默认关着~~ **20260920 复核：生产 `.env` 里已是 `1`，实测已生效**——不带 `X-Agent-Assertion` 直连 `/chat` 探针得 **401**（不是静默回退），`/review`、`/graph/query` 不经 `_resolve_principal`，不受影响。代码默认值仍留 `False`（本机/测试环境不必带头） | **无需动作**。回归锁=一条不带头的请求必须 401；将来任何"给 agent 加公网入口"的改动都要先过这条 |
| ③ | **写操作的"人在回路"** | 秘书的价值在写，而今天唯一的防护是"调用前查断连"——没有确认、没有"谁同意了"的记录 | **agent 侧已落地（20260920，见 §3.4）**：需确认的 scope 未获用户本轮明确确认 → 产 `__ERROR__` 帧、**不执行**、叙述侧也说不成"已完成"。**剩下的是 ①（真实秘书账号）与第一个写工具**——写通道（④）与审计（⑥）仍缺 |
| ④ | **agent → Rust 的写通道凭据** | agent 现在一个 admin 接口都不调，"代用户发文章"没有可用的通道：既没有写接口的调用约定，也没有"agent 持用户授权"的凭证语义 | 建议：不要复用用户 JWT 长期有效，而是同一套断言思路——Rust 签发**带 scope 的短时效授权**，写接口按 scope 校验（与 agent 侧的 manifest 同名同义） |
| ⑤ | **前端角色模型** | `AuthRouter.tsx` 客户端解 JWT 硬编码 `'admin'`；Dashboard 侧栏是静态全量列表——"比 admin 窄、比 user 宽"的界面无处安放 | 建议：等 ① 之后再做；先把硬编码换成与后端同名的常量，避免第三处字面量 |
| ⑥ | **审计** | 秘书代表用户做了写操作，事后要能回答"谁、以谁的名义、什么时候、改了什么" | `execution_log` 已有 `skill/detail/created_at`，缺"以谁的名义"（principal）与授权来源；建议在回执顶层加 principal 字段（跨语言契约，同 `digest` 的处理方式） |

**明确不在本轮范围**（防蔓延）：不做秘书的具体功能（日程/发文/审批流）、不动后台路由、
不引入权限表/多角色表、不做前端的秘书界面。

## 6. 分阶段建议

- **P0（已完成）**：身份与范围的地基 + shadow 观测 + 两侧单测与文档（本文件）。
- **P1（进行中，20260920 起）**：① 已执行（一个真实账号 = `secretary`，账号名见本机私有记录；建号/配套助手账号的步骤与默认值见 §7）+ ② 已在生产生效。
  剩下的是**收 shadow 证据**（该账号带角色跑一段真实对话，看 `authz.shadow` 记录里
  "如果打开会拦下什么"）→ 校准授予表 → 打开 `AGENT_AUTHZ_ENFORCE`。此时"秘书能做什么、
  不能做什么"第一次成为**可执行的事实**。
  > **20260921 已决（用户拍板）**：不新增任何建号代码路径。管理员与配套助手账号一律
  > **命令行 + 一条 SQL** 建，"默认是什么"写死在 §7 —— 那份文档就是这条路径的唯一说明。
  > 之所以不写代码：今天根本没有"产生管理员账号"的路径可挂（见 §7.5），为它新开一个
  > 高危接口，得先有审计（⑥），顺序反了。
- **P2（按需）**：③ 人在回路（agent 侧已落地）+ ④ 写通道凭据 + ⑥ 审计，一起做（写操作要
  同时有授权、确认与记录，缺一条就等于没做）。

## 7. 怎么建账号：管理员 + 配套助手账号（命令行 + 一条 SQL，20260921 定稿）

**结论先说**：**不新增任何建号代码**——`create_temp_user` 的 `role` 保持写死 `"user"`、
不上注册接口、也不做"自动配助手"的路径（理由见 §7.5）。管理员账号与配套助手账号一律
**命令行直接写库**；本文档就是这条路径的**唯一说明**，"默认是什么"就是 §7.1 那张表。

> 本文件在公开仓库里，**一律用占位符**：真账号名、真口令、真哈希都不入库。

### 7.1 默认值（唯一一份声明）

| | 管理员（博主本人） | 助手（配套账号） |
|---|---|---|
| 谁 | 一个 | **每个管理员一个**，命名 `<管理员名>-assistant`（归属靠命名可追溯，不建关联表） |
| `role` | `admin` | **`secretary`** |
| `nickname` | 留空字符串 | 留空字符串 |
| 口令 | 建号者当场定的**强随机**口令（≥20 字符） | 同样强随机，**必须与管理员不同** |

- `nickname` 留空是**有意的**：`auth.rs:112` 的 profile 逻辑是 `if nickname.is_empty() { username }`
  ⇒ 空串自动回落到账号名，不会显示成空白。
- 两个角色名都是**跨语言契约**：Rust 侧 `src/authz.rs` 的 `KNOWN_ROLES`（`admin`/`secretary`/`user`，
  大小写敏感、无旧别名）与 agent 侧 `agent/authz.py` 的授予表必须一致，改一侧须同步另一侧 + 两侧单测。
- **助手为什么是 `secretary` 而不是 `admin`**：`secretary` 在 agent 侧拿到
  `read.public/own/any` + `write.page/device/content`，**唯独没有 `admin.console`**；Rust 侧
  `auth_guard` 也只认 `admin`（`authz::can_access_console`，`authz.rs` 尾部的单测锁着
  "秘书不得进后台管理面"）。也就是说"助手能读会写、但进不了后台"不是一份配置，是**两侧各有一处判据**。
- **口令必须不同**：Rust 会**查库**取 `role` 并随身份断言下发给 agent（§4 布线图），
  两账号同口令等于把管理员那一档的权限面抄一份到低权账号上。

### 7.2 口令在 DB 里只认两种格式（命令行建号只能用第二种）

| 格式 | 说明 |
|---|---|
| `$argon2id$v=19$...`（PHC，含算法/参数/随机盐） | 现行格式（20260917 起）。`utils::hash_password` 产出 |
| 无盐单轮 SHA-256 十六进制 | **旧格式，但 `verify_password` 仍认**，且**登录成功那一刻自动升级成 Argon2id**（`auth.rs:69` 的 `needs_rehash` 分支） |

- 命令行建号用第二种：MySQL 的 `SHA2('<口令>', 256)` 正好等于 `utils::encrypt_password`
  （`hex::encode`，小写十六进制）。**不要去找"生成 PHC 的一行命令"**——本机没有 argon2 CLI、
  agent venv 也没有 argon2 模块（20260921 实测），走 SHA-256 路径即可，首次登录就完成升级。
- 这条路真的在跑：`user` 表现存**所有行**的 `password` 前缀都已是 `$arg`（惰性升级的实证）。
- 代价与纪律：SHA-256 口令在 SQL 文本、终端历史、`mysql` 客户端日志里都是**可离线爆破**的 ⇒
  强随机口令 + 建号 SQL 放父仓 `scripts/migration/`（父仓是公开仓库，迁移文件只留 `REPLACE_ME`
  模板，**真口令/真哈希绝不入库**）+ 建完第一次登录即升级，此后行里不再有弱哈希。

### 7.3 步骤（可复制；建号是**生产库写入**，仍按既有约定说清「库名 + 迁移文件」再动）

```bash
# 1) 写迁移文件（模板见下），口令从环境变量代入，别写进命令行（argv 会进 ps/history）
#    scripts/migration/admin_and_assistant_<日期>.sql
```

```sql
-- 幂等取向：**只新增，不自动改已有账号**（见下）
SELECT id, username, role FROM user
 WHERE username IN ('REPLACE_ME_ADMIN', 'REPLACE_ME_ADMIN-assistant');
-- ↑ 先跑这一句。两行都必须为空才继续；已有行时**不要**用 ON DUPLICATE KEY 自动提权，
--   要改角色就单独写一条带 WHERE 的 UPDATE，并先看清它命中了谁。

INSERT INTO user (username, nickname, password, role) VALUES
  ('REPLACE_ME_ADMIN',            '', SHA2('REPLACE_ME_ADMIN_PW', 256),     'admin'),
  ('REPLACE_ME_ADMIN-assistant',  '', SHA2('REPLACE_ME_ASSISTANT_PW', 256), 'secretary');
```

```bash
# 2) 落库 → 3) 核对（只查角色，不查口令）
#    SELECT id, username, role FROM user WHERE username = 'REPLACE_ME_ADMIN'
#       OR username LIKE '%-assistant';
#    记下两行 id（删除接口按 id 操作，见 §7.4）

# 4) 登录冒烟——口令走 stdin，不进 argv / history
read -r -s -p "口令: " ADMIN_PW; echo
curl -s -X POST http://127.0.0.1:3000/api/login \
  -H 'Content-Type: application/json' --data-binary @- <<JSON | head -c 200
{"username":"REPLACE_ME_ADMIN","password":"$ADMIN_PW"}
JSON
unset ADMIN_PW
# 期望：{"code":..,"data":"<JWT>"}；这一登录的同时该行 password 已变成 $argon2id$...
```

- 登录有 **IP+用户名限流**（`rate_limiter`）：口令打错几次会被挡（"账号或密码错误"或限流提示），
  别把它当建号失败——先核对口令再等限流窗口。
- 助手账号同样冒烟一次（用助手的账号名与口令），确认能登录后它的哈希也就升到了 Argon2id。

### 7.4 建完之后要知悉的副作用（都不是 bug）

- `list_temp_users` 过滤 `role = 'user'` ⇒ 管理员与助手**都不出现在后台"临时用户"列表**里。
  后台看不到 ≠ 被删了；这是预期。
- `delete_temp_user` 按 id 删除、**不校验 role**（级联删会话/历史/摘要）⇒ 助手账号若被按 id
  删除，不会被"列表里看不到"挡住。**建号时把 id 记下来**，删除前核对。
- 断言里的 `role` 是 Rust **每请求查库**得到的（不是读 token）⇒ 改了 `role` 立即生效、不必重新登录；
  反过来把 `admin` 降成 `user`，已登录的后台会话**下一次请求**就会被 `auth_guard` 挡下。
- **助手账号的对话侧今天看不出区别**：`AGENT_AUTHZ_ENFORCE` 仍**未设**（20260921 复核，shadow 期
  只记不拦），而 `AGENT_REQUIRE_ASSERTION=1` 已在生产生效（不带断言头直连得 401）。要让它真的
  "多出 / 少掉"能力，得先收 shadow 证据再打开 enforce（P1）。

### 7.5 为什么不做代码（20260921 拍板记录）

- **没有"产生管理员账号"的代码路径可挂**：全库唯一的建号接口是 `src/routes/temp_user.rs` 的
  `create_temp_user`（`POST /api/temp-users`），`role` **写死 `"user"`**、不是请求参数；
  `auth.rs` 只有 `login`/`profile`（**无注册**）。管理员账号一直是直接写库产生的
  （本次角色变更操作就是这一类）。要"自动配助手"就得先补一条建号路径 = 新增一个高危接口，
  而它该有的审计（⑥）还没做——顺序反了。
- **注册账号保持不开放**：`create_temp_user` 的 `role` 写死就是这条的后端一半，前端入口
  由页面侧处理（登录页只给提示、不给注册流程）。
- **不要做的事**：不要让 agent 自己去建账号——那是写操作，且"谁授权"（④/⑥）都还没做。

