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
| ① | **秘书账号与角色落库** | `user.role` 无 ENUM/CHECK，新建一个 `secretary` 角色就是一行 UPDATE/INSERT——**生产库写入，须用户点名「库名+迁移文件」** | 先只建一个体验账号，角色用 `secretary`；不批量改任何现有行 |
| ② | **`AGENT_REQUIRE_ASSERTION` 收口** | ~~断言已落地快照但默认关着~~ **20260920 复核：生产 `.env` 里已是 `1`，实测已生效**——不带 `X-Agent-Assertion` 直连 `/chat` 探针得 **401**（不是静默回退），`/review`、`/graph/query` 不经 `_resolve_principal`，不受影响。代码默认值仍留 `False`（本机/测试环境不必带头） | **无需动作**。回归锁=一条不带头的请求必须 401；将来任何"给 agent 加公网入口"的改动都要先过这条 |
| ③ | **写操作的"人在回路"** | 秘书的价值在写，而今天唯一的防护是"调用前查断连"——没有确认、没有"谁同意了"的记录 | **agent 侧已落地（20260920，见 §3.4）**：需确认的 scope 未获用户本轮明确确认 → 产 `__ERROR__` 帧、**不执行**、叙述侧也说不成"已完成"。**剩下的是 ①（真实秘书账号）与第一个写工具**——写通道（④）与审计（⑥）仍缺 |
| ④ | **agent → Rust 的写通道凭据** | agent 现在一个 admin 接口都不调，"代用户发文章"没有可用的通道：既没有写接口的调用约定，也没有"agent 持用户授权"的凭证语义 | 建议：不要复用用户 JWT 长期有效，而是同一套断言思路——Rust 签发**带 scope 的短时效授权**，写接口按 scope 校验（与 agent 侧的 manifest 同名同义） |
| ⑤ | **前端角色模型** | `AuthRouter.tsx` 客户端解 JWT 硬编码 `'admin'`；Dashboard 侧栏是静态全量列表——"比 admin 窄、比 user 宽"的界面无处安放 | 建议：等 ① 之后再做；先把硬编码换成与后端同名的常量，避免第三处字面量 |
| ⑥ | **审计** | 秘书代表用户做了写操作，事后要能回答"谁、以谁的名义、什么时候、改了什么" | `execution_log` 已有 `skill/detail/created_at`，缺"以谁的名义"（principal）与授权来源；建议在回执顶层加 principal 字段（跨语言契约，同 `digest` 的处理方式） |

**明确不在本轮范围**（防蔓延）：不做秘书的具体功能（日程/发文/审批流）、不动后台路由、
不引入权限表/多角色表、不做前端的秘书界面。

## 6. 分阶段建议

- **P0（已完成）**：身份与范围的地基 + shadow 观测 + 两侧单测与文档（本文件）。
- **P1（待拍板）**：① + ② → 让一个真实账号带角色跑起来，收 shadow 证据 → 校准授予表 →
  打开 `AGENT_AUTHZ_ENFORCE`。此时"秘书能做什么、不能做什么"第一次成为**可执行的事实**。
- **P2（按需）**：③ 人在回路 + ④ 写通道凭据 + ⑥ 审计，一起做（写操作要同时有授权、
  确认与记录，缺一条就等于没做）。
