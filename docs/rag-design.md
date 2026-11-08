# RAG 文章检索与证据读取

当前契约由版本化语料快照、结构感知检索、正文范围读取和证据检查组成。
组件边界见 [架构说明](agent-architecture.md)。

## 1. 数据与权限边界

检索池只收公开文章。留言、说说、公告、本人数据与后台数据使用各自工具。
宿主的 public_condition 同时约束公开状态、is_public 和非修改稿；
Agent 不持有后台凭据扩充语料。正文每次经公开详情接口重新核对可见性。
首页向量词图是另一条展示/搜索链路，不是回答阶段的正文证据。

## 2. 版本化语料协议

GET /api/public/rag/corpus 接受 limit、after、revision。
首请求不带游标，后续请求携带 next_cursor 和同一 revision。

| 成功信封 data 字段 | 含义 |
|---|---|
| schema_version | 协议版本 |
| revision | 整个公开语料集合的内容指纹 |
| total | 同版本的文章总数 |
| documents | 每篇 id、title、content、revision |
| next_cursor | 下一页游标；末页为 null |

每页在 PostgreSQL repeatable-read、read-only 事务中取得页和摘要。
指纹覆盖公开集合成员、标题及正文，不依赖写方更新 updated_at。
分页期间内容或可见性变化返回 HTTP 409，数据库故障返回 HTTP 503，
不能将故障包装成成功空列表。接口不增加阅读量、不返回作者内部身份。

rag/corpus.py 校验版本、内容摘要、总数、严格递增 ID、页大小和游标。
完整且同版本的全部页面才能提交索引。没有旧列表接口的隐式回落：
先部署宿主新增接口，再部署 Agent。

## 3. 刷新与陈旧数据

RagIndex 使用单次构建锁，在短锁内替换完整视图。失败保留上次成功版本，
记录错误并退避重试；超过最长陈旧窗口返回 unavailable，正常空语料返回 empty。
周期与窗口以 rag/search.py 为准。工具 meta 记录版本、最近成功时间、stale 和错误；
候选文本也标注使用了旧完整索引。

正文读取仍实时核对公开可见性；索引标题元数据有刷新窗口，不声称即时撤回。
读取反馈的版本不匹配会使相关缓存失效，构建期间的失效信号也不能丢失。

## 4. 切片与词法检索

rag/sections.py 使用 markdown-it-py 的 CommonMark block token 与 source line map，
识别顶层 ATX/Setext 标题；围栏、缩进代码和 HTML 中的类标题，以及引用/列表内局部标题，
均留在所在正文中。应用只维护分节树、空父标题、层级路径和读取视图版本内的小节 ID。
正文和后代范围直接切原始字符串，保留 CRLF/CR 和 Unicode，不经渲染文本找回偏移。
重复名称/编号不能默认取首个。

rag/chunks.py 使用 LangChain 的 RecursiveCharacterTextSplitter 在小节内切片，
按段落、行和中英文边界配置限制字符预算并保留重叠，
记录 chunk_id、section_id、标题路径和相对小节正文的范围。
词法与向量均使用文章标题、路径和正文，标题不再因跨过长度阈值而丢失。
字符预算是模型无关的输入上限，不宣称等于供应商的精确 token 数。
可选 embedding profile 只在向量侧细分超 token 预算的片段，完整计算标题、路径及
指令，保留小节源范围；配置损坏仍可走词法，见 [输入预算说明](embedding-inputs.md)。

词法通过 scikit-learn CountVectorizer 的公开 analyzer 生成 CJK 2/3-gram 与英文数字词元；
应用只配置语言边界，n-gram 生成交给该 analyzer。查询词元去重。`rag/lexical.py` 是 BM25S 的薄适配，
索引、词频统计、长度归一与评分交给锁定版本的库；参数来自 BM25_K1、BM25_B。
使用 Lucene 方法、float64 和 NumPy 后端，构建后只读，随完整语料快照原子替换。
状态与评测报告记录库版本和评分方法。原生分数不作为置信度；
原分只用于本路排序，候选由固定 top_k 限制，RRF 按相对名次融合。
两路共用 rag/ranking.py 聚合文档，并提供可回取的命中位置。
查询保留原始问句；不删除疑问词、不按头部分数裁掉尾候选。
少量领域别名映射属于博客词表配置；候选与实际答案证据仍分开判断。
没有独立 LLM 查询重写器或 cross-encoder 重排模型；RRF 是名次融合。
新增改写或重排须用固定数据分别比较召回、排序、延迟和费用，不能用新调用数量代替收益。
默认候选数由 rag.DEFAULT_TOP_K 定义，工具与固定语料评测共用。

## 5. 向量与持久化

RAG_HYBRID_ENABLED 控制可选的向量融合；
只融合可用、与语料和空间一致的视图，故障明确退回词法并记录原因。

空间包含模型、端点、请求维度、分块策略与可选 profile 签名，内容键覆盖实际发送的
embedding 文本。查询 LRU 同时校验空间与实际维度；拒绝非有限值、零向量、错误批次
index 和不一致维度。未声明请求维度的模型若实际维度变化，必须重新构建完整视图。

持久化使用 PostgreSQL 的独立 `rag` schema；`RAG_VECTOR_DSN` 可以指向现有业务数据库，
`RAG_VECTOR_COLLECTION` 隔离不同公开语料。SQLAlchemy Core 管理有界连接池、事务和绑定
参数，官方 pgvector 类型负责向量编码与 `<=>` 余弦距离。运行时不自动建扩展或表；
显式初始化、授权和切换步骤见 [部署](deployment.md#向量存储)。

数据库保存内容向量、当前 corpus membership 和空间/指纹状态。embedding 在数据库事务外
完成；只嵌新增或变化的唯一输入。成功片段可缓存供失败重试，但只有完整覆盖的构建才能在
一个短事务中替换 membership、发布状态并清理集合内已删除/私密化及旧空间的向量。
事务异常保留原完整快照。session advisory lock 协调写方，在模型等待期间没有长事务；
读取以短 repeatable-read 事务核对语料指纹，不能将旧向量与新正文片段拼接。
正文与候选的公开性仍须由宿主实时复核，数据库快照不代替授权。
语料 revision 当前是内容 hash，不提供跨进程单调顺序；较早抓取但较晚提交的构建可能
暂时覆盖新 fingerprint。查询按当前词法指纹拒绝混用，回退 BM25 后重建，而不宣称 writer
互斥锁提供来源版本 fencing。彻底避免这段可用性回退需要宿主单调版本或提交前的轻量版本校验。

当前规模使用 pgvector exact search；数据库先计算片段余弦距离，按文章最佳片段聚合后
取文档 top_k，再返回这几篇的证据位置分数。不把片段 top_k 当成文章 top_k，不把全部
向量拉回 Python 算距离。不默认建立 ANN 索引；只有规模、延迟和过滤后召回实测需要时
才比较 HNSW/IVFFlat，并重新验证召回与证据覆盖。

每个进程的向量池最多4个连接、无 overflow，获取连接等待与SQL有独立上限；一个构建者
会在模型等待期间占用一个 session-lock 连接，部署多 worker 时须计入数据库总预算。
向量库故障或未配置 DSN 明确降级 BM25。旧文件缓存不再参与读取；首次迁移要按公开语料
重新生成，先估算费用，原文件由维护者确认新索引验收后清理，不自动删除回滚材料。

RRF 排名交给 LangChain 官方 EnsembleRetriever.weighted_reciprocal_rank，
仅合并博客来源位置，不复制其评分算法。该公开接口只返回顺序，混合候选因此提供 rank，
不伪造 score；词法、向量单路的原生分仅用于各路排序和截断，工具出口统一显示排名。
langchain-classic 只提供此 retriever，不装配第二套运行时。
RRF 是相对排名，不是相关性置信度。启用前比较固定语料上的命中、噪声、证据获取、
回答支持度与延迟/成本；文章篇数不能单独证明收益。

## 6. 正文、小节和范围

get_article_detail 增加 offset、max_chars、revision，返回 contentRevision、
readOffset、nextOffset、totalChars、readComplete、source 和小节 ID。
续读范围或按小节 ID 读取必须带版本，防止编辑后跨版本拼接。
按标题/编号只接受唯一匹配；父节读取包括子节。
contentRevision 是包含解析策略及库版本、标题和原文的读取视图 hash；它与宿主语料传输的
内容 revision 分开。检索候选 document_revision 使用相同读取视图版本，客户端只需原样回传。
解析策略变化会明确拒绝旧 sN/续读版本；首次全文读取可取得当前版本与目录。

无结构文章和超长单节也能连续读到尾部。偏移以选定正文为准，
切片记录与按节读取一致。预算渲染保持可解析数据，并把 nextOffset/source.end
调整到模型实际看到的正文末尾；剩余预算不足时不登记正文证据。

## 7. 运行时证据规则

文章能力运行于现有 LangChain Agent，通过
[ArticleMiddleware](../agent/products/blog/articles.py) 的公开 middleware hooks 观察工具结果、
处理最终回复，并在恢复时读取已保存的证据快照。工具选择与后续读取由 Agent 决定。

[ArticleEvidence](../agent/domains/articles/evidence.py) 分别记录公开搜索候选、
模型实际可见的非空正文范围，以及已读正文提到的文章链接。正文先按本轮预算渲染，
再登记可见字符、revision、小节及范围；到达选定范围末尾不等于读完全文。
候选与其中的真实链接可用于文章推荐，带来源的可见片段可作为局部工具观察，
但这些观察不登记为正文读取，也不证明看过整篇或能支持任意正文结论。

最终回复的站内文章引用必须来自已知候选、已读文章或已读正文提到的本站链接；
未知文章及错误站点引用会被阻止。成功读取正文后，若回复遗漏相应来源，
中间件追加真实已读文章链接。RAG 搜索的 empty 与服务 failed 分别记录，
无可核实结果时使用不同说明。

这些检查验证链接出处和读取范围。内容相关性、结论支持度、忠实度及提示词注入风险
仍由语义评测与端到端验收判断，不能从候选分数、链接合法或成功读取推导答案正确。

## 8. 评测与验收

确定性回归覆盖分页故障/版本变化、空语料、标题独有词、长度统计、并发构建、
重复小节、无缺口范围续读、缓存每阶段中断、同长损坏、空间切换和候选直答。

报告携带整库及逐篇内容摘要、查询/标注摘要、切片版本、检索配置、embedding 空间摘要
和检索代码摘要。同 ID/篇数不代表相同语料。历史 recall@k 字段表示至少一个正确文档
进入前 k，应按 Hit@k 解读，不作为多文档完整覆盖率。live 评测用于运维，固定语料用于回归。

运行 python -m pytest tests/test_rag_contracts.py tests/test_rag_evidence.py。
宿主包含 rag_corpus 单元测试和 TEST_PG_URL 指向的一次性 PG 集成测试。
真实回答质量与 embedding 收益需要真实模型凭据，不能用桩结果冒充。

固定离线检索评测及外部模型对照入口见 [固定语料评测](../eval/offline-rag.md)。

## 9. 设计依据

- [Lucene BM25Similarity](https://lucene.apache.org/core/9_9_0/core/org/apache/lucene/search/similarities/BM25Similarity.html)：平均长度采用总词频/文档数。
- [Azure RAG chunking](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-chunking-phase)：按结构和预算切分并验证效果。
- [Lance table format](https://lance.org/format/table/)：不可变文件与版本化 manifest 的提交思想；本项目没有引入 Lance 依赖。
