# RAG 成熟库迁移与专项验证

本次将通用算法交给已有生态中的成熟库；公开语料、权限边界、小节 ID、原文范围、
文档级聚合与增量持久化继续由博客适配器负责。没有增加检索服务或第二套 Agent 运行时。

| 环节 | 实现 |
| --- | --- |
| 词元/gram 生成 | scikit-learn CountVectorizer.build_analyzer，CJK 字符 2/3-gram、ASCII 词元 |
| 小节内字符切片 | LangChain RecursiveCharacterTextSplitter，保留分隔符、空白及 start_index |
| embedding 超预算窗口 | Hugging Face Tokenizers Encoding.truncate/overflowing，以原始 offsets 回取正文 |
| BM25 | 保留 BM25S 的公开索引与评分接口 |
| 混合排名 | LangChain EnsembleRetriever.weighted_reciprocal_rank，等权与原 k 配置 |
| 向量相似度 | SciPy cdist(metric="cosine")，只读 float64 矩阵，零行相似度为零 |

中文/ASCII 边界、同义词及候选宽度仍是应用策略，不宣称是库默认。
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
保留旧目录可供旧版本回滚。本次专项只读生产索引和公开语料，没有重建或写入生产索引。

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
