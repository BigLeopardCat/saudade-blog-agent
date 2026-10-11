# 文档索引

这里是本仓手写文档的**唯一入口**。根 [README.md](../README.md) 只讲"这是什么、怎么跑"，
细节全在下面这十几份里——按"你现在想知道什么"挑一份，不要按文件名猜。

**三档类型**（判断一份文档该不该改写时按这个分）：

| 类型 | 正文写什么 | 日期戳怎么处理 |
|---|---|---|
| **现状型** | 只写"**现在是什么**" | 成段的逐版流水账收进文首/文末的《变更注 / 修订记录》，正文不写；**但"这条规则当初怎么来的、为什么现在是它"那句话留着**——它是这条规则的依据，不是流水账 |
| **记录型** | 时间线**就是内容**（事故、实验、评估、交接，按日期） | 不动——历史描述是这份文件存在的意义 |
| **方向型** | 还没做、或已搁置的**预案**，带触发条件 | 只写"在什么可观测的事实出现时重开" |

> **允许一份文档跨档，但必须在文首给一张分节地图。** 框架（现状）与路线（方向）常长在同一份
> 文件里，切不干净时不要硬贴一个标签了事——**在文首写一行"哪几节是什么档"**，索引表那一格写
> **主档 + 括注另一档的节号**。判据**按节走**：现状节照现状型改（该更新就更新），方向节照方向型改，
> 记录节不动。**别因为"这份是方向型"就放过它里面的现状节，也别因为"这份是现状型"就把它的历史节
> 重写一遍。**

## 目录

| 文档 | 一句话 | 类型 |
|---|---|---|
| [agent-architecture.md](agent-architecture.md) | 系统现在是什么：节点拓扑、跨语言契约、目录与机制 | 现状型 |
| [问题记录.md](问题记录.md) | 事故取证，按日期：现象 → 根因 → 修复 → 回归锁（**§2.1 例外**，见下） | 记录型（+ §2.1 跨仓契约） |
| [eval-observability.md](eval-observability.md) | 评测与可观测性的 L0–L3 分层口径（判据该长什么样） | 现状型 |
| [lint-baseline.md](lint-baseline.md) | 静态检查（ruff）基线与"新代码要干净"的参照点 | 现状型 |
| [rag-design.md](rag-design.md) | 检索设计与实现总结，§8 是触发条件驱动的升级预案 | 现状型 |
| [native-toolcalls-mainline.md](native-toolcalls-mainline.md) | 换主线的**交接件**：为什么换、冻结了什么、边界在哪 | 记录型 |
| [toolcall-stability-roadmap.md](toolcall-stability-roadmap.md) | 工具调用稳定性的长期路线图，含明确"不做"清单 | 方向型（§3–§7）+ 记录型（§0–§2、附一–附七） |
| [react-line-experiment.md](react-line-experiment.md) | ReAct 试验线的实测：原生 tool calls 当骨架行不行 | 记录型 |
| [zero-call-residual.md](zero-call-residual.md) | 「零调用/声称」一次没有效果的实测与残余落点 | 记录型 |
| [param-tuning-20261006.md](param-tuning-20261006.md) | 旋钮 A/B 实验报告（温度 / 思考 / 跨模型 + 两臂成本） | 记录型 |
| [agent-eval-report-20260924.md](agent-eval-report-20260924.md) | 2026-09-24 的评估报告与当时列的行为清单 | 记录型 |
| [identity-and-permissions.md](identity-and-permissions.md) | 身份与权限：Principal / scope manifest / 同意闸 / 管理助手（原名 secretary.md） | 现状型 |
| [multimodal-retrieval.md](multimodal-retrieval.md) | 多模态检索分阶段方案（ADR-0005 的展开，**已搁置**） | 方向型 |

> 这份表**刻意不写"最后修订"日期**：手抄的日期没有判据看着（CI 是浅克隆，读不到逐文件的
> 提交日期），注定漂。要看某份文档最近改了什么：`git log -1 --format=%ad -- docs/<文件>`。

架构决策记录另有索引：[adr/README.md](adr/README.md)（7 份，记"当时为什么这样选、什么条件下重开"）。

仓根还有几份不属于 `docs/`： [README.md](../README.md)（是什么、怎么跑）、
[CONTRIBUTING.md](../CONTRIBUTING.md)（判据规矩、能/不能在本机跑什么）、
[ROADMAP.md](../ROADMAP.md)（现在做什么 / 等触发条件 / 明确不做）、
[SECURITY.md](../SECURITY.md)（安全问题走私密通道）、[CHANGELOG.md](../CHANGELOG.md)（对外行为与判据的变更）。

## ⚠️ 跨仓引用契约：这四组编号/标题**不许动**

父仓（`/home/ubuntu/Saudade-Blog`）的文档与迁移脚本**按编号/标题**引用本仓这几份文档。
改标题、重排编号会让那些引用**静默指错**（没有任何判据会红——这正是要把它们写下来的原因）：

| 本仓的锚点 | 父仓引用它的地方 |
|---|---|
| [问题记录.md](问题记录.md) **§2.1** | `docs/iot-device-integration.md`（两处）、`iot/firmware/固件开发指南.md` |
| [identity-and-permissions.md](identity-and-permissions.md) **§3.4 / §3.6 / §5.2 / §5.3** | `docs/security-boundary.md`（五处）、`scripts/migration/zako_role_20261002.sql` |
| [agent-architecture.md](agent-architecture.md) 的标题 **《3. 一次对话的完整链路》** | 父仓 `README.md`（按标题引） |
| [toolcall-stability-roadmap.md](toolcall-stability-roadmap.md) 的 **D1–D6** | `scripts/migration/execution_log_struct_20260927.sql`（引 `§D2`） |

`tests/test_docs_links.py` 会核对这四组锚点仍在（改了就红，提醒你先去父仓同步）。

> 顺带纠正一条**曾经被记错**的说法：`agent-architecture.md` 的**其它**章节编号**不是**跨仓契约
> ——父仓只在两处提它（一处按文件名、一处按上面那个标题），代码注释里一处都没有。
> 唯一被引的章节标题就是上面那一行。
