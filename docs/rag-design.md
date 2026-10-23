# RAG 文章检索与证据读取

当前契约由版本化语料快照、结构感知检索、正文范围读取和证据检查组成。
决策见 [ADR-0017](adr/adr-0017-versioned-rag-evidence.md)；旧实验和基线保存在 Git 历史。

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
判断“是否该查站内资料”不进行 I/O，也不依赖语料源是否可用。

## 4. 切片与词法检索

rag/sections.py 提供共享 Markdown 边界和 outline：识别围栏外 1–6 级标题，
保留空父标题、层级路径和文档版本内的小节 ID。重复名称/编号不能默认取首个。

rag/chunks.py 在小节内部继续按段落/行切片，限制字符预算并保留重叠，
记录 chunk_id、section_id、标题路径和相对小节正文的范围。
词法与向量均使用文章标题、路径和正文，标题不再因跨过长度阈值而丢失。
字符预算是模型无关的输入上限，不宣称等于供应商的精确 token 数。
可选 embedding profile 只在向量侧细分超 token 预算的片段，完整计算标题、路径及
指令，保留小节源范围；配置损坏仍可走词法，见 [输入预算说明](embedding-inputs.md)。

词法使用 CJK 2/3-gram 与英文数字词元。BM25 的 dl、avgdl 都使用 token 总数，
查询词元去重；参数来自实现的 BM25_K1、BM25_B。
两路共用 rag/ranking.py 聚合文档，并提供可回取的命中位置。
候选断崖保留既有前 K 名保护，只控制供给宽度，不证明文档有答案。

## 5. 向量与持久化

RAG_HYBRID_ENABLED 和 AGENT_ARM 是独立开关。向量保持可选；
只融合可用、与语料和空间一致的视图，故障明确退回词法并记录原因。

空间包含模型、端点和请求维度；内容键覆盖实际 embedding 文本。
启用 profile 时还包含固定 tokenizer、输入指令、预算与模型版本等语义配置的签名。
查询 LRU 按空间及预处理后的查询分区，命中仍校验维度。
拒绝不等长点积、非有限值和错误批响应 index；维度变化时重新构建完整视图。

持久化 schema 2 使用内容寻址的不可变 f32/JSON 文件。
写方先 fsync 文件及目录，最后原子替换 manifest；读方核验文件摘要。
跨进程 flock 协调写方，读方不依赖写锁。活跃/最近代保留，过期孤儿按实现清理。

旧 schema 1 的固定文件不能证明未发生错位，因此不导入，首次启用会重新嵌入；
升级前应估算一次全量 embedding 成本。旧文件不自动删除，可作回滚材料。

RRF 是相对排名，不是相关性置信度。启用前比较固定语料上的命中、噪声、证据获取、
回答支持度与延迟/成本；文章篇数不能单独证明收益。

## 6. 正文、小节和范围

get_article_detail 增加 offset、max_chars、revision，返回 contentRevision、
readOffset、nextOffset、totalChars、readComplete、source 和小节 ID。
续读范围或按小节 ID 读取必须带版本，防止编辑后跨版本拼接。
按标题/编号只接受唯一匹配；父节读取包括子节。

无结构文章和超长单节也能连续读到尾部。偏移以选定正文为准，
切片记录与按节读取一致。预算渲染保持可解析数据，并把 nextOffset/source.end
调整到模型实际看到的正文末尾；剩余预算不足时不登记正文证据。

## 7. 运行时证据规则

默认图在 RAG 候选后试图空工具收尾时先产生正文读取计划。
重复检索的补读允许有限探索未读候选，不把最高检索分当作答案充分性，
也不跨 query 比较 BM25 原分来禁止补读，补读直接定位命中小节与范围。

可选 hybrid 运行时记录成功读取的非空正文。候选直答交回默认图；
搜索为空和服务失败使用不同的确定性答复。引用来自实际读过的文章，
缺少引用时追加真实来源。普通聊天和目录工具保持各自路径。

成功读取是必要条件，不是语义正确性的证明。相关性、忠实度和提示词注入
仍需端到端评测，不宣称程序检查消除了全部幻觉。

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
