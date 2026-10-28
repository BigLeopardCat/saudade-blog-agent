# 架构决策记录（ADR）

这个目录回答一个问题：**「为什么会是这样？」**——尤其是那些从代码里读不出来的部分。
代码能告诉你"这里有个断崖系数 0.25"，读不出"0.35 会丢多答文档、绝对阈值已实证不可用"；
能告诉你"合成向量 + RRF"，读不出"为什么不是向量库、为什么开关默认关"。

`docs/问题记录.md` 记的是**事故**（发生了什么、怎么修的），本目录记的是**取舍**
（当时还摆着哪些选项、凭什么选了这一个、什么条件下该重开）。两者互补，不互相替代。

## 什么时候该写一份 ADR

写：

- 一个决定**会被后来的人想推翻**（"这写得不对吧，直接用 X 不就行了"）——把"X 试过了、
  为什么不行"写下来，才不用再试一遍；
- 决定**横跨多个文件/语言**（Python ↔ Rust ↔ 前端的三端契约、CI 与部署管线）；
- 决定是**不做某事**。这一类比做某事更需要写：代码里看不见一个"没被写出来的功能"，
  于是每年都会有人重新提议一次。

不写：单个函数怎么实现、命名、排版、纯测试补件。那些 `git log` 就够了。

## 体例

文件名 `adr-NNNN-<短横线短描述>.md`，编号连续、**不复用**（废弃的也留着占位）。
正文开头是一段固定字段的头部，然后是 `## 背景` / `## 决策`（可选 `## 取舍`、`## 复验`）：

```markdown
# ADR-NNNN：一句话说清这个决定

- **状态**：<取值见下表>（YYYYMMDD）
- **背景文档**：<这个决定落在哪些文件/哪些守卫上>（可不填）
- **影响范围**：<改动波及哪些文件；"无"也要写>
- **判据**：<哪条测试/哪个守卫会抓住"这个决定被违反了">
- **触发条件**：<仅搁置态需要：什么可观测的事实发生了，就该重开这份 ADR>

## 背景

本来是什么样、哪里不对、当时的实测数字。

## 决策

选了什么、为什么不是别的选项。
```

### 状态取值

| 状态 | 含义 | issue/标签 |
|---|---|---|
| `提议中` | 还没拍板，正在写 | 对应 issue 开着，`needs-triage` |
| `已采纳` | 已确认采用；正文区分已落地范围与待验证实现 | 合了就关对应决策 issue，实施状态写入路线图 |
| `已搁置` | **方向认可，但条件没到**——同时写清触发条件 | 见 [../../ROADMAP.md](../../ROADMAP.md)，issue 带 `future` 标签且**保持开着** |
| `已废弃` | 曾经采纳，后来不成立了——**写清为什么不成立** | 关 issue，`wontfix` |
| `被 ADR-NNNN 取代` | 有更新的一份说了算，这一份只作历史 | 关 issue |

搁置与废弃的区别值得较真：**搁置 = 迟早要做，等一个信号**；**废弃 = 不想做了，且理由变了**。
两者在 GitHub 上都表现为"没在写"，所以只能靠 ADR 的状态字段区分——这也是为什么"不做某事"
的决定必须写下来。

### 头部字段的用处

`判据` 这一格不是修辞。这个仓的失效模式里最贵的一类是**「看着在跑、其实没跑」**
（判据恒真、守卫静默跳过、开关拨了没生效），所以每个决定都要能回答"它被违反的时候，
**哪一行会变红**"。答不出来，通常说明这个决定还没有判据，而不是说明它不需要判据。

## 索引

| 编号 | 标题 | 状态 |
|---|---|---|
| [0001](adr-0001-native-toolcalls-skill-level-seam.md) | native tool calls 的接缝开在技能级，不接在工具级 | 已采纳 |
| [0002](adr-0002-task-state-model-declared-intent.md) | 未完成的意图由模型声明，完成由回执认定 | 已采纳 |
| [0003](adr-0003-single-skill-table-in-native-mode.md) | native 档一轮只发一份技能表（技能块去重） | 已采纳 |
| [0004](adr-0004-cross-language-guard-in-ci.md) | 公开契约源码固定版本，PR 检查无需私人凭据 | 已采纳 |
| [0005](adr-0005-multimodal-retrieval-deferred.md) | 多模态检索：方向认可，现在不做（等图片规模与查询形态） | **已搁置** |
| [0006](adr-0006-permission-rules-deferred.md) | 权限规则集（(action, resource, effect) +「总是允许」）：方向认可，现在不做 | **已搁置** |
| [0007](adr-0007-unavailable-retry-not-fixed-in-prompt-layer.md) | `unavailable` 的那次原地重试不在提示词层治（试过两版、A/B 否掉） | **已搁置** |
| [0008](adr-0008-second-arm-stays-in-mainline-frame-contract-locked.md) | 第二条臂留在主线当开关；帧契约收成一件有锁的产物 | 已采纳 |
| [0009](adr-0009-react-arm-becomes-a-real-graph.md) | 第二条臂的循环拆成真节点（`create_agent` 退场） | 已采纳 |
| [0010](adr-0010-state-accumulator-reducers-deferred.md) | state 累加器的合并语义：先立台账，暂不加 reducer | **已搁置** |
| [0011](adr-0011-graph-holds-context-checkpoint-memory.md) | 上下文由图自己持有（原生 checkpoint 取代手写注入）：代码面已采纳，**默认不生效**（DSN 空则不挂 saver） | 已采纳 |
| [0012](adr-0012-write-idempotency-ledger.md) | 写操作的幂等账：先记账再动手，至多一次 | 已采纳 |
| [0013](adr-0013-chat-image-persistence.md) | 聊天图片落库，但只做展示（模型仍只看 `[图片]` 标记） | 被 ADR-0022 取代 |
| [0014](adr-0014-assertion-verify-via-pyjwt.md) | 身份断言的验签与签发改用 PyJWT（`verify_sub` 那枚地雷写在最前面） | 已采纳 |
| [0015](adr-0015-planner-becomes-router-schema-on-demand.md) | planner 降成路由器，schema 索引化按需取（分四阶段，**部分取代 ADR-0001**；**阶段 0 已按判据停**） | **提议中** |
| [0016](adr-0016-skill-registry-becomes-config-files.md) | 技能注册表从 Python 字面量改成一条技能一个 TOML 配置（试点域已落地） | 已采纳 |
| [0017](adr-0017-versioned-rag-evidence.md) | 版本化公开语料、不可变向量代与正文证据约束 | 已采纳 |
| [0018](adr-0018-embedding-input-budgets.md) | 固定 tokenizer 的完整 embedding 输入预算与空间签名 | 提议中 |
| [0019](adr-0019-local-embedding-runtime.md) | 本地 embedding 共用成熟运行时、固定资产并保留 API | 提议中 |
| [0020](adr-0020-conversation-store.md) | 原生会话存储采用薄接口，旧图 checkpoint 细节留在兼容适配器 | 已采纳 |
| [0021](adr-0021-native-runtime-core.md) | 原生轮次与会话存储脱离旧编排图，传输接入单独收敛 | 已采纳 |
| [0022](adr-0022-conversation-memory-boundaries.md) | 会话历史按来源裁剪，图片按需回看 | 已采纳 |
| [0023](adr-0023-confirmation-continuation.md) | 签名确认暂停原生工具交换，取得回执后续行 | 已采纳 |
| [0024](adr-0024-framework-kernel-and-domain-extensions.md) | 成熟执行内核、精简业务 harness 与领域能力包 | 已采纳（标准框架已落地，整体验收进行中） |

| [0025](adr-0025-interrupted-task-recovery.md) | 普通任务通过框架 checkpoint 显式续跑 | 已采纳 |
| [0026](adr-0026-tool-activation-lifecycle.md) | 工具激活状态独立于历史窗口 | 已采纳 |

## 相关的、但不算 ADR 的文档

- [../toolcall-stability-roadmap.md](../toolcall-stability-roadmap.md)：工具调用稳定性的
  长期路线图，含明确"不做"的清单；
- [../rag-design.md](../rag-design.md)：当前语料协议、检索、读取证据与评测设计；
- [../multimodal-retrieval.md](../multimodal-retrieval.md)：多模态检索的分阶段方案
  （ADR-0005 的展开）；
- [../问题记录.md](../问题记录.md)：事故级取证，按日期。
