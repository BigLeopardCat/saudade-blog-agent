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

工具级的 `TOOL_SCOPE`（22 个工具一个不漏，**完备性由 `tests/test_authz.py` 在 CI 层锁死**：
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
- **`write.content` 至今空转**：现有工具里**没有一个是 `write.content`**（`tests/test_authz.py` ⑨
  锁着这条事实）。它等的是第一个"代用户发文"的工具——**新增时不需要改这段代码**，
  声明表里给它 `write.content` 就自动落在闸下。

**20260921 第二个消费者：`write.console`（管理助手写三件，见 §5.2）**。同一个闸门、同一个
判据函数，差分只在**判据表**：`_CONSENT_PATTERNS[write.console]` 与 `write.content` 的
"确认发布"族**分开写**。

- **同意语义（用户 20260921 拍板）= 同轮命令即确认**：管理员说「把《架构文档》设为私密」
  就是命令、也是确认，**不再要第二句"确认"**。所以这个判据回答的是"**本轮有没有明确命令**"，
  **不是**"有没有第二次确认"——措辞与文档都要说准，否则下一个人会以为漏了一层。
- **判据必须是命令式的**（动作词 + 目标词），并**排除疑问/假设/转述**。成对断言锁在
  `tests/test_authz.py` 与 `tests/test_admin_write.py`：`"把文章 12 设为私密"` → 放行；
  `"把文章 12 设为私密会有什么影响？"` / `"如果我把文章 12 设为私密的话"` → **不放行**。
- **fail-closed 的方向 = 判不出来就先追问**（多问一次，不误写）。这一条与 §3.4 顶部那句
  同源：确认闸宁可挡下一次合法写，也不能放行一次没被要求的写。
- **`write.console` 同样进 `_HARD_SCOPES`**（不吃 shadow）：写能力纯新增、没有观测期。
  ⚠️ 只进 `CONSENT_SCOPES` 而**不进** `_HARD_SCOPES` 是本轮最危险的一处——`authz_enforce=False`
  时 `decision.allowed=False` 会直接落到 invoke。两个集合都进，`tests/test_admin_write.py` 用
  `authz_enforce=False` 下的非 admin 断言把这条锁死。
- **叙述侧的第二道网**：同意闸挡的是执行，narrator 还有可能把"没执行"讲成"已经置顶啦"。
  因此新写动词进了 gate 的两族判据（err 帧轮走 5a 的 `_WRITE_CONTENT_CLAIM_RE`，
  零帧轮走 洞① 的 `_STATE_ACTION_CLAIM_RE` ④支），**且两处都必须带疑问豁免**——
  未获确认那轮的正确回复恰好就是一句追问（"需要我现在帮你执行吗？"），少了豁免会把
  设计好的正确答案判成"声称已执行"。教训写进了 `graph.py` 的注释（这两支是**独立备选**、
  各自带完成标记，豁免必须两处都挂）。`_EXECUTION_CLAIM_RE` 刻意**不收**这批动词：
  它只在零帧 `content_query` 宽查，加进去会把合法的疑问句打成声称（实测：三个动词
  在 516 条历史 trace 上的命中差异为 0，这条改动零历史影响）。

**20260921 第三轮：闸判 False 不再等于死路（弹窗），并修掉一个把判据整个架空的壳**。
生产实测暴露了两件事，都是这一段的直接后果：

- **判据看到的是包装过的文本**。`server.py` 给本轮用户消息加了 `[当前问题]: ` 锚点
  （20260901，防"把历史旧问题当当前问题回答"），而 §3.4 这一族判据**全是锚定的**
  （句首 把/将、句首动词、句首假设词）——带着壳一条都命不中。后果实测到两条：
  **教科书式的明确命令**「把文章 12 设为私密」判 False（连"同轮命令即确认"这条快道
  也从未真正生效过），假设句「如果我把文章 12 设为私密」也判不出提问。修法 =
  判据入口先剥系统方括号注记（`authz._strip_system_tags`），`tests/test_authz.py` ⑨f 用
  "带壳与不带壳判定一致 + 剥完仍是对的那个判定"两句锁住（只测透明度的话，
  "两边都判 False"也能过）。**教训**：判据函数有测试 ≠ 判据在真实输入形态上有测试；
  真实输入形态是 server 拼出来的，测试必须按它的拼法喂。
- **判 False 的出口从"再问一轮"改成"点一下"**（§5.3）：非命令措辞的意图 → 弹确认框
  （零 LLM、零执行）；提问/假设照旧走追问。判据本身**没有放宽**——为明确命令补的是
  「确认…」短回声骨架（agent 自己建议、用户照抄的那句），其余一律继续 fail-closed。

**20260923 第三个消费者：`write.own`（用户**自己**的私有数据：收藏文章、标记通知已读）**。
它与前两个的差别不是"更松"，而是**判据的粒度不同**——本轮新增了一条结构性判据：

- **一个 scope 挂多个工具 ⇒ 谓词必须看得到工具名**。前两个 scope 各自只有一个语义方向
  （"发出去" / "改后台"），而 `write.own` 底下是三个动作（加收藏 / 取消收藏 / 标记已读）。
  同意闸是按 scope 查表的，若判据只看消息，用户说「把通知都标记已读」而 planner 填错成
  `add_favorite` 时，**scope 级同意会照样放行**——那是 20260921 golden 抓过的**误靶写**
  在一个本为防它而设的闸门里溜过去。所以 `consent_granted` 对**可调用**的判据多传一个
  参数（工具名），`_own_command(msg, tool)` 据此判"这句是不是在命令**这一个**工具"；
  `_console_command` 收下不用（后台写的靶子由技能模板与目标校验管，那里更硬）。
  回归锁 = `tests/test_authz.py` ⑨g 的"同一句话对不同 own 工具结论相反"。
- **工具 → 动作家族是完备映射**（`_OWN_TOOL_FAMILY`，仿 `TOOL_SCOPE` 的完备性纪律）：
  没登记的工具一律 False，**不用"反正都是 own"兜底**（那正是上面那条要防的）。
- **三档角色都授予、不进 `_HARD_SCOPES`、不进 `_ALWAYS_CONFIRM_TOOLS`**：它写的是调用者
  自己的东西（自己看得见、一键能撤），且"在页面上点收藏"本来就是每个登录用户都能做的事
  ——满足 shadow 的"观测既有流量"前提，没有"纯新增能力"那个理由。写面的守卫落在
  scope + 同意闸 + 工具层写后复核三道，全部确定性。
- **判据刻意比 `write.console` 更窄**（宁可落回弹窗）：陈述句（"我已经把这篇文章收藏了"）、
  名词用法（"帮我看看收藏"——这里的"收藏"是页面名）、量词型打听（"我收藏了哪些文章"）
  一律判非命令；同一句里出现另一族动作（"取消收藏这篇，收藏那篇"）也判不准。

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
| ④ | **agent → Rust 的写通道凭据** | agent 现在一个 admin 接口都不调，"代用户发文章"没有可用的通道：既没有写接口的调用约定，也没有"agent 持用户授权"的凭证语义 | 建议：不要复用用户 JWT 长期有效，而是同一套断言思路——Rust 签发**带 scope 的短时效授权**，写接口按 scope 校验（与 agent 侧的 manifest 同名同义）。**20260921：读的那一半已落地**（管理助手只读三件，见 §5.1）——通道选定为「以发起人身份代调」：agent 用**本轮发起人的 uid** 现签一条 **60 秒** JWT 直连 `127.0.0.1:3000`，Rust **授权侧零改动**（`auth_guard` 本来就按 `claims.sub` 查库判角色，token 里的 role 无权威）。**写的那一半也落地了**（同日第二轮，管理助手写三件，见 §5.2）：同一个 `_admin_post` 通道，写工具另受 scope `write.console` + 同意闸 + 目标校验三道门 |
| ⑤ | **前端角色模型** | `AuthRouter.tsx` 客户端解 JWT 硬编码 `'admin'`；Dashboard 侧栏是静态全量列表——"比 admin 窄、比 user 宽"的界面无处安放 | 建议：等 ① 之后再做；先把硬编码换成与后端同名的常量，避免第三处字面量 |
| ⑥ | **审计** | 秘书代表用户做了写操作，事后要能回答"谁、以谁的名义、什么时候、改了什么" | `execution_log` 已有 `skill/detail/created_at`，缺"以谁的名义"（principal）与授权来源。**20260921 已落地（用户拍板「零迁移：写进 detail」）**：写回执顶层带 `principal_role`/`op`/`before`/`after`，Rust `render_exec_row` 渲染成 `以管理员身份 · 修改文章 12：私密 → 公开` 整行进既有 `detail` 列——**不加列、不做迁移**。⚠️ 只落**角色不落 uid**：`detail` 会进生产库、还会被 `recent_executions` 注入上下文并被 narrator 念出来 |

**明确不在本轮范围**（防蔓延）：不做秘书的具体功能（日程/发文/审批流）、不动后台路由、
不引入权限表/多角色表、不做前端的秘书界面。

### 5.1 管理助手：只读三件（20260921 落地，④ 的读侧）

博主（role=admin）问运维/审核/用户数据时，agent 现在**真的去读后台**，而不是答"我看不到"：

| 能力 | 数据源 | 通道 |
|---|---|---|
| 服务器健康度（CPU/负载/内存/swap/磁盘/开机时长） | agent 本机自采（`/proc`、`shutil.disk_usage`） | **不经后台门**——agent 与 Rust 同机，自己读就行（`agent/hostinfo.py`） |
| 服务健康（三个 systemd 单元 + 心跳日志 + 今日 trace 异常） | `systemctl show`、`logs/health.log`、`logs/agent/traces/` | 同上 |
| 留言审核状况（待审/AI 拦下/交叉表 + 最近明细） | `GET /api/protected/board` | 以发起人身份代调 |
| 用户数据报表（用户数/角色分布/会话消息量/近 7·30 天活跃） | `GET /api/protected/stats/users`（本轮新增，见父仓 `src/routes/stats.rs`） | 以发起人身份代调 |

**三条判据（都在同一个点上，与 §3.3 一致）**：

1. **新 scope `admin.console`**，且**不吃 shadow 开关**（`_HARD_SCOPES`）：shadow 的目的是"观测
   既有流量会不会被拦"，而这批能力是纯新增、没有观测期——shadow 期越权是可被利用的窗口，
   所以硬拦。全局 `AGENT_AUTHZ_ENFORCE` 仍关，那件事要有自己的 shadow 证据。
2. **结构性不可达**：四个工具不进 planner 点名白名单（`_EXPLICIT_TOOLS`/`_CALLABLE_QUERY_TOOLS`），
   非 admin 的 planner 上下文里根本看不到这三个技能（`build_planner_context(role)` 按角色过滤）。
3. **越权的措辞要如实**：403/401 → `unavailable("当前身份无权访问后台数据")`，不返回空、
   不假装成功（空结果会被下游读成"没有待审留言"）。

**数字在工具侧算好**：报表返回的是**渲染好的中文报表文本**，不是原始 JSON 让模型自己数——
LLM 计数是幻觉源。这是对既有 `_shape(data)` 惯例的有意偏离（理由写在 `agent/reports.py` 头注）。

**注入面（写给下一个改的人）**：agent 从此会读**攻击者可控的文本**（待审留言原文会进工具帧）。
本轮全是只读，注入最多导致**答错**、不导致**做错**；工具侧对这类文本做了命令前缀消毒
（`sanitize_untrusted`，插 U+200B 零宽符断开 `EFFECT:`/`AUTO_NAVIGATE:` 的命令形态，两侧
`\s` 都不匹配该字符）。**下一轮做写操作（标签创建/文章状态变更）之前，consent 闸必须先真正
跑通**——它已就位且同样不吃 shadow（§3.4）。

**写操作两件为什么留下**（用户 20260921 拍板「先只读三件」）：写要同时有 ① 授权（谁让我做的）、
② 确认（这一轮他确认了吗）、③ 记录（审计 ⑥），今天只齐了 ①；缺 ②③ 的写通道等于没有防护。

### 5.2 管理助手：写三件（20260921 第二轮，④ 的写侧 + ⑥ 审计）

第一轮把"写"留下的公开理由是"写要同时有 ① 授权 ② 确认 ③ 记录，当时只齐了 ①"。这一轮把三件
补齐——写操作**第一次真的走通了那条同意闸**（§3.4 从"空转"变成"承重"）。

| 能力 | 工具 | scope | 通道 | 关键约束 |
|---|---|---|---|---|
| 读后台文章清单（**含草稿/私密**） | `list_admin_notes` | `admin.console` | `GET /api/protected/notes/list` | 这是草稿/私密文章**唯一的可达读口**——公开 `list_notes`/`search_notes`/详情都硬过滤 `is_public && status!='draft'`，没有它，"把草稿发布出来"这半个能力在 planner 侧永远拿不到 id |
| 建标签（一级/二级） | `create_tag` | `write.console` | `POST /api/protected/tagone` / `tagtwo` | **先查后建**（同名同层 → 复用不写库）+ **建后复核**（按返回 id 读回，名字一致才算成）；颜色按名字哈希，与前端 `NoteTagSelect` 的 `colorForName` 同算法 |
| 改文章状态/置顶 | `set_article_status` | `write.console` | `POST /api/protected/notes/:id` | **只发点名的字段**；绝不发 title/content（会触发 `from_editor` 分支：重定向 + 级联删修改稿）、不发 isPublic（由 status 联动）；已是目标值时**不发请求**（`update_note` 会无条件刷新 `updated_at`，空改动把文章顶到列表最前） |
| 加/去/替换文章标签 | `set_article_tags` | `write.console` | 同上 | `noteTags` 是"传了就写"、`""` = 清空 ⇒ **只有 `replace=[]` 才可能清空**；未点名的标签**永不被顺手摘掉**；标签按名字精确匹配，找不到就如实说（不自动新建） |

**三道门，都在确定性点上**（无一道依赖 LLM 自觉）：

1. **授权**：`authz.required_scope(tool)` = `write.console`，进 `_HARD_SCOPES`（不吃 shadow，
   理由同 §5.1）+ 进 `CONSENT_SCOPES`。`_ROLE_SCOPES` 一行没改：admin 自动含全部 scope，
   **secretary 刻意拿不到**（后台写与 `admin.console` 同域，Rust 那道门也只认 admin）。
2. **确认**：同轮命令即确认（判据见 §3.4）。
3. **目标有据**：写工具的 `article_id` 必须来自 ①本轮某个读类工具帧 ②页面上下文 `/article/<id>`
   ③用户本轮消息里显式点到的数字；否则产 `__ERROR__: 目标未经确认[unknown_target]` 帧 →
   planner 先读再写。**如实定位**：它拦的是"整轮没读过任何东西却写一个凭记忆的 id"，
   **不保证 id 一定对**（后者靠回执回显 + 管理员复核）。

**读写都走 `/api/protected/notes/list`（一条口径，不是随手选的）**：`/api/protected/draft/editor/:id`
对**修改稿**行会解引用成原文章，而 `update_note` 只发 `{status,isTop}` 时 `from_editor=False`、
**写的是被点的那行本身**——读的行与写的行不是同一行，前值/回显全假；更坏的是把修改稿行写成
`status=public` 后，公开列表（只滤 `is_public`/`draft`，**不滤 `draft_of`**）会多出一篇同标题文章。
`/notes/list` 过滤 `draft_of is null` ⇒ 修改稿在这里**读不到** ⇒ 写工具**如实拒绝**
（"这不是一篇文章本体"），而不是猜。

**一条硬纪律：一切"没做成"都必须 `unavailable()`**。checker 对**非空文本**一律判 PASS，而 PASS
会被记成**系统确认事实**落进回执 → `execution_log` → 下轮注入 narrator——`ok("创建失败…")` 会被
下一轮的自己念成"已创建"。反过来 `empty("")` 会被判 `empty_result` 而 BLOCK ⇒ **"零写成功"的返回
也不能是空串**（如"标签已存在，复用 id=13"走的是 `ok`）。另：Rust 的 `ApiResponse::error` 是
**HTTP 200 + code 500**，所以 `_admin_post` 必须看业务码，只判状态码会把"创建失败"读成成功。

**执行去重是 args-aware 的**：既有收尾判据只比工具名，对写不够——planner 第二轮补做"另一篇"
（同一工具名）会被静默收尾，而 narrator 手握第一条真回执必然说成"都改好了"。新增
`_EXECUTED_ONCE_SKILLS`（不改 `SNAPSHOT_SKILLS` 的语义与断言），判据改成 **`(tool, args)` 整体**，
并给写技能一条**独立收尾文案**（不能复用"快照型只读、重复调用拿回同一份数据"）。
`_CONTENT_TOOLS` **只加 `list_admin_notes`**：那个集合的语义是"跑过 ⇒ 检索/读取声称有据"，
塞写工具会让"建了个标签"变成"我检索过"的证据。

**记录（⑥，零迁移）**：写回执顶层带 `principal_role`/`op`/`before`/`after`（跨语言契约键，
Python 写 / Rust 读，`src/routes/chat.rs::render_exec_row` 四个新臂），渲染成
`以管理员身份 · 修改文章 12：私密 → 公开` 整行进既有 `detail` 列——**不加列、不做迁移**。
写行**刻意不带《标题》**（回执会经 `recent_executions` 注入下一轮，带《标题》会被读成
"我读过这篇"的指代证据）。`actor_prefix` 取不到角色时返回**空串**而不是"访客"：
写操作从不由访客发起，把管理员的操作标成访客是伪造审计记录。

**验证（三件套，口径不同）**：
- `tests/test_admin_write.py`（秒级、零网络、**进 CI**）= 本轮回归主力：假 httpx 验 `_admin_post`
  的 `uid<=0` 不发请求 / 401 / 403 / HTTP 200+code 500 一律 unavailable；假工具 + 假 principal
  直接驱动 `execute_node`，验三道门（疑问句/假设句 → 零调用 + `consent_required`；
  非 admin → `denied`；**`authz_enforce=False` 下非 admin 也必须被硬拦**；目标无据 →
  `unknown_target`；非法参数不发请求）；checker 三态；去重 args-aware；`color_for_name` 与
  前端对拍。
- **golden 只加"不写"的三条**（真写用例会改生产库，一律不进 golden）：`admin_write_question_no_exec`
  （问影响 → 零写 + 无完成式声称 + 真有影响说明）、`admin_write_denied_user`（访客下写命令 →
  零写 + 如实无权）、`admin_write_no_identity_honest`（uid=0 打不存在的 id → 不许声称成功）。
  三条都带 `forbid_fallback` + 负向正则族——**正向一律用形态正则族而不是词表**（实测：
  "没有权限"这种连续串命中不了"没有修改文章权限"，词表追不上措辞）。
- `eval/probe_admin_write.py`（**不进 CI**，需要真实管理员 uid）：默认只跑零真写的三步
  （非管理员写指令 / 管理员疑问句 / 打不存在的 id），真写（草稿置顶来回、标签加减、
  经生产入口真写一轮 + 跨轮复述）需显式 `--allow-write`，删临时标签还需 `--allow-tag-delete`；
  **断言读后端真值**（探针自己现签 JWT 直查 `/api/protected/*`），不看工具返回值。

**诚实备注（缺口）**：

① 上面那三条 golden 与探针的安全步在 `uid=0` 下结构上安全（`uid<=0` 守卫 ⇒ 请求走不出
进程），但**"管理员 200 路径"要等真实管理员 uid 才算验过**（同 §5.1）。

② 探针的 `--allow-tag-delete` 会触发 `DELETE /api/protected/tag` 里**全表**
`prune_note_tags`（清理 `note.tags` 悬空引用），**不可回滚、与探针本身无关**——默认不跑，
跑前披露。

③ **残余波动：`admin_write_no_identity_honest` 约每 6 次有 1 次走 gate 打回**（`forbid_fallback`
如实把它记成 FAIL）。复核：narrator 写了「本轮没有执行任何工具」，而本轮**确实执行过**
公开读工具（`receipts` 非空）⇒ 命中 20260920 落地的洞③（假阴性声称），gate 换成兜底文本。
**判据没坏、是模型措辞的波动**，且**不在回归组**（按通过率计）。兜底文本本身如实（"其实
执行过工具、只是返回是空的"），但**偏题**——用户问的是"把文章 999999 设为私密"，回复却在
问"要不要换一组关键词再查一遍"。**20260921 第三轮已收口**：兜底文案按**原因码**分
（`_FALLBACK_CONSENT` / `_FALLBACK_UNKNOWN_TARGET`，gate 5a 用 `authz.consent_error_reason`
与 `adminops.target_error_reason` 取回），不再套用通用的检索话术。

④ 复核一条**作废的旧读数**：早先一轮（判据还是**词表阳性**时）测得"访客写命令只有六到七成
给出明确拒绝"，看上去像"拒答不稳"。改用形态正则族后复跑 6 次（golden `admin_write_denied_user`）
+ 探针非管理员三问 6 次，**全部干净拒绝、零写工具**——那条读数的绝大部分是**判据侧问题**
（"没有权限"这种连续串命中不了"没有修改文章权限"），不是模型行为。教训与 §5.2 开头一致：
正向断言要用**形态正则族**，词表永远追不上措辞。

### 5.3 写操作的确认弹窗：一次点击代替一轮对话（20260921 第三轮）

**用户实测的原话**：「实测根本不行，还有确认机制非常有问题，需要再执行一次对话浪费 token
而且会被认为是再次请求吧……需要操作授权或者二次确认或其他方案等用户输入时，弹出类似于
泠月喵建议去：xxx 同类型的窗口，还有 agent 对话窗口加上颜色预览」。三条缺陷：① §3.4 的
判据认不出人话（连 agent 自己建议的「确认创建标签 X」都判 False ⇒ 死路）；② 唯一的确认
通道是**再发一条消息**（烧一轮 planner+narrator，且在前端呈现为一条新请求）；③ 颜色参数
根本不存在（说了"粉色"也进不了链路，颜色由名字哈希决定）。

用户拍板四条：**① 弹窗点确定 = 隐藏确认请求 + 跳过 planner；② 文字命令快速路径留，但只认
明确命令；③ 颜色 = 站内 8 色板 + 中文色名映射；④ 弹窗 = 通用协议，先只接写操作。**

#### 协议：`__CONFIRM__:<json>` 帧 + HMAC 待办令牌

帧体是**通用形状**（本轮只用第一种）：`{"id","q","opts":[{"label","value","kind"}...],"token"}`。
转发路径与 `__PROCESS__` 同族——Python 侧走 `event_stream` 的 JSON 编码分支，Rust 侧
（`chat.rs`）**只转发、不累积进 reply、不落库**。⚠️ 这是必须三端同步的强约束：漏了 Rust
那条分支，帧体（含令牌）会被拼进 assistant 回复并持久化——用户看到一坨 JSON，令牌还会
进入下一轮上下文。

令牌（`agent/confirm.py`）是**无状态的 HMAC 签名串**，不是内存里的待办表：uvicorn 跑
**2 个 worker**，内存表在另一个 worker 上不存在；落库要迁移。payload = `{v,uid,conv,exp,
skill,specs}`，`base64url(json).hmac_sha256(jwt_secret, _DOMAIN + body)`，TTL **600 秒**，
`_DOMAIN = b"saudade-confirm-v1"`。`verify()` 是 **fail-closed**：签名/版本/uid/会话（含
"签发时有会话、现在是 None"）/过期任一不符 → `None`；密钥空缺时**既不签也不验**（绝不降级
成"无签名令牌"）。**`specs` 里不许残留 `$ref`**（引用依赖签发那一轮的工具帧，执行轮早已
不在）——检出即不签发、不弹窗，退回如实追问。令牌**不落 trace、不进日志、不进回执**。

#### 三条路径（判据都在确定性点上，无一道靠模型自觉）

```
execute 遇到写 spec 卡在同意闸上
  ├─ 这句是提问/假设（authz.is_question_like）→ 照旧：__ERROR__ 帧 + planner 追问（绝不弹窗）
  ├─ 已是明确命令（同轮命令即确认）        → 照旧：直接执行（快道，不弹窗）
  └─ 有意向、只是没判成命令                → 弹窗：pending_confirm + confirm_text，
                                             route_after_execute → END（**零 LLM、零执行**）
                                             用户点「确定」→ 隐藏确认请求 → 跳过 planner 执行签名里的动作
```

- **弹窗轮零 LLM 且路由到 END**：叙述层的 LLM 结构上不参与，所以"已经建好啦"这类谎称
  在这一轮**不可能发生**（文案是 `adminops.render_confirm_text` 的确定性中文）。
- **点确定之后**：前端发一条**隐藏确认请求**（`confirm_token` + `conversation_id`）。Rust 见
  `confirm_token` 就**不落用户消息**（历史里不留空 user 行——否则占掉注入窗口、干扰标题
  派生），但仍照常落 assistant 回复与执行回执（那是真发生过的执行）。agent 侧验签失败 →
  **零执行** + 如实说"确认已过期"。
- **planner 被跳过**：`planner_node` 最前面的确定性短路径用令牌里的 `skill` + `specs` 直接
  拼计划（技能名取自签名、**不猜**；参数一字不改；技能名与工具对不上 → 整单拒绝，防令牌
  被换工具）。execute 的两道确定性门（同意闸 / 目标有据）对确认轮**放行**——"用户点的确认"
  本身就是凭据，而"有据"已在签发时校验；**授权不放行**：非 admin 即便持有有效令牌，
  仍被 `_HARD_SCOPES` 硬拦（`tests/test_confirm.py` ⑤ 锁着）。
- **弹窗只弹该弹的**：`_confirm_popup` 要求"授权过 ∧ 未判成命令 ∧ 非提问 ∧ 参数能实例化 ∧
  无 `$ref` ∧ 文章写有目标有据"，任一不满足都不弹（退回既有链路）。**目标无据不弹**尤其
  重要：弹出来的是"要不要改文章 12"，而 12 是编的——确认框会把一个幻觉洗成一条已授权的写。
- **取消不发请求**：点「取消」只关窗 + 在气泡末尾追一行灰字，令牌自然过期（零 token、
  零副作用、无服务端往返）。

#### 颜色：站内 8 色板 + 中文色名映射（三处同源）

`_COLOR_CANON` 的中文名与 `NEW_TAG_COLORS` 同源同序（蓝/绿/橙/粉/紫/青/红/黄绿 →
`#1677ff/#52c41a/#fa8c16/#eb2f96/#722ed1/#13c2c2/#f5222d/#a0d911`），前端
`NoteTagSelect/index.tsx`、`chatMarkdown.ts::chatColorPalette` 与 agent 三处必须一起改。
`match_tag_color` **刻意不做子串/模糊匹配**（"天蓝/浅蓝"含"蓝"→ 会被换成一个用户没说的
颜色，而界面上看不出来），认不出就**不认**；`resolve_tag_color` 在"没说颜色"时回落到
`color_for_name`（既有"同名同色"契约一字不变）。工具层第二道 fail-closed：用户点了名但
认不出的颜色 → `unavailable`，**绝不静默换成哈希色**。回程渲染成「粉色（#eb2f96）」，
前端按色板白名单把色值画成色块（色板外不装饰）。narrator 纪律 17 要求提到颜色必须同时给
中文色名 + 色值（色块由前端画，不许自己画符号）。

#### 验证与**诚实缺口**

- 离线（进 CI）：`tests/test_confirm.py`（令牌往返/篡改/换 uid/换会话/过期/空密钥/`$ref` 拒签 +
  弹窗触发矩阵 + 点确定后的执行轮 + 颜色表）、`tests/test_authz.py` ⑨d/⑨f、`judge_offline_test.py`
  新增帧级判据的 fixture。
- golden（**点不了按钮**）：`admin_write_natural_confirm_popup` 只能验"该弹窗时弹了窗、
  且什么都没写"——帧级断言 `require_frame_prefix: ["__CONFIRM__:"]` + 零写工具 + 文本里
  色名与色值齐全 + `forbid_fallback`；另两条既有写用例补 `forbid_frame_prefix`（问句/非
  admin/目标不存在时**不许弹窗**）。
- 活体（`eval/probe_admin_write.py` ⑧⑨⑩，**需真实管理员 uid**）：非命令措辞 → 确认帧
  （**20260922 探针侧盲区已修**：⑧⑨⑩ 的判据从"整名匹配 / token 子串"改成
  **请求前快照、请求后差分**——新出现的行就是这一腿造的，无论它叫什么名字；腿⑤ 的"复原"
  从"循环跑完"改成"按库真值核对"。此前⑩ 曾因 planner 把下划线当 markdown 强调剥掉而
  假 FAIL、⑭ 的兜底清理也认不出。**修的是探针自身，不是被测系统**）
  → 带令牌的隐藏确认请求 → **库真值**变了 → 明确命令复原（顺带验快道不弹窗）；篡改/过期
  令牌 → 必拒且零写；经弹窗确认建带颜色的标签 → 库真值颜色 = 点名的色值。
  **20260922 已真跑并全绿**：授权后首跑（⑧⑨⑩⑯ 第一次真正走到该走的路）抓出 5 项不符，
  归因 = 探针自身 2 处（`_tampered` 篡改末位 1/16 无效 + ⑨ 子腿基线级联）+ 断言过窄 1 处
  （⑮ 原因词表缺「无法」）+ **被测系统 1 处真回归**（探针腿⑭：`_name_arg_fix` 把 planner
  写对的新名字覆写成目标自己，见 `问题记录.md` 1.41）。三处探针侧与那一处回归修完后复跑
  **①–⑯ 全部符合预期、警告 1 条**（警告 = 删分类那句措辞没走命令快道而弹了窗、点确定走完
  ——同意闸词表缺词，fail-closed 方向，不改判据）。
- **令牌是 bearer 凭据**：拿到帧就等于拿到一次已授权的写。防线是"帧只发给发起它的那个
  会话"（uid + `conversation_id` 绑定）+ 10 分钟 TTL + 服务端零状态。**它防的是被诱导的
  越权写，不防"本地能读到自己 SSE 的人"**——那个人的身份本来就是令牌的 uid。
- **弹窗是通用协议、今天只接了写确认**：将来接别的用途（例如"要不要重新检索"）不用改帧格式，
  但**每接一种就必须想清楚它的令牌能授权什么**（令牌里带着 `specs`，执行轮照它执行）。

### 5.4 管理助手：标签/分类写六件（20260922，写侧的第二次扩容）

§5.2 的三件只覆盖"文章状态/文章标签/建标签"。用户实测「把已有标签 Asyncio 改成编程的子标签」
六轮全错——**改标签、删标签、碰分类在系统里根本不存在**（narrator 建议的"先删再建"也不存在，
而且删了重建会把标签从所有文章上摘掉、不可回滚）。本轮把写面补全：

| 能力 | 工具 | 通道 | 关键约束 |
|---|---|---|---|
| 改标签（改名/改色/换父级/换层级） | `update_tag` | `PUT /tagone|tagtwo/:id`（仅改名改色）/ **`POST /api/protected/tag/move`**（动了层级或父级） | PUT 的 title+color **都是必填** ⇒ 只改名时把**当前色**从索引原样回传，**不猜** |
| 删标签 | `delete_tag` | `DELETE /api/protected/tag` | **任何标签都能删**（用户拍板），代价写进确认卡：会从 N 篇文章上摘掉引用 + 子标签连坐；`prune_note_tags` **不可回滚** |
| 建/改/删分类 | `create_category` / `update_category` / `delete_category` | `POST /api/protected/category`、`POST …/category/:id`、`DELETE …/category` | 建分类**不回 id**（复核靠重拉列表按名找）；改分类**空串 = 不改**；删分类 `ON DELETE SET NULL`（文章变成没有分类） |

**这一轮真正的机制改动是"名字通道"**（用户拍板 ②）：写技能参数一律写**名字**——人嘴里说的就是
名字，跨轮执行记忆里也只有名字（回执行摘要**刻意不带 id**）；工具在 execute 阶段对着**实时标签
字典**解析成 id。解不出就**响亮零写**（唯一命中才动手 / 歧义追问并列出候选 / 查无此名如实说），
**绝不猜、绝不顺手新建**。副作用是好的：planner 侧再没有"必须知道 id 才能写"的动作，
技能描述里 `$list_tags[N].tagKey` 那种写轮永远满足不了的引用示例被删掉。

**同轮修的另一个洞（①）**：`refs.resolve_args` 改成**递归**（嵌套引用此前既解析不出、
又会让弹窗拒绝签发令牌），且写技能分支的引用**原样透传**给 execute 报错误码——
旧行为是把 `"$list_tags[3].tagKey"` 静默变成 `None`，注记还肯定地写下「（一级标签）」。

**审计口径不变**（同 §5.2 ⑥）：回执行仍走 `execution_log.detail` 的渲染定稿，新 op 在父仓
`render_exec_row` 补分支（`修改标签「X」` / `删除标签「X」` / `新建分类「X」` …），零迁移。

**验证**：`tests/test_tag_admin.py`（离线、进 CI）+ golden 4 条零真写用例 + 探针腿 ⑪–⑮（真写，全部复原；
**统一读到连接关闭才算干净收尾**——20260921 那次"库改了、回执落了、前端只看到一行报错"就是
旧盲区放过去的）。**诚实备注**：① 腿⑭ 建分类时 planner 会把名字里的**首尾下划线当 markdown 强调
吃掉**（trace 实证：说 `_探针分类_0922…`、传 `title="探针分类_0922…"`）⇒ 探针改成按 token 认人、
不当 FAIL（功能本身正常，纯粹是模型转写名字）；② 腿⑮ 曾发现**目标解不出来时仍会弹确认框**
（卡片诚实、零写安全，但用户点确定只会拿到一句拒绝）——**20260922 用户拍板后已改**，见下。

**20260922 收口（用户拍板后落地，`16e2e4d`）**：

- **② 不弹窗了**：planner 决策后先做一次**按名字的目标预演**（`graph._write_target_refusal`，
  与工具**同一套解析器**、`level=None` 更宽松 ⇒ 它拦下的一定是工具也会失败的），解不出即
  **零工具 + 确定性如实收尾**（写明原因、候选名单、该补什么，并明说"一个字节都没有改动"）。
  字典读不到时不拦（那是"读不到"不是"没有"，仍走既有的弹窗路径）。
  ⚠️ 这个分支**必须 `return` 而不是 `break`**：收尾路径要读只有 `instantiate_plan` 才会产出的
  `params` 键，`break` 过去必抛 `KeyError`——20260922 实测正是它把这一轮打成 `__ERROR__`
  （与 20260921 的 `KeyError('model')` 同一类：**分支走通了、收尾路径没走通**）。已补假 LLM
  整轮回归锁（`test_skills.test_write_target_refusal_round`）。
- **快道动词表补「改名 / 改名叫」**：腿⑭ 实测「把分类「X」改名叫「Y」」这句教科书式命令
  **命不中快道**（表里只有「改名为」「改成」）⇒ 落进弹窗那条路，用户明明下的是命令却被多问一次。
- **腿⑮ 由 WARN 升为 FAIL**：断言从"没有弹窗"扩到"逐标签都要给出具体原因"；腿③ 的找不到措辞
  补「查不到」（判据假失败当轮修）。
- **仍待拍板**：`删除标签/删除分类` 的措辞（删掉/删除）**不在**快道动词表里，于是也走弹窗。
  **倾向保持现状**——删除是这套写面里唯一不可回滚的动作（`prune_note_tags`），
  多问一次是想要的 fail-closed 取向；改与不改都要用户点头。

### 5.5 管理助手：公告三件（20260922，写侧的第三次扩容）

代发 / 修改 / 删除**站内公告**（`create_announcement` / `update_announcement` /
`delete_announcement`，scope `write.console`）。这是写面里唯一**对全体访客可见**的动作
（公告挂在首页），所以它比 §5.2/§5.4 那批多一道**结构性限制**：

| 约束 | 做法 | 为什么 |
|---|---|---|
| **同意快道永久关闭** | `agent/authz.py::_ALWAYS_CONFIRM_TOOLS`（三件）在 `consent_granted` 里被**第一顺位**短路 ⇒ 永不判"同轮命令即确认" | 「发个公告说今晚维护」是教科书式命令，快道会给它免问；而对全站说的话，多问一次正是想要的取向。**收窄只落在这三件上**（`test_authz` 锁：同一句话对别的后台写仍判命令） |
| **正文只准原样落库** | 技能描述写明"他给几个字就写几个字"，缺 content 就追问、**不许替他润色或编一句凑上** | 编出来的公告是**以主人名义对全体访客说的假话**，比一次失败的写更贵 |
| **身份只有标题** | 工具按标题在实时公告表里定位（`_find_named_announcement`），查无此名即零写 + 如实说 | 公告没有稳定 id 指称，用户从来只说标题；与 §5.4 的"名字通道"同一取向 |

**三个端点的既有事实（已写进 `docs/agent-architecture.md` §5.4）**：建公告**不回 id**（复核靠重拉
列表按标题找）；改公告的 `PUT` **title+content 都是必填**（只改正文要把当前标题原样回传）；
删公告传一个不存在的 id **静默无操作**（所以删除必须自己按标题定位、并把"没删到"响亮报出来）。

**验证**：离线锁 `test_authz`（快道关闭 + 收窄不外溢）+ 活体探针腿⑯（建 → 只改正文 → 删，
三步都按**后端真值**断言，`finally` 兜底删除，不留残留）。

### 5.6 管理助手：留言复核与删除（20260922，写侧的第四次扩容）

人工复核（驳回/隐藏 · 通过/放行）与删除河灯留言（`audit_board_comment` / `delete_board_comment`，
scope `write.console`，技能 `board_audit` / `board_delete`）。这一族的靶子**不是主人的东西**——
是**访客写下的内容**，所以判据与取向都比前几族更紧：

| 约束 | 做法 | 为什么 |
|---|---|---|
| **身份只有正文片段** | 唯一子串命中才动手；撞车（两条都含这段）列出候选并零写；查无此句如实说没有 | 留言没有标题也没有名字，用户从来只念一段原文（同 §5.4 的"名字通道"取向） |
| **片段先校正、再预检** | 主人引号里那段就是身份（`_board_quote_fix`，见 `docs/agent-architecture.md` §6.6） | planner 的转写会截短（「好笨」）、会填成主人给的**理由**（「有点乱」），甚至把技能描述抄成参数值 |
| **删留言进 `_ALWAYS_CONFIRM_TOOLS`** | 与公告三件同款：结构性关闭"同轮命令即确认" | 动的是访客的东西，且删了没有回收站 |
| **审核不进** | 走既有的"命令式措辞才免问"（判不出来照旧弹窗） | 可改判（驳回的能再放行）、改的只是可见性，与 `set_article_status` 同类 |
| 请求体 **0/1** vs 落库值 **1/2** | 两张表**分开** | 发 `2` 会被端点读成「通过」——方向正好相反 |
| 删除对不存在的 id **静默 no-op** | 删后读不回 = 没删掉 ⇒ 响亮报 `unavailable` | 同 §5.5 的公告删除 |

**验证**：离线 `test_admin_write` ⑰（身份地基 + 弹窗回落）+ `test_skills`（片段校正三态、台账
豁免锚）；golden 四条（复核弹窗 / 删除弹窗 / 疑问句不弹窗 / 目标落不到唯一一行如实收尾，末一条
需真管理员 uid）；活体探针腿⑧⑨⑩（弹窗链路）**20260922 已真跑并全绿**（详见 §5.3 末条）。
阻塞点不在代码：复跑要真写（一次性标签/分类/公告 + 指定草稿文章的置顶往返），其中 `--allow-tag-delete`
会触发**不可回滚**的全表 `prune_note_tags`，属生产写操作，必须由主人逐字授权后才能跑。
20260922 探针侧的两处盲区（见 §5.5 上方）已在离线修好，重跑时 ⑧⑨⑩ 才第一次真正走到该走的路。

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
- **P1.5（20260921 已落地，用户点名「先只读三件」）**：④ 的**读侧**——管理助手（运维报表 /
  审核状况 / 用户报表）。通道、三道判据与注入面见 §5.1；Rust 只加了一个只读端点
  （`src/routes/stats.rs`），授权逻辑一行没动。
- **P1.6（20260921 第二轮，已落地）**：③ 人在回路 + ④ **写侧**凭据 + ⑥ 审计，三件一起做
  （写操作要同时有授权、确认与记录，缺一条就等于没做）——管理助手写三件，见 §5.2。
  同意闸由此**第一次真正承重**。
- **P2（按需，剩下的都是"下一次"）**：⑤ 前端角色模型（`AuthRouter.tsx` 硬编码 `'admin'`，
  等秘书界面真要做时一起改）；收 shadow 证据 → 打开 `AGENT_AUTHZ_ENFORCE`；访客写命令的
  确定性拒绝（§5.2 缺口③，待拍板）；删除类写操作（删标签/删文章/删留言）与发文/改正文
  （`from_editor` 那条路）**本轮明确不做**。

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

