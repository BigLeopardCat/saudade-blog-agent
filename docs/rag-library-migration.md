# RAG 成熟库迁移与专项验证

本次将通用算法交给已有生态中的成熟库；公开语料、权限边界、小节 ID、原文范围、
文档级聚合与增量持久化继续由博客适配器负责。没有增加检索服务或第二套 Agent 运行时。

| 环节 | 实现 |
| --- | --- |
| 词元/gram 生成 | scikit-learn CountVectorizer.build_analyzer，CJK 字符 2/3-gram、ASCII 词元 |
| Markdown 语法 | markdown-it-py CommonMark block tokens 与原始 source line map |
| 小节内字符切片 | LangChain RecursiveCharacterTextSplitter，保留分隔符、空白及 start_index |
| embedding 超预算窗口 | Hugging Face Tokenizers Encoding.truncate/overflowing，以原始 offsets 回取正文 |
| BM25 | 保留 BM25S 的公开索引与评分接口 |
| 混合排名 | LangChain EnsembleRetriever.weighted_reciprocal_rank，等权与原 k 配置 |
| 向量相似度 | SciPy cdist(metric="cosine")，只读 float64 矩阵，零行相似度为零 |

中文/ASCII 边界与领域别名是应用配置，不宣称是库默认；查询保持原文，候选使用固定 top_k。
langchain-classic 只提供上述官方 retriever；现有 LangChain/LangGraph 运行时不变。
HF Tokenizers 仅修改每次 encode 得到的独立 Encoding，不改变共享 Tokenizer 的截断配置。

官方 RRF 接口只给排序，不暴露融合分值。混合行删除 score、增加从 1 开始的 rank；
词法与向量内部仍有其真实原分用于本路排序/截断。rag_search 工具统一显示 rank，越小越靠前，
不把排名当置信度。合并的来源范围、文章类型、ID、版本和小节定位保持原契约。

## 缓存迁移

空间、内容键和查询缓存包含分块策略签名；签名覆盖字符预算、重叠、分隔符和切片库版本。
profile 签名同时更新 token 窗口策略。缺少签名的旧 schema 2 索引拒绝作为新视图使用。
这是一次空间迁移，需要为新版本准备一次新索引；同一新空间之后仍只嵌入新增/变化内容。

发布时先在独立向量目录完成现有 prepare 的显式计划、预算检查和构建，再切换版本与目录。
保留旧目录可供旧版本回滚。已在独立v4目录完成显式计划与223输入构建，验证后切换生产目录，未覆盖旧索引。

## 2026-11-02 验证记录

[脱敏报告](../eval/report/baseline_rag_library_migration.json)以 57246e5e 为算法基线。
使用当时公开的 9 篇文章，以及 2026-10-27 已固定的 16 条站内开发查询和原标注，
未修改黄金用例、语料正文、权限或门槛。全部标注文章仍在本次公开集合中。

| 对照 | Hit@1 | Recall@3 | nDCG@10 |
| --- | --- | --- | --- |
| 原词法 | 15/16 | 1.0000 | 0.97582 |
| 新字符切片与库 analyzer 的词法 | 15/16 | 1.0000 | 0.97582 |
| 原混合，冻结原 BGE 输入/向量 | 15/16 | 1.0000 | 0.98084 |
| 官方 RRF，使用同一批冻结候选 | 15/16 | 1.0000 | 0.98084 |

官方 RRF 的 16 题完整候选顺序均与旧实现一致。相同真实向量的 SciPy/旧余弦最大绝对差
为 6.7e-16。新字符切片为 183 个（原 181），新 BGE 输入计划为 224 个（原 227）。
**没有对新切片全量重新嵌入，后两行不证明新切片的完整混合质量。**
这些是小型开发集及冻结向量的替换回归，不是留出集、官方榜单或最终回答正确率。

单机 4 vCPU 上交替执行 50 次相同 227×512 向量扫描，中位墙钟耗时从 15.782ms
变为 0.322ms；进程 CPU 时间从 15.779ms 变为 0.324ms。相同旧字符切片上各清除
范围缓存 5 次，token 预处理中位从 610.73ms 变为 204.45ms。
这些是纯 CPU 微基准，不含模型、HTTP、Agent 或证据读取，不代表端到端提速。
SciPy 的该距离内核不调度 BLAS 矩阵乘法线程，没有调整服务并发或本地 BGE 的单线程单槽。

真实 BGE 仅使用临时目录验证一份 1,804 字符长中文输入：6 个完整输入的 token 数为
512/512/512/512/246/209，全部返回 512 维、missing=0；原文覆盖、Unicode 和尾部完整。
第二次原样更新 embedded=0、API 调用=0。该项为功能验证，不作检索质量评分。

依赖锁增加 7 个包，其余 NumPy/SciPy 等复用原依赖栈；新增文件逻辑体积约 35.2MiB，
本机含字节码与文件系统分配的磁盘体积约 49MiB。独立检索进程的首次导入约增加 1.1 秒，
建词法索引后的 RSS 从约 75.8MiB 变为 144.3MiB、native 库线程由 5 变为 8。
四 worker 对应约 274MiB 的进程 RSS 增量估算，部署时需保留余量；它不是整站容量验收。

离线行为/负向判据覆盖原文逐字符范围、重复内容、非法/零/反向向量、维度拒绝、
分块空间改变后旧视图拒绝、查询缓存分区、增量零调用、来源合并、同名不同文章，
以及 4 线程同时切片时共享 tokenizer 配置与完整计数不变。

参考接口：[CountVectorizer](https://scikit-learn.org/stable/modules/generated/sklearn.feature_extraction.text.CountVectorizer.html)、
[LangChain 字符切片](https://docs.langchain.com/oss/python/integrations/splitters/recursive_text_splitter)、
[EnsembleRetriever](https://reference.langchain.com/python/langchain-classic/retrievers/ensemble/EnsembleRetriever)、
[SciPy cdist](https://docs.scipy.org/doc/scipy/reference/generated/scipy.spatial.distance.cdist.html)。
Tokenizers 接口以锁定的 0.22.2 实际安装代码及相应回归为准，未依赖最新预发布文档中的 API。

## 标准 Markdown parser 追加

通用标题、围栏、缩进代码及 HTML 语法交给 markdown-it-py 4.2.0。库返回的行范围直接映射
原始 CR/LF 字符串；仅顶层标题进入文章分节，引用与列表内部的局部标题保留在正文。
正文及父节的后代读取直接切原文，保留原始标题标记、换行和 Unicode。

读取视图的 contentRevision 同时覆盖 parser 策略/库版本、标题和原文；宿主 corpus 的传输
内容 hash 不变。检索候选与读取工具共享视图版本，旧 checkpoint 中的 sN/游标版本明确过期。
parser 策略也进入 v4 分块空间，发布计划须以 v4 代码重新计算，不能使用 v3 输入计划代替。
上节开发集与 CPU 数字记录的是 v3 替换；parser 追加的离线对照见其独立验证记录。

融合通过官方 EnsembleRetriever.invoke 承接预计算的两路候选，库内执行其公开 RRF。
两个 Runnable 适配器只返回已有列表，不重新查询、发模型、执行工具或拥有调度/重试循环；
它们属于该公开 retriever 的输入适配，没有增加 Agent 执行器。

查询策略已收敛为原始查询与固定 top_k，领域别名保留为 key/val/why 的小型配置。
CLIFF、手工疑问词删除及对应实验 API、探针和夜间入口已经退役，不保留空兼容开关。
完整新索引上的[固定查询消融](../eval/report/baseline_rag_query_simplification.json)说明取舍：

- 16 题 hybrid 的 Hit@1=15/16、Recall@3=1、nDCG@8=0.980839，简化前后相同；
  候选具体顺序仍有变化，不能把指标相同当作最终回答等价。
- 词法 fallback 的 Recall@3 从 1 降至 0.96875，nDCG@8 从 0.975822 降至 0.973165；
  agent_intro 中文章 62 离开前 3。接受这项明确的词法取舍，未改标注或融合权重。
- 16 题均不含别名 key，不能由零变化宣称别名无用。既有词法查询显示 JWT、断连、OTA
  的当前召回收益；现有“RAG测评体系怎么建立？”中别名使目标文章 19 从第 6 升至第 3。
  这些是站内术语别名，并非自建通用分词、语义改写或排名算法。

参考：[markdown-it-py 配置与 token](https://markdown-it-py.readthedocs.io/en/latest/using.html)、
[Token source map](https://markdown-it-py.readthedocs.io/en/latest/api/markdown_it.token.html)。

parser 追加的[脱敏验证记录](../eval/report/baseline_commonmark_parser.json)：固定语料最终为
182 个词法切片、223 个 BGE 输入计划；16 题词法指标与 v3 一致。119 项扩大专项和
28 项额外来源/运行时证据/探针测试通过。完整新索引的混合排序验证见上方消融记录，最终回答另见[P4回归](p4-completion-report-20261103.md)。

相同 9 篇语料各运行 7 次进程内热词法重建，中位耗时从 85.68ms 变为 127.28ms；
该开销包括标准 Markdown 解析和读取视图摘要，未计导入、HTTP、模型或完整聊天。
markdown-it-py 4.2.0 已在原依赖锁中，本次只显式声明直接使用，没有新服务。
