# 平台面建设与整体规范化路线图
> ⚠️ 20261018 又一批模块已改名（**记录型文档一律保留当时的名字，不回溯改写**）：`agent/absence.py` → `agent/absence_claims.py`、`agent/frames.py` → `agent/frame_contract.py`、`agent/probe.py` → `agent/runtime_probe.py`、`agent/decisions.py` → `agent/deterministic_plan.py`、`agent/entities.py` → `agent/entity_digest.py`、`agent/refs.py` → `agent/arg_refs.py`、`agent/workflow_defs/nav.py` → `agent/workflow_defs/nav_map.py`、`agent/workflow_defs/navigation.py` → `agent/workflow_defs/nav_skills.py`、`agent/workflow_defs/reports.py` → `agent/workflow_defs/report_skills.py`、`agent/context.py` → `agent/prompt_context.py`。**现状地图（每个模块一行）**见 `docs/agent-architecture.md` §2。

**类型：方向型**（还没做的分批预案）。状态与触发条件的**总入口**仍是仓根
[ROADMAP.md](../ROADMAP.md)——这份文档是它在「平台面 / 规范化」这一支上的展开，
**不重复那份表里的状态**。

**这份文档回答一个问题**：一个 agent 系统该有的**外围**能力（断点恢复、可观测性、
错误与用量、权限强制、提交与静态检查闸），这个仓**现在有哪些、缺哪些、按什么顺序补**。

**不回答什么**：核心链路（planner → execute → narrator → gate）的取舍。那些在
[agent-architecture.md](agent-architecture.md) 与 [adr/](adr/README.md)。

**为什么另起一份而不是并进 ROADMAP.md**：那份是**状态表**（现在做什么 / 等什么条件 /
明确不做），一屏读完；这份是**分批工作清单**，随执行逐批划掉。读者也不同——那份回答
「你现在该知道什么」，这份回答「接下来按什么顺序动」。

---

## 1. 现状：核心链路已经硬，缺的是平台面

**下表是 20261018 建账时的实测读数，不是实时状态**——每批落地后那一行就地标上「已补」
（保留原文，好让下一个人看得见它当时到底缺成什么样）。分批进度看 §2 的批标题。

这个仓的核心链路有受限规划、回执驱动、gate 声称检查、技能注册表、跨轮执行记忆、
权限 scope 表、同意闸、写幂等账、trace 与 L0–L3 评测分层，外加一整套机械闸
（4 条预算棘轮 + 十来个接线/一致性闸，都带正控/反控）。**缺的不是业务能力。**

| 面 | 现状 | 证据 |
|---|---|---|
| 断点恢复 | 建账时**无**：生产不挂 saver；`agent/checkpoint.py` 的 `build_checkpointer()` 是响亮桩、零生产调用方；`thread_id` 每请求随机。**"挂没挂"现在看得见了**（**已补**：批 6a，`/health` 的 `checkpoint_backend`；半接会被 `tests/test_checkpoint_adoption.py` 判红）。**接线已落地**（**已补**：批 7，"三处一起动"共用同一个判据）——**但生产是否在跑仍取决于 `CHECKPOINT_DSN`**，空则与从前逐字节相同 | `agent/checkpoint.py`、`server.py` |
| 幂等账 | **已建成但惰性**：`agent/idem.py` 的 `active()` 要求 checkpointer 在场 ⇒ 恒 False | `agent/idem.py` |
| 版本可见性 | **无** `/version`、无 build-info；`/health` 只回声 dials （**已补**：批 3，`agent/probe.version_payload`） | `server.py`、`agent/probe.py` |
| 错误码 | BLOCK 原因码**已有闭集**（`agent/block_reasons.py` + `tests/test_block_reasons.py`）；**发射侧**没有对应闭集 | 同上 |
| 重试 | **已有**但**散在**：重试码集、重试异常集、`_llm_retriable()`、planner 里的一次性重试各住一处 | `agent/graph.py` |
| 用量计量 | 主链路计；**侧任务（moderator / summarizer）不计** | `agent/llm_usage.py` 头注自认 |
| 权限 | scope 表与同意闸齐备，但 `AUTHZ_ENFORCE` 默认 **0（影子）**；影子读数**已建成**（批 5） | `agent/authz.py`、`eval/authz_shadow_divergence.py` |
| 静态检查闸 | **文档自称「待建」**：ruff 基线还只是快照，没有执行者 （**已补**：批 2，`eval/lint_gate.py` + CI 一步） | [lint-baseline.md](lint-baseline.md) |
| 提交闸 | **本仓没有** commit-msg hook，规则只写在 [CONTRIBUTING.md](../CONTRIBUTING.md) §3 （**已补**：批 2，`.githooks/commit-msg` + CI 一步） | 实测 `.githooks/` 不存在 |
| 文档计数 | [docs/README.md](README.md) 里手抄的 ADR 份数漂了（写 7、实际 12），且这条计数没有闸 （**已补**：批 1，进 `tests/test_docs_facts.py` 同源判） | 实测 `docs/adr/` |
| 台账幽灵键 | 已删模块的键还留在类型棘轮台账里（**已补**：批 1/2，两处棘轮各加一条幽灵键守卫） | `eval/mypy_baseline.json`、`eval/lint_gate.py` |

---

## 2. 分批

每批 = 一个可独立提交、可独立回退的单元。每项 = **能用 + 一条判据**，不追求生产级加厚。

### 批 0 — 预案落仓（只动文档）✅ 已落地

- 本文档；
- [docs/README.md](README.md) 索引加一行（索引与磁盘**双向判**，漏列就红）＋ 顺手修 ADR 份数；
- [ROADMAP.md](../ROADMAP.md) 加一条指针（它仍是「现在该做什么」的唯一事实源，**不在这里复述分批表**）；
- `CHANGELOG.md` 一行。

### 批 1 — 收口与自净（前置）✅ 已落地

1. **收口在途的 skill → workflow 重构**，逐文件点名提交。
2. **台账收紧**（文本改那一个数，**不跑 `--update`**——那是放宽通道，且会重排整份）：
   `eval/module_baseline.json` 的 `agent/graph.py`；`eval/mypy_baseline.json` 删幽灵键。
3. **补两处「闸的漏洞」**——「台账松了没人管」的本体：
   - `tests/test_module_budget.py` 现在只在「实测 > 台账」时红，**台账比实测松是静默容忍的**；
   - `eval/type_gate.py` 只遍历报出来的文件，**幽灵键不可见**。
   两处各加一条判据，各带一条反控（把台账改高 / 留着幽灵键 ⇒ 必须红）。
4. **文档漂移收口**：ADR 份数进 `tests/test_docs_facts.py`（与工具数/技能数/用例数同族）；
   指向**不在本仓**的路径逐条标注。
5. 清掉已删模块残留的字节码缓存（运维动作，不进提交）。

### 批 2 — 补闸 ✅ 已落地

- **静态检查棘轮**（待建）：`eval/lint_gate.py` —— ruff E/F/I/W 按文件记账、`--update` 唯一放宽通道、
  自检带正控反控。建完把 [lint-baseline.md](lint-baseline.md) 的「待建」改掉，**同一个提交**
  （否则那句成了谎，文档判据 ④ 会红）。
- **本仓提交闸**（待建）：把 [CONTRIBUTING.md](../CONTRIBUTING.md) §3 已经写死的规则
  （八个 type / 首行 ≤60 字符 / 无署名尾注）做成 `.githooks/commit-msg`，规则文本**单一事实源**。
- 判据：提交闸在 `tests/test_commit_msg_hook.py`（hook 存在/可执行、坏消息要拒、好消息要收、
  八个 type 与 §3 那份文本同源）；**静态检查棘轮没有单独的套件**——它的正控/反控住在
  `eval/lint_gate.py::_self_check()` 里（跑闸就跑它），CI 的 `eval.yml` 有一步真执行它，
  接线由 `tests/test_packaging.py` ⑦ 盯着。此前这里写过的那个棘轮套件（**未建**、
  也**不需要**建）别照着找——判据就在闸自己身上。
- ⚠️ 接线陷阱：CI 要调 `.githooks/commit-msg`，**不要**直接调那个套件文件——
  `tests/test_ci_suite_list.py` 判的是「套件清单只有 `tests/run_all.py` 一处」。
- **穷举量那一层补上反方向（20261018，批 2 的补网）** ✅：`eval/wordlist_gate.py` 的
  穷举量棘轮此前**只判"变大"**——而只判变大的棘轮会留下一笔**永久免费额度**。实测抓到过：
  `tools/base.py` 账上写着 132、盘上其实早就是 125，那 7 条是历史上缩小之后没重切留下的
  ⇒ 下一批新增只要没回到 132 就**一格都不报**（"改完还是绿的"与"没改"同形）。
  补两条：`_shrunk_files`（账比现场松 ⇒ 红，"台账该收紧"）与 `_stale_metric_keys`
  （账上有、扫描里没有的文件键 ⇒ 红，"台账该销账"——键留着就是一笔等着被领的额度）。
  与 `tests/test_module_budget.py` 的双向棘轮、`eval/type_gate.py` / `eval/lint_gate.py`
  的幽灵键守卫**同一族**，四处一起补齐的。判据 `tests/test_wordlist_gate.py` ⑫：
  正控 + 三条反控（放松一格 ⇒ 红；塞一个已删文件的键 ⇒ 红；边界——新文件不归这两条管、
  相等不报）+ 接线锁。真数据反控实测：把真账里 `tools/base.py` 放松三格 ⇒ 当场红。
  ⚠️ `eval/wordlist_gate.py` 现在 **792 行**（预算 800）——下一批要往它加东西之前先按主题拆。
- **待补（20261018 实测撞上，尚未做）——跨仓取值取不到时不许报成"内容不符"**：
  `tests/test_write_param_words.py` ③ 从父仓 `src/routes/notes.rs` 的
  `create_note` / `update_note` 里按 `== "x"` / `unwrap_or("x")` 抽状态词表。父仓
  **工作区里在途的那次重构**把词表收进了 `normalize_status_word` / `public_status_words`，
  于是抽取结果变成 `[]`——而它**报成了六条内容不符**（"本侧产出的每一个状态词后端都认"
  之类），读的人会以为是本仓多产出了一个词。实测对照：父仓**已提交的 HEAD** 上抽取正常
  （`['draft','private','published']` 等），工作区才抽不到 ⇒ 这不是本仓的回归。
  按本仓既有口径，**前提取不到应当是"退 3（账不可用）"，不是"判据失败"**（同
  `eval/type_gate.py` 三个量对不上时退 3 的理由）。做的时候顺手补一条反控（喂一段
  抽不到的函数体 ⇒ 必须退 3）。**等父仓那次重构落地再做**——现在动是照着一个还在变的
  形状写。

### 批 3 — 运行时可观测 ✅ 已落地（关停排空除外，见下）

- **`/version`** ✅：**只放静态构建身份**（git sha / 启动时刻 / python 版本），
  **不复述 dials**——那是活体状态、已经住在 `/health`，复述就是第二个真源。
  sha 取值顺序 = 环境变量 → git → `"unknown"`，不许手抄；**来源一起回**（`sha_source`），
  否则 `"unknown"` 与一个真 sha 长得一样权威。判据 `tests/test_version.py`。
- **`/ready`** ✅：就绪 ≠ 存活。判据要能证明这个区别：硬依赖缺失 ⇒ `/ready` 503，
  **而同一条件下 `/health` 仍 200**。两者同时红同时绿，说明 `/ready` 是装饰。
  判据 `tests/test_ready.py`（三种缺失各一遍，每遍配一条 `/health` 仍 200）。
- **启动配置自检** ✅：`/health` 现在把配错的提供方吞掉 ⇒ 加一行启动告警 + `/ready` 503，
  把「静默用错模型」变成可见状态。**刻意不拒绝启动**（拒了就是让探针把"配置缺"报成
  "agent 挂了"，又是指错方向）。
- **关停排空** ❌ **搁置**（20261018 决定，理由见 §3 那张表）：不自己装 SIGTERM handler。
- **补网（20261018 批 5 期间实测撞上）** ✅ —— **`hostinfo.trace_stats` 天天在编一份全绿**：
  它自己 `os.listdir(TRACES_DIR)` + `fn.endswith(".json")`，那是 **20260925 之前**的平铺
  布局；布局改成"按天一层"之后 `os.listdir` 回的全是目录名 ⇒ **一份 trace 都进不了循环**
  ⇒ 管理助手报表上天天是"今日 0 轮 / 异常 0 / 质检拦截 0 / shadow 权限拒绝 0"，而
  `readable` 还是 `True`（实测：真值 4 轮，它报 0）。**"缺键绝不编 0" 那一族里最坏的一种
  —— 它编的是一份全绿，而且长得跟"今天真的没人聊"一模一样。**
  修法 = 枚举与读取都走 `utils/trace_files.py` / `utils/trace_io.py` 那两份**单一实现**
  （20261019 之前它们在 `eval/` 下，而生产代码只许 import 本仓的 wheel 包
  ——`tests/test_packaging.py` ① 就是这条闸；搬进 `utils/` 后与写入端 `utils/trace.py`
  同住一个包，包门面同时收成空的，好让离线读取端不必连带拉起 `config.settings`）；
  trace 目录不存在 ⇒ 报 `readable: False`，**不许退回自己扫**（那正是病灶）。
  判据搬到 `tests/test_hostinfo_trace_stats.py`（新文件，见下条），含"**只有按天目录**"
  这条正控——那才是生产的形状。**原先那条判据没抓住它，因为夹具只铺平铺布局**：
  判据在一个**已经不存在的形状**上保持绿（同族：`test_ready.py` 那条"判据的前提要自己
  摆出来，而且摆的必须是今天的那个前提"）。
- 落点：判定逻辑住在 `agent/probe.py`（纯函数），`server.py` 只留两个薄路由——
  `server.py` 已超文件预算 3 倍，加 118 行会被 `tests/test_module_budget.py` 判「长文件又长了」，
  而这两件事**不需要**住在 app 模块里。台账那一格随之 +40 行（2589/52），是这次唯一的放宽。

### 批 4 — 错误与用量：可归因 ✅ 已落地（其中两项实测**早已做完**）

- **发射侧错误码收敛** ✅：**没有新增第三张表**。实测清点：生产侧能发出的码一共
  **20 个**（`agent/refs.py` 的字面返回 + `authz` / `adminops_target` / `idem` /
  `graph` 的 `REASON_*` 具名常量），**20/20 都已在表里**——没有"裸码"要并。
  真正缺的是**看着这件事的判据**：`tests/test_block_reasons.py` ③ 判的是**取回侧**
  （`_check_spec` 认哪几个字面、六个取回通道还接不接着），看不见生产侧。新增一节 ⑥：
  从源码现取生产侧的码（AST，不手抄名单）断言 ⊆ `REASONS`，带正控（扫不到就空过）
  与两条反控（合成一个未登记的新码 ⇒ 必须红；`return None, "人话"` 那种不许被当成码）。
  反控实测：往 `agent/authz.py` 加一个 `REASON_EXPIRED = "consent_expired"` ⇒ 当场红。
  豁免只有一条且**点名**（`REASON_OK = "ok"` 是裁决常量、不是受阻码），并配一条
  "这条豁免仍然必要"的判据。
- **重试策略归一** ✅ **早已做完**：三处常量本来就住在一起（`agent/graph.py` 的
  `_RETRY_HTTP` / `_RETRY_EXC` / `_llm_retriable`），判据 `tests/test_planner_llm_error.py`
  已逐码判（4xx 只有 408/409/425/429、5xx 全可重试、**400 不重试**、**超时不重试**）
  且带源码级共现锁。**没有 langchain `max_retries` 可"归一"**——全仓一处都没设。
- **侧任务计费** ✅：两条侧任务**走不同的路**（这是本批唯一需要判断的地方）：
  `summarizer` 跑在**同一轮对话**里（`_submit_with_context` 的 `copy_context` 把 recorder
  带进 worker 线程）⇒ 直接落本轮 trace 的 `llm_done`；`moderator` **没有** trace 上下文
  （留言板的独立请求），而 `record()` 没有 recorder 时是**静默空转**——写下去等于生产上
  什么都没发生。所以它把用量交给调用方（`review(..., on_usage=…)`），由审核端点打进既有
  那条 `[review]` 日志行；**返回值一个键都没加**（那是 Rust 读的响应体）。
  `agent/llm_usage.py` 头注原来写着"两条都运行在没有 trace 上下文的位置"——对摘要那条是
  **错的**，已改。
  判据 `tests/test_side_tasks.py` ⑧：两条各判一次，**反控实测**——把摘要的 `record()`
  摘掉只红摘要那两条、把审核的回调摘掉只红审核那一条（两条路确实分开了）。

### 批 5 — 权限影子读数（只测量，不切换）✅ 已落地

- **`eval/authz_shadow_divergence.py`** ✅：读 trace 里的 `authz_shadow` 事件，出分歧读数。
  **分歧的定义**＝影子判 denied、而那一件**真的执行成功**（`check` 事件 `verdict=PASS`）
  ⇒ 今天开 enforce 会拦掉这一条真实执行。判 denied 而**没**执行的（被同意闸/目标校验拦了，
  或 planner 改了主意）不算收口成本，单独报数。
  退出码：0 跑通 / 1 结构违约 / **3 窗口内零份 trace**（"没数据"不是"零分歧"）。
- **判据** ✅ `tests/test_authz_shadow.py`（12 节，零 LLM 零网络，合成 trace 由写侧那两行
  `str()` 生成而不是手抄格式）。**六处变异实测全部翻红**：读端把 `reason` 键名写错
  （模拟 `Decision.__str__` 改格式）⇒ 红 6 条；把 `call` 事件当成"执行了" ⇒ 红 2 条；
  `diverged` 恒 0 ⇒ 红 2 条；`_hard_scopes` 改成手抄常量 ⇒ 红 1 条；`no_manifest` 不切开
  ⇒ 红 2 条；零份 trace 也退 0 ⇒ 红 1 条。
  两条**格式往返锁**（`Decision.__str__` / `Principal.__str__` 的字面 + 解析回字段）：
  没有它们，格式一变读端会**静默**解析出空串、按 reason 的分档全变 0 —— 那是本仓
  "看着在跑、其实没跑"那一族里最难发现的一种。
- **两处"看着一样、其实是两件事"被切开**（这是本批的主要产出，比读数本身值钱）：
  ① **`no_manifest` 是两义**：名字**在**工具注册表里 ⇒ 真·工程疏漏（漏声明，应该是 0）；
     名字**不在** ⇒ 它不是"漏声明的工具"，是**解析碎片/幻觉工具名**（20260929 那条分号事故
     的残留，已由写端 `_esc_spec` 修）。混在一格里会让真信号被历史噪声淹掉。
  ② **`unknown_role` 数的不是越权**：它是"身份没传进来"（`uid=0` / `src=body`），
     与"授予表写窄了"是两件事。
- **20261018 首次读数**（近 30 天，1145 份 trace；只有 6 份带影子事件）：
  影子拒绝 **22 条**、分歧 **10 条**，**没有一条是"角色已认而 scope 没授予"（`denied` 0 条）**：
  · 10 条 `unknown_role`，全在 **20260920**（权限模型上线当天的联调流量，`role=ghost`/`?`、
    `src=body`）；
  · 12 条 `no_manifest`，全在 **20260929** 那一条分号事故里，名字是 `tri`/`circle`/`line`/`text`
    —— **一个都不在注册表里** ⇒ 真·漏声明 **0 条**；
  · 硬拦 scope 进影子事件 **0 条**。
  **这份读数要说清楚它没说什么**：分母只有 22 条、且全部落在两个历史现场 ⇒
  它证明的是"**这一个月里没有真分歧**"，**不是**"授予表已被充分验证"。
  批 8 的触发条件（影子读数零分歧）今天成立，但成立得很**薄**——切之前值得再挂一段时间，
  让分母长起来（这不需要任何新代码，`AUTHZ_ENFORCE` 保持 0 即可）。
- **先量再切**：影子读数必须在**当前请求形状**上取，因为断点恢复会改请求形状。
  ⇒ 批 6/7 之后这份读数**不可与今天直接比**，要重新取一段。

### 批 6 — 断点恢复前置（纯加法，无行为变更）✅ 已落地

**已落地（6a，20261018）**

- **`/health` 加 checkpoint 后端 dial** ✅：`dials.checkpoint_backend` = `none` / `postgres` /
  `sqlite` / `memory` / `unknown` / 认不出的模块路径原文。判定逻辑住
  `agent/probe.checkpoint_backend`（纯函数），`server.py` 只留一格。
  ⚠️ **它读的是 `_agent` 这个活体对象，不是 `CHECKPOINT_DSN`**——批 6 当时那一格**填了也不
  生效**（`build_checkpointer()` 没有调用方），读配置会把"配了 DSN"报成"挂上了 saver"，正是
  本仓最贵的那类失效。**批 7 起接线落地，但这条判据与理由一字不变**：拨的是**已经起好的那个
  进程**的配置，图那个对象一个字节没变 ⇒ "配了 DSN"与"这个进程挂上了 saver"永远是两件事。
  判据 `tests/test_health_dials.py` ⑤：七条形态各判一次，外加一条**反控**
  ——把 `CHECKPOINT_DSN` 拨成合法 DSN 而图没挂 saver ⇒ 这一格**必须不变**（变了就说明它是
  从配置推的）。`unknown` 与 `none` 刻意分开（"读不出来"不许报成"没挂"，
  同 `hostinfo.trace_stats` 那条 `readable`）。
- **共现锁** ✅ `tests/test_checkpoint_adoption.py`：判的不是"接了没有"，是**"有没有半接"**。
  四处（① saver 交给图、② `thread_id` 是会话坐标、③ 历史注入已删、④ 持久化档是 sync）
  要么全「未接」、要么全「已接」，混合就红，并点名是哪几处没跟上。
  **今天四处全「未接」（0/4）**，与生产行为逐字节相同。
  ⚠️ **这一条与预案原文的写法不同，理由记在这里**：预案写的是"三条全红，落地批翻绿"。
  按字面写成"断言已接"的话，它在采纳批之前**一直红**——而 `tests/run_all.py` 按磁盘枚举、
  **没有"预期红"这个档**，一条红的套件进不了 CI，只能被人挪出清单（那就成了一份没人跑的
  判据）。所以改成"一致即绿"，把"能红"这件事交给 ④/⑤ 那两组反控：
  **四条反控各单独拨一处，实测全部翻红**；另有一份最小的"全已接"骨架作正控（证明探针
  不是一组恒「未接」的东西）。
  ⚠️ **第四条（持久化档）比预案多出来，且落点与预案不同**：`durability` **不是
  `compile()` 的参数**（`StateGraph.compile` 的签名里没有它），它是 `stream()` 的参数
  （`langgraph 1.2.7` 实测）。所以"compile 处必须有同步持久化档"这句话在 API 上落不了地；
  真正的不变式是"**凡有 saver 在场的那次调用必须显式给同步档**"——`agent/idem.py` 的
  `require_sync_durability` 是它在运行期的断言，本件是它在源码形状上的对应物。
  判据读 **AST 不读文本**：`server.py` 那段解释注入的注释里就写着 `req.history[-20:]`、
  `agent/checkpoint.py` 头注里就写着 `durability="sync"`——子串匹配会被自己那段注释骗红
  （同 `tests/test_hostinfo_trace_stats.py` 那次）。

**已落地（6b，20261018）**

- **语料** ✅ **批 3 就做过了**：`GOLDEN_PROD_HISTORY=1`（`eval/run_golden.py` 的
  `ENV_PROD_HISTORY`）+ 判据 `tests/test_golden_prod_history.py`。批 6 只需确认它在。
- **golden 线程坐标** ✅：新增 `eval/golden_thread.py`——`thread_id` 从**常量**
  `"golden_thread"` 换成 **(用例, 重复, 臂)** 的坐标（`golden_<臂>_<用例>[_r<遍>]`）。
  `run_case` 显式传 `run_one(thread=…)`；同一个坐标也落进 trace 的 `thread_id` 那一格
  （`golden_trace.start_case(..., thread=…)`——那里写死的话，trace 里这一格会与图实际用的
  线程**不一致**，而"线程有没有复用"恰恰要读它）；驱动的 `GOLDEN_REP` 补上"重复"那一段。
  ⚠️ **驱动的逐遍复位仍然必需**：它删的是 `golden_thread` 与 `conv_<id>`，而臂 B 的线程
  是 `conv_<conversation_id>`——那个坐标**跨遍、跨臂都相同**，本坐标管不到它。
  ⚠️ **为什么不是"只靠驱动复位就行"**：那个复位只活在 `ckpt_ab_driver` 里，不经驱动的
  跑法（夜间 `nightly_regression.sh`、手跑、`golden_case_runner`）没有这一层。把复位换成
  "名字里本来就带着坐标"，等于让正确性不再依赖调用方记得做某一步。
  判据 `tests/test_golden_thread.py`：三条不变式 + 归一化 + **拿真语料量撞名**
  （192 条用例的线程名两两不同——归一化会把 `a.b` 与 `a-b` 折成同一个 slug，那是真实存在
  的失效面，所以这条不靠"应该不会撞"的直觉）+ 两条 AST 接线锁。
  ⚠️ **`run_one` 的 `thread` 缺省仍是老常量**：`eval/zero_call_residual_probe.py` /
  `eval/arm_capability.py` 等探针直接调 `run_one` 并各传各的线程名，缺省不许动它们。
- **依赖进 `pyproject.toml` 与锁文件** ✅ **已做**（`langgraph-checkpoint-postgres==3.1.0` /
  `psycopg[binary]==3.3.6` / `psycopg-pool==3.3.3`，`uv.lock` 里有对应条目；本机不跑同步，
  见 §5）。
- **保留期归口** ✅ **已做**（`eval/retention_manifest.py` 的 `agent-idem-ledger`，登记为
  `open`／未接管，`why` 里写明"与将来的 checkpoint store 必须一起设计"——那一格**不该**
  在批 6 就给它一个保留期，理由见登记项本身）。

**批 6 收口**：四处纯加法全部落地，`AUTHZ_ENFORCE` 与生产行为一字未动。本批动了三本账
（都是**文本外科改那一个数**）：模块行数 `server.py` 2592 → 2595、`eval/run_golden.py`
3677 → 3686；函数行数 `eval/run_golden.py::run_one` 259 → 262。**批 7 是这些前置的落地对象。**

### 批 7 — 断点恢复落地（一次原子提交，不拆）✅ 已落地（代码面；**生产是否在跑看 DSN**）

**三处一起动**（`agent/checkpoint.py` 头注）已落地，且**共用同一个判据**
`server._graph_holds_context()`——它读**活体对象上有没有 saver**（`agent/probe.py`，与
`/health` 的 `checkpoint_backend` 同一个取值实现），不读 `CHECKPOINT_DSN`。于是"配了 DSN
但没挂上"与"没配 DSN"是同一个源码形态，**半接在结构上不可能**。

⚠️ **与预案写的三处不同，逐条记在这里**（理由都在 `agent/checkpoint.py` 头注）：
- **`build_graph(checkpointer=…)` 改成 `server._build_arm()` 的
  `create_agent(checkpointer=checkpoint.build_checkpointer())`**：生产调用点是 `create_agent`，
  `build_graph` 只转发（`tests/test_checkpoint_factory.py` ⑤ 锁着"只传不造"）。
- **"删注入循环"改成"给注入套闸门"**（`if not _graph_holds_context():`）：删掉的话，
  **没挂 saver 的那条路**（DSN 空时的生产、没挂 saver 的评测臂）会失去全部跨轮上下文
  ——比"多注入一份"更坏，且同样不报错。
- **`durability` 是 `stream()` 的参数，不是 `compile()` 的参数**（langgraph 1.2.7 实测），
  且**有 saver 才给**：没 saver 时给了会招一条 UserWarning，而 `ReactGoldenArm.stream()`
  压根没有这个形参 ⇒ `TypeError` 炸在生产者里、整条端到端判据**静默变成"零帧"**
  （实测被 `tests/test_react_arm.py` 抓住）。`ReactGoldenArm` 收到非 None 的档会
  **响亮拒绝**——它不挂 checkpointer，静默收下等于把"没有持久化"伪装成"有持久化但没区别"。

**建表加了一把咨询锁**（`agent/checkpoint.py::_setup_under_lock`）：`saver.setup()` 幂等但
**不并发安全**（每条迁移末尾 `INSERT INTO checkpoint_migrations (v)`，没有 `ON CONFLICT`），
产线 `--workers 4` 同时启动 ⇒ 后到的主键冲突、**启动崩**。

⚠️ **第一版用的是阻塞式 `pg_advisory_lock`，它造成了一次真停服**（20261018 上线当天：
4 个 worker 无限期卡死、零日志、8010 不监听、`systemctl restart` 卡在等旧 worker 上）。
机制、现场读数、为什么四层判据全绿、以及"杀掉客户端 ≠ PG 停了"这条同源的坑，都记在
[adr-0011](adr/adr-0011-graph-holds-context-checkpoint-memory.md) 的《20261018 追记（二）》。
现在建表那一步是 **`pg_try_advisory_lock` 轮询 + 60s 预算**（超了响亮抛），建表前先删掉
上一次被打断留下的**无效索引**（`CREATE INDEX CONCURRENTLY IF NOT EXISTS` 会永远跳过它）。
**停服之后补上的那一层判据**是 `tests/test_checkpoint_setup_concurrent.py`——真连一个库、
4 个进程同时建表，**红基线 = 换回阻塞版必须卡住**。

**级联清理**：`agent/checkpoint.py::delete_thread`（返回值是**"删完读回来确认"**，不是
"没抛异常"——`DELETE ... WHERE` 删不存在的线程同样不抛，只看"没抛"的话恒不生效的删除与
真删除**长得一模一样**）+ `DELETE /checkpoint/{conversation_id}` 钩子。
⚠️ **父仓那一半截至 20261018 还没接上**（`src/routes/conversation.rs::purge_conversation_data`
的两个臂都没调钩子）⇒ 本仓的钩子今天**没有任何调用方**，这是**已知缺口**。

**判据**（四份，全绿）：
- `tests/test_checkpoint_adoption.py`：四处形态**一致即绿**。批 6 时是 0/4（全未接），
  本批起 4/4（全已接）。**"能红"由反控证**：四条反控各拨一处全部翻红，外加 ④b
  ——**把坐标本体换成不产坐标的实现而 `server.py` 一个字节不动**（"改名不改义"），
  也必须红。正控是另一极（一份全「未接」骨架）⇒ 证明探针不是一组恒「已接」的东西。
- `tests/test_checkpoint_factory.py`：④ 改成**打桩 import**（采纳后本机与 CI 都装了 PG
  后端，照"装没装"分叉的话这条在最需要它的两个环境里**永远不执行**）；新增 ④b
  （连不上 ⇒ 响亮抛、**绝不返回坏 saver**、异常里的口令已脱敏）与 ⑧（`conversation_thread`
  在缺席六形态上必须回 `None`——拼成 `conv_None` 就是**跨用户串台**；`thread_id_for` 三支；
  `redact`）。所有 DSN 指 `127.0.0.1:1`：**不许指 5432**，那是本机生产 PG，
  判据去连它就把单测变成对生产的探测。
- `tests/test_checkpoint_cascade.py`（**新建**）：① 机制（含"删了没生效必须返回 False"
  的反控）② 端点三支 + "不许把没删掉报成删掉了"的反控 ③ **跨仓守卫**。
  ⚠️ ③ **判的不是"父仓接了吗"**（那会一直红、而红的套件进不了 CI，同批 6 那次）：
  判的是**父仓自己两臂的一致**——`purge_conversation_data` 的 `ConversationId` / `UserId`
  两臂要么都调钩子、要么都不调，**半接就红**。这不是编出来的判据：父仓自己在那份头注里
  写着它上一版正是"清单两处各写一遍、写的不是同一份"，漏掉的三张表因此变成孤儿。
- `tests/test_checkpoint_setup_concurrent.py`（**20261018 停服之后补的第四份**）：真连一个库、
  4 个进程同时建表。**它才是抓得住那次停服的那一层**——前三份全在 `127.0.0.1:1` 这种必然
  失败的 DSN 上判。缺 `SAUDADE_CKPT_TEST_DSN` 时它报「未评估」并退 0（`SAUDADE_REQUIRE_CKPT_TEST=1`
  把它变成失败），`tests/run_all.py` 的汇总里另有一行**每次运行都报**这一层量没量。

**幂等账转「在跑」**：`agent/idem.py` 的 `active()` 是"挂没挂 checkpointer"，本批起挂上就
自然打开（`tests/test_write_idempotency.py` 全绿）。**没挂时逐字节不变**。

**三处台账的放宽（全是文本外科改那一个数，没跑 `--update`）**——都因为"批 7 的接线真的
落在这几个文件里"，且都**先试过按主题拆**（判定本体已搬到 `agent/probe.py` 与
`agent/checkpoint.py`，`server.py` 只剩三个薄壳）：

| 台账 | 条目 | 变化 | 为什么不能再拆 |
|---|---|---|---|
| 模块行数 | `server.py` | 2595 → **2657**，顶层定义 52 → **56** | 四个薄壳（`_graph_holds_context` / `_thread_id_for` / `_stream_kwargs` / `checkpoint_delete`）各是"判据 + 路由"，本体已搬走 |
| 模块行数 | `agent/react_arm.py` | 1243 → **1248** | 只加了一段注释：说明**本臂刻意不收 `durability`**（它不挂 checkpointer；收了会静默吞掉同步档） |
| 模块行数 | `tests/test_react_arm.py` | 1210 → **1214** | ⑫ 那条源码锁跟着钉**新**不变量（`graph` 档只多一个 `checkpointer=`） |

⚠️ **复杂度与函数行数那一本最后没动**：第一版把持久化档写成**无条件**的
`durability=(X if 闸门 else None)`，那给 `_run_agent_stream_to_queue` 加了 1 个分支、2 行，
账得跟着放宽。**实测那是个坏形状**——没挂 saver 的臂/桩根本没有这个形参
（`ReactGoldenArm.stream()`），一条 `TypeError` 把 `test_factblock` /
`test_exec_frame_incremental` / `test_trace_reply_source` 三条端到端判据**静默变成"零帧"**。
改成"有 saver 才给这一格"（`_stream_kwargs()`）之后，复杂度和行数都回到账上，
**两本账一格都不用放宽**。记在这里是因为"改完发现不用放宽"和"一开始就该那么写"
是两件事，下一个遇到同类形状的人值得知道试错方向。

`agent/checkpoint.py`、`agent/checkpoint_arm.py`、`agent/checkpoint_delta_arm.py` 不在模块
台账里（都不足 800 行）。

**还没做（不在本批）**：真机读数三条（长会话 K=10 正向收缩、崩溃重放对产线等效 saver、
同会话并发两轮）。本批只保证"接得上、接得对、半接会红"。

⚠️ **一条已知缺口（20261018 实测，未修）**：**填上 DSN 那一刻，老会话的第一轮会丢上下文**。
saver 挂上之后历史注入被闸门关掉，而**没有任何"播种"路径**——`server.py` 里没有
`get_state` / `get_tuple`，那条会话的 checkpoint 是空的 ⇒ 图的 state 里没有历史，
注入又被关掉了 ⇒ **那一轮它看不见之前聊过什么**。第二轮起 saver 里有了这一轮的消息，
才接上。新会话不受影响（它本来就没有历史）。这是"三处一起动"的**必然代价**，不是 bug；
要修就得在闸门那一支里加一次"saver 空 ⇒ 注入一次"的播种（`_graph_holds_context()` 的
判据也要跟着改），**那是一个独立的改动**，别顺手塞进别的批。

#### 批 7 的运维交接（**归主人**，本仓不跑）

代码落地 ≠ 生产在跑。三件事，按顺序：

0. **先跑一遍那条真连库的判据**（**上线前的唯一前置**，本机就能跑）：
   ```bash
   DSN=$(grep '^DATABASE_URL=' /home/ubuntu/Saudade-Blog/.env | cut -d= -f2- \
         | sed 's#/saudade_blog$#/saudade_it#')
   SAUDADE_CKPT_TEST_DSN="$DSN" .venv/bin/python tests/test_checkpoint_setup_concurrent.py
   ```
   要看到 `全部通过 ✅`（含**红基线卡住**那一半）。**它会 `DROP TABLE`**，所以只喂
   `saudade_it`——喂 `saudade_blog` 它当场拒跑。⚠️ 20261018 那次停服就是**跳过这一步**
   直接上线的：当时四层判据全绿，而它们全在必然失败的 DSN 上判。
1. **建表 + 加 DSN**：在 agent 仓 `.env` 里加一行 `CHECKPOINT_DSN=postgresql://…`
   （表由 `PostgresSaver.setup()` 在**启动时**幂等建，**不需要迁移脚本**，见 ADR-0012
   《迁移到 PG》；建表那一步是**轮询抢锁 + 预算**，4 个 worker 同时启动安全）。
   ⚠️ **DSN 指向哪个库是要挑的**：它会在那个库里建 4 张表
   （`checkpoints` / `checkpoint_writes` / `checkpoint_blobs` + 幂等账 `agent_write_idem`），
   这些是**本 agent 自己的表**、与博客业务表不重名；但**别指错库**。
   · 生产候选 = **`saudade_blog`**（`saudade_app` 有 CREATE，且 20261018 那次实验已经在那里
     建到 `max(v)=5`、没有无效索引 ⇒ 这次启动会从 6 继续跑到底）。
   · **`agent_checkpoint` 不归生产**：那是实验线（`/home/ubuntu/exp/ckpt.env`，owner `agent_ckpt`）
     的库，别拿它当生产 saver 的家。
   · 全新库也行（要有 CREATE 权限）。**不建议**再另起一个专库——多一个库就多一份要记的运维面。
   `.venv` 里 `langgraph.checkpoint.postgres` / `psycopg` / `psycopg_pool` **已在**。
   ⚠️ **如果这个库上还有上一次被打断留下的痕迹**（迁移停在中间、或
   `SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE NOT i.indisvalid`
   有行）：**不用手动清**，启动时 `drop_invalid_indexes()` 会删掉无效索引、`setup()` 从
   停下的版本号接着跑。但**先确认没有卡死的旧 worker**（见第 2 步那句）。
2. **重启**：`sudo systemctl restart saudade-agent && sleep 8`。
   DSN 连不上/依赖没装/scheme 不认识 ⇒ **进程起不来**（`Restart=always` 会反复重启，
   日志里是 `[checkpoint]` 那条 RuntimeError）——这是**刻意的**：坏 saver 的失效时刻
   本该是启动，不是"第一轮对话的第一次 checkpoint"。
   ⚠️ **重启卡住 = 快去看 PG，别看日志**（20261018 那次就是零日志）。`restart` 超过
   十几秒还没回来时：
   ```bash
   psql "$DSN" -c "SELECT pid, state, wait_event_type, wait_event, left(query,60) \
     FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
   ```
   看到 `Lock/advisory` + `Lock/virtualxid` 同时在场就是环（或是一把没人放的旧锁）。
   旧版才会成环（现在这一版是 try-lock + 60s 预算，最坏是一条点名的启动错误）；
   **真遇到了**：`SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE …` 点掉
   那几个后端，`systemctl restart` 就会立刻回来。
3. **读回执**（**唯一能证明它在跑的东西**）：
   ```bash
   curl -s http://127.0.0.1:8010/health | python3 -c 'import json,sys;print(json.load(sys.stdin)["dials"]["checkpoint_backend"])'
   ```
   要看到 `postgres`。看到 `none` ⇒ DSN 没读到（`SAUDADE_IGNORE_ENV_FILE`？）；
   看到 `unknown` ⇒ 图那个对象上读不出 saver。**`none`/`unknown` 都算没接上**。

**父仓那一半（同样是主人那边的事，本仓改不了）**：在
`src/routes/conversation.rs::purge_conversation_data` 的**两个臂**里各加一次对本仓钩子的调用
（`DELETE http://127.0.0.1:8010/checkpoint/{id}`，`UserId` 那一臂要按该 uid 的会话逐个删）。
**只加一个臂 ⇒ `tests/test_checkpoint_cascade.py` ③ 当场红**——那正是父仓自己踩过的形状。
没接上之前：删了会话、checkpoint 还留在库里（**已知缺口**，判据 ③ 会打一行字说明）。

### 批 8 — 权限从影子到强制（触发条件驱动，不硬排）

`AUTHZ_ENFORCE=1` 的前置 = 批 5 的影子读数零分歧 + `tests/test_authz.py` 在钉住的取值下
判 denied 必 BLOCK。新增的钉值要同时进 `tests/run_all.py` 的钉表与 `tests/test_ci_suite_list.py`
的档位检查。

⚠️ **"零分歧"要读对**（批 5 的读数给的口径）：20261018 的读数是"真分歧 0 条"，但分母只有
22 条、且全部落在两个历史现场 ⇒ 那是"没被触发过"，不是"被验证过"。切之前先问两个问题：
① 分母长起来了吗（近 30 天里出现过几次影子拒绝）；② `unknown_role` 那一族在**断言强制
打开**的生产上还出现吗（它出现 = 身份没传进来，切 enforce 会让那些请求**一个工具都没有**，
而那不是"越权被拦住"）。两条都答完再动开关。

---

## 3. 明确不做 / 搁置

写下「不做」和写下「要做」一样重要。每条带**可观测的触发条件**——
「以后有需要再做」不是触发条件。

| 不做 / 搁置 | 为什么 | 触发条件 |
|---|---|---|
| MCP 接入 | 工具注册表是闭集 + scope 受审，与 MCP 的动态面不兼容 | 出现第一个「要接的外部工具不在本站 HTTP 面内」 |
| 提供方 failover / 熔断 | 生产只钉了一个提供方，failover 是投机 | 生产**真的配了第二个提供方** |
| Prometheus `/metrics` | 可观测是 trace + dials 两处，端点会与它们重复 | 出现第二个消费方（现在全是离线脚本） |
| agent 边界再加一层鉴权 | 只回环，鉴权是父仓的活，加了就是两份要同步的真源 | 永不 |
| 通用重试库 | 现有重试是**有预算意识**的（超时不重试），库会把这件事藏起来 | 永不 |
| 第三张错误码表 | 已有 BLOCK 原因码闭集，再叠一张只多一份要同步的东西 | 永不 |
| 自己装 SIGTERM handler 做「关停排空」 | uvicorn **已经**有优雅停机（SIGTERM 后在途请求自然收尾），自己再装一个就是**顶掉它**、把这件事重实现一遍；而真正的缝在**跨仓**：父仓 unit 的 `TimeoutStopSec=120` **小于**本仓 `STREAM_TOTAL_TIMEOUT=300` ⇒ 走到第 120 秒还在跑的流会被 SIGKILL，那时进程已经没了、`end_reason` **写不出来**（这一条不是本仓能补的） | 出现一次「部署重启后用户看到半截回复，而 `logs/agent/agent.log` 里没有对应的 `[stream] end reason=` 行」⇒ 那时把父仓 unit 的 `TimeoutStopSec` 抬到 >300，**不要**在本仓加 handler |
| OTel / 任务队列 / 缓存层 / 向量库 / 管理 UI | 超出本仓形状 | 永不 |

---

## 4. 判据与验证口径

- **每批结束**：`SAUDADE_PARENT_REPO=/home/ubuntu/Saudade-Blog .venv/bin/python tests/run_all.py`
  （不设该变量，几条跨语言守卫找不到父仓）。
- **静态**：改动的文件 `ruff check` 零 error；`eval/type_gate.py`。
- **台账**：收紧一律**文本改那一个数**。
- **文档**：新文档必须进 [docs/README.md](README.md) 索引；行为变更进 `CHANGELOG.md`。
- **接线自查**（每个提交前问一遍）：套件清单是否只有 `tests/run_all.py` 一处？钉值是否同步？
  ruff 版本是否与 `pyproject.toml` 的 dev 组一致？新增的运行期产物是否登记了保留期？

## 5. 本机约束

- **不跑依赖同步**；离线套件用仓里的 `.venv`。
- **提交逐文件点名**，严禁 `git add -A`——工作区常年挂着别的批次的在途改动。
- 生产迁移 / 部署 / 重启不在本文档范围内。
