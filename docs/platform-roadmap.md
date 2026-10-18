# 平台面建设与整体规范化路线图

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
| 断点恢复 | **无**。生产不挂 saver；`agent/checkpoint.py` 的 `build_checkpointer()` 是响亮桩、零生产调用方；`thread_id` 每请求随机 | `agent/checkpoint.py`、`server.py` |
| 幂等账 | **已建成但惰性**：`agent/idem.py` 的 `active()` 要求 checkpointer 在场 ⇒ 恒 False | `agent/idem.py` |
| 版本可见性 | **无** `/version`、无 build-info；`/health` 只回声 dials （**已补**：批 3，`agent/probe.version_payload`） | `server.py`、`agent/probe.py` |
| 错误码 | BLOCK 原因码**已有闭集**（`agent/block_reasons.py` + `tests/test_block_reasons.py`）；**发射侧**没有对应闭集 | 同上 |
| 重试 | **已有**但**散在**：重试码集、重试异常集、`_llm_retriable()`、planner 里的一次性重试各住一处 | `agent/graph.py` |
| 用量计量 | 主链路计；**侧任务（moderator / summarizer）不计** | `agent/llm_usage.py` 头注自认 |
| 权限 | scope 表与同意闸齐备，但 `AUTHZ_ENFORCE` 默认 **0（影子）** | `agent/authz.py`、`.env.example` |
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

### 批 5 — 权限影子读数（只测量，不切换）

- 读 trace 里的影子事件，出分歧读数（待建：`eval/authz_shadow_divergence.py` + 对应套件）。
  反控：塞一条「判 denied 却真执行了」的 trace ⇒ 非零。
- **先量再切**：影子读数必须在**当前请求形状**上取，因为断点恢复会改请求形状。

### 批 6 — 断点恢复前置（纯加法，无行为变更）

- **语料**：让 golden 具备生产形状（多轮用例注入真实历史），否则第 3 条证据不成立。
- **golden 驱动**：`eval/run_golden.py` 的线程常量要按（用例, 重复, 臂）复位——
  ADR-0011 已写明「不复位就是假读数」。
- **判据先红**（待建：`tests/test_checkpoint_adoption.py`）：源码级共现锁，三条——
  thread_id 路径里不许有随机后缀、历史注入循环必须消失、compile 处必须有同步持久化档。
  **现在三条全红**，落地批翻绿。
- **依赖进 `pyproject.toml` 与锁文件**（同一提交，否则打包闸红）。
- **`/health` 加一个 checkpoint 后端 dial**：让人**看得见**这个进程到底有没有挂 saver，
  而不是从 `.env` 推。
- **保留期归口**：`agent/idem.py` 的 SQLite 账本进 `eval/retention_manifest.py`
  （现在登记为「未接管」）。

### 批 7 — 断点恢复落地（一次原子提交，不拆）

- **三处一起动**（见 `agent/checkpoint.py` 头注）＋ 显式同步持久化档——默认档会在硬杀时绕开幂等账。
- **级联清理**：删会话时清该会话的 checkpoint。⚠️ **这是跨仓契约**——删除动作在父仓
  `src/routes/conversation.rs`，本仓只出钩子 + 一条父仓守卫（`SAUDADE_REQUIRE_PARENT=1`）。
- **幂等账转「在跑」**：`agent/idem.py` 的惰性闸自然打开。
- 判据：批 6 那三条翻绿 + 级联删除的 no-op 要红（待建：`tests/test_checkpoint_cascade.py`）。

### 批 8 — 权限从影子到强制（触发条件驱动，不硬排）

`AUTHZ_ENFORCE=1` 的前置 = 批 5 的影子读数零分歧 + `tests/test_authz.py` 在钉住的取值下
判 denied 必 BLOCK。新增的钉值要同时进 `tests/run_all.py` 的钉表与 `tests/test_ci_suite_list.py`
的档位检查。

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
