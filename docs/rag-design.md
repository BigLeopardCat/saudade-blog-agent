# RAG 检索增强问答：架构设计与实现总结

> 博客看板娘 agent 的 RAG 能力（2026-08-30 落地）：访客问博客内容（"Git 和 SVN 有什么区别"
> "ESP32 的 OTA 怎么配置""留言板里有人聊过 RAG 吗"），agent 从线上可见语料检索定位、
> 精读全文后作答。
> 本文是 RAG 管线的设计记录：架构 → 选型 → 实现 → 工作流 → 问题与解决 → 评测 → 升级预案。
> 正文为 20260830 初稿的历史形态，机制现状以文首各变更注为准；检索现状另见 rag/search.py
> 头注释（查询侧停用词剔除/同义扩展）与 eval/recall_eval.py（22 条 queries 基线）。

> 20260903 架构变更注（planner 全权，正文保留为历史设计记录）：20260903 起
> **自由 ReAct 与 reflector 已废除**，执行链为 planner ⇄ execute → model → gate（见
> `agent-architecture.md` §3/§6.5）。本文正文及上文各变更注中依赖"执行层自由决策"的旧路径
> ——rag_query 技能两段式模板、执行层自选检索工具（rag_search/search_notes → 精读全文）、
> reflector 检查点核验与 REVISE 打回——均为历史记录。现行 content_query 由 planner
> 唯一决策，逐轮给调用清单（PARAMS.tools/calls 白名单点名，检索定位 → 看帧 → 读全文/换词再搜/
> 收尾），execute 确定性执行。"检索管定位、解读管精读"的行为纪律与 §3 技术选型（索引粒度/
> 评分/语料可见性）不变，检索实现仍直接测线上 rag/search.py。

> 20261005 变更注：**向量 + RRF 混合检索已落地**（`rag/vector_index.py` +
> `rag/search.py` 的编排层），但**出厂默认关**（`RAG_HYBRID_ENABLED`）——关掉即本文
> 正文描述的纯词法形态，逐字节不变。触发条件（语料长大 / BEIR 对比显示词法掉点）没达到，
> 所以交付的是能力与开关，不是换主力。另：向量模型的端点/密钥**独立配置**，与对话模型
> 无关（供应商不进代码）。完整落地形态、增量索引口径与"想拿公开数据集量效果"的注意事项见
> **§9**；§3/§8 里"向量留作升级预案"的表述以 §9 为准。

> 定位变更（20260901/20260902，正文保留为历史设计记录）：本文正文描述的是
> 20260830 的 rag_query 技能两段式设计——20260901 定位重构后已废除（见
> [问题记录 1.32](问题记录.md) 前置部分与 `agent-architecture.md` §6.5）：
> 1. 20260901 定位重构（RAG 定位错了）：把说说/留言拉进检索语料是污染（碎碎念无参考
>    价值）。RAG 的正确用法是**文章检索的前置任务**；把它当作一种意图类型（"除了 chat 就是
>    rag_search"）是错误路由。rag_query 技能废除，检索池只收文章；content_query 扩容承接
>    全部内容查询：知识型 → 执行层自由 ReAct 自选 rag_search/search_notes 定位 +
>    get_article_detail 精读全文（"检索管发现，工具管精读"不变，正文 §2 的架构原则仍成立）；
>    数据/列表型 → list_guestbook/list_talks 等数据工具直查，不走检索。
> 2. 20260902 planner 显式点名工具：查"留言板/说说里有没有人聊过/写过 X"由 planner
>    PARAMS.tools 显式点名（白名单 _EXPLICIT_TOOLS，双源必须成对），经 TOOLS 行强制 +
>    reflector 逐工具核验——根治"planner 对、model 零工具编造"（233815）。
> 3. 检索评测口径更新：recall_eval 现 21 条 queries = 12 条 recall 正例 + 9 条噪声
>    （20260901 语料净化后 rag_talk_rag 移出 recall 仅留 golden 端到端覆盖、
>    rag_fingerprint_pin/crc 转噪声、rag_eval_system 回流）；直接测线上 rag/search.py，
>    词法基线 recall@1=1.00 仍成立。

---

## 1. 背景与目标

博客内容问答是看板娘的高频需求，但 agent 的 LLM 不掌握站点私有内容（文章/说说/留言/公告）。
目标：**访客问博客内容时，agent 检索真实语料、基于全文回答，不编造**。
约束（生产环境决定）：单机 3.7GB 内存、agent 无 DB 依赖（架构原则）、语料必须与前台可见性
严格一致（agent 不应答出访客看不到的内容）。

## 2. 架构：路线 B（agentic RAG，retrieve-then-read 两段式）

主流"管道式 RAG 直答"（检索 top-k chunk 直接拼 prompt）快，但 chunk 断裂、无法精读全文，
不采用。本设计里**检索只负责定位，解读永远走工具精读全文**：

```
用户问题
  → [检索] rag_search(query)        只返回候选：type/id/标题/命中小节/分数，不含全文
  → [解读] get_article_detail(id, doc_type)   精读候选全文（note/talk/board/announcement）
  → [回答] 基于全文作答，无候选/无关时诚实拒答
```

- 索引粒度 ≠ 读取粒度：索引按 markdown 小段切 chunk（召回精）；读取用全文（解读准，不被切片污染）。
- 引用可追溯：回答可关联具体文章/说说 ID（trace 记录工具轨迹，四端可对账）。
- 两条硬纪律由系统强制，不靠模型自觉：
  - 两段式 TOOLS 行（技能模板）：TOOLS 行固定 `rag_search` + `get_article_detail`，
    reflector 检查点强制两段都出现在工具轨迹中，缺失即 REVISE——堵"只检索不读全文"。
  - 诚实拒答：检索无结果、或候选与问题无关时如实告知，回复契约明确"不得编造"。

## 3. 技术选型（每个选择都有数据/约束支撑）

| 决策 | 选择 | 理由与备选 |
|---|---|---|
| 检索算法 | 词法 2/3-gram 子串匹配 + BM25 | 检索 eval 实证 recall@1=1.00 **打满当前语料**（当时 34 文档含说说/留言；20260901 净化后仅收公开文章，现 12 篇＝20261005 实测）。纯 2-gram BM25 只有 0.43（gram 太碎、idf 失效）；词法基线先上（红牌清单"先 top-k 基线"），向量 + RRF 20261005 已按开关落地（默认关，见 §9）|
| 中文分词 | CJK 连续段拆 2/3-gram（子串匹配近似） | 不引 jieba/词向量——3.7GB 机器不跑本地 embedding；2/3-gram 覆盖 2 字词与 3 字词的共现，混合语料（中文博客+英文技术词）按 `[一-鿿]+|[a-zA-Z0-9_\.]+` 分型 |
| 存储 | 内存倒排索引（纯 Python dict） | 语料规模小（全量重建 <100ms），不做增量；线程安全（锁 + 原子替换），重建失败沿用旧索引。不引 sqlite-vec/向量服务（万级语料才需要考虑） |
| 语料源 | Rust 公开 API（/api/public/*） | agent 保持无 DB 依赖架构；可见性过滤由 Rust 层保证（is_public=1 AND status!='draft'，talk/board approved=1）——与前台严格一致，**draft/private 不进语料** |
| 刷新 | 10 分钟懒刷新（TTL 600s） | 全量重建幂等且便宜，不做增量/CDC；博客写少读多，10 分钟延迟可接受 |
| 工具化 | 技能注册表两段式（rag_query 技能） | 复用既有"技能模板 + reflector 质检"体系（与导航/特效同构），TOOLS 行强制序列依赖 |

## 4. 实现（rag/search.py）

```
RagIndex
├── build()      _fetch_corpus()（走 Rust API，note 逐篇拉全文）→ chunk_note()（markdown 标题切分，
│                >2000 字才切）→ tokenize() → 倒排 postings + 每 chunk TF + idf + avgdl → 锁内原子替换
└── search(q)    TTL 懒刷新 → tokenize → 命中的 gram 逐 chunk BM25 打分
                 → 文档级聚合（by_doc 取最高分 chunk + sections 汇总）→ 排序截 top_k
```

- BM25：k1=1.2, b=0.75，idf 用 `log(1 + (N - df + 0.5) / (df + 0.5))`（平滑零概率）。
- 文档级聚合是核心细节：索引粒度是 chunk，候选粒度是文档（解读走全文）——
  chunk 分数直接排序会让长文刷屏 top-k，聚合后每文档一个候选、附带命中小节定位。
- 工具层（tools/base.py）：`rag_search`（调 search()，返回候选 JSON）；
  `get_article_detail` 泛化 `doc_type`（note/talk/board/announcement），
  talk/board/announcement 无单条端点，从列表接口按 key 过滤（列表已带全文）。
- 技能层（agent/skills.py）：rag_query 技能定义两段式 plan；实例化时第二段参数填
  占位说明文本（"从 rag_search 返回结果中取最高分候选"），模型自行填 id/type——
  reflector 检查点据 TOOLS 行强制两段都执行。

## 5. 工作流程（一次完整问答）

```
"留言板里有人聊过 RAG 的本质吗？"
  → planner：分类为知识型问题 → 选 rag_query 技能，PARAMS.query="RAG 本质"
  → 实例化 TOOLS 行：rag_search({"query":"RAG 本质"}) ; get_article_detail({...占位})
  → executor：调用 rag_search → 候选 [{type:"talk", id:23, title:"2026-8-6", score:6.1}, ...]
  → executor：从候选取最高分 → get_article_detail(article_id=23, doc_type="talk") → 留言全文
  → executor：基于全文作答（"说说里有一条专门讨论 RAG 本质的碎碎念……解耦、参数化……"）
  → reflector：对照模板查 TOOLS 行两段都在轨迹中 → VERDICT PASS
  → 前端渲染；trace 落盘（planner/model/tools/reflector 分段耗时可查）
```

## 6. 评测体系（评测驱动，先立验证再动工）

- L1 检索 eval（eval/recall_eval.py）：**直接 import 线上 rag/search.py 的 search()**
  ——评测即线上实现，不另写模拟实现（防"评测绿、线上烂"）。22 条 queries 与 golden
  rag_* 用例同源出题（13 条 recall 正例 + 9 条噪声：20260901 语料净化后 12+9，
  20260920 回流 rag_arch_ports_real 成 13+9），报告 recall@1/@3/@5 + MRR +
  noise_hit_rate + **mean_candidates** + 本次生效档位 → eval/report/runs/。
  实证：词法基线 recall@1=0.92（13 条正例里 1 条是点名留档的已知 FAIL
  `rag_arch_ports_real`，rank=2）、recall@3=1.00、MRR=0.96、平均候选 3.36
  （top_k=5；20261005 实测，语料 12 篇）。**基线是这三个数加这条 known_fail，
  不是"全 1.00"**——照旧口径读会把已知 FAIL 当成回归。
- L2 端到端 golden（现 22 条 rag_* 用例）：recall 正例（从文章出题，断言知识词命中）+
  noise 组（语料外问题，断言诚实拒答），与导航/特效等用例同池，全量 golden 66 条
  （20260905 判据改写后 66/66、0 resets 基线，留档 eval/report/runs/20260905_195300.json）。
- trace 可观测：每轮对话落 JSON trace（工具序列 + 分段耗时），RAG 失败可直接
  读 trace 归因（模型没调工具？检索没命中？读错候选？）。

## 7. 遇到的问题与解决（浓缩版，详见 docs/问题记录.md 1.19-1.25）

| 问题 | 根因 | 解决 |
|---|---|---|
| 中文检索零命中 | CJK unigram 被 `len>=2` 过滤 | 拆 2/3-gram，recall 0.79→1.00 |
| 纯 2-gram BM25 只有 0.43 | gram 太碎、idf 失效 | 2/3-gram 混合 + 词法基线定论；向量留 BEIR 对比 |
| 语料构建三处问题 | status 实际是 'public'；talkKey serde 改名；列表接口空正文 | 排除 'draft'；talkKey 字段；note 逐篇拉详情 |
| 长文刷屏 top-k | chunk 级评分违背文档级候选语义 | 文档级聚合（最高分 chunk + sections） |
| 模型只检索不读全文 | 两段序列依赖无强制 | TOOLS 行两段式 + reflector 检查点强制 |
| talk 候选无法读全文 | get_article_detail 只支持 note | 泛化 doc_type，从列表接口按 key 过滤 |
| 单条概率波动 | LLM 随机性跳过工具 | 归因为波动（重跑 PASS），接受，reflector 概率性兜底 |

## 8. 升级预案（触发条件驱动）

已按触发条件落地/随架构裁决废除的行不再列（查询侧通用词剔除 = 原 P1"BM25 轻量改进"，
20260905 以 _QUERY_STOPWORDS 落地，见 rag/search.py 头注释；reflector 相关规划随
20260903 裁决废除；失败用例回流/语料在位性检查 = eval/corpus_check.py + recall_eval
QUERIES 已常态化）。仍开放的行：

| 优先级 | 项 | 触发条件 / 现状 |
|---|---|---|
| P1 | 向量 + RRF 升级：双路召回 BM25 + 向量，RRF 融合 | **已落地（20261005，开关控制）**，见 §9。原触发条件（语料 >100 篇或 BEIR 基准对比显示词法掉点）至今未达到——20260831 POC 在 22 query/32 文档下三路几乎打平，唯一差异是 1 条困难用例（rag_eval_system 词法 rank4 → 向量/RRF rank2，未到 rank1，agentic RAG 下 rank2 完全可用）。所以这次落地的是**能力 + 开关**（`RAG_HYBRID_ENABLED`，出厂默认关），不是"换主力"：写多读少的个人博客本来就要等语料长大，届时拨一下开关即可，不必再改代码 |
| P2 | 超长文档按节读取：get_article_detail 扩展 section 参数，单篇超长按"文章X第Y节"精读，不全文注入 | **已落地（20260920）**，回归锁 `tests/test_sections.py`。触发条件早已满足：站内最长文章 25,445 字（帧 52,834 字）而单帧上限 20,000 ⇒ §7–§10 四个整节从未进过任何一轮上下文，模型连"有东西被截掉了、截掉的是哪几节"都无从知道。切分/节选/按节取回三处现在共用 `rag/sections.py` 的同一套边界 |

## 9. 向量 + RRF 混合检索（20261005）与检索评测数据集选型

### 9.1 落地形态

- **开关**：`RAG_HYBRID_ENABLED`（`config/settings.py`，出厂默认**关**）。关 = 逐字节同
  从前（BM25 原分、原相对断崖、原候选数）；开 = 词法 + 向量双路召回、RRF 融合重排。
  语料规模与 query 类型自己拨——本仓语料目前 12 篇，词法已经打满，开着也量不出收益。
- **向量模型独立配置**：`EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL`
  （+ `EMBEDDING_DIM` / `EMBEDDING_BATCH_SIZE` / `RRF_K`）。供应商不进代码——任何
  OpenAI 兼容的 embeddings 端点都行；用聚合平台就把 `QWEN_BASE_URL`/`QWEN_API_KEY`
  的值复制一份过去（代码不做隐式绑定，那正是"独立配置"的代价）。
  **20261007 起这一组不再只归检索**：首页「文章向量空间图谱」的建图
  （`scripts/build_word_graph.py`）与图谱检索（`rag/wordgraph.py`）共用 `rag/embed_space.py`
  同一条解析规则——**配了 `EMBEDDING_*` 就用它，没配回落 `QWEN_*` + `text-embedding-v4`**
  （所以只配 `QWEN_*` 的老部署行为逐字不变）。图上记着自己那片空间（模型 + 端点），
  查的东西对不上就 `space_mismatch` 明着降级（零 embedding 调用），处置是重建一次图谱
  ——**换 `EMBEDDING_*` 的模型或端点之后必须重建**。
- **索引与增量**（`rag/vector_index.py`）：自研 f32 文件 + manifest（零新增依赖，
  生产 venv 仍是那 11 个钉死的包）。内容寻址（键 = 模型+端点+请求维度+文本的摘要）
  ⇒ **只有真正变了的 chunk 会重新调 API**：改一节只嵌那一节，删文章 0 次调用，
  换模型/换端点/换维度整库失效重嵌。只有 embedding 调用是增量的；拉语料、BM25 重建、
  每 worker 读盘建视图仍是全量（几十篇语料，为 200KB 做行级 patch 是拿正确性换不存在的性能）。
- **三条硬规则**（`rag/search.py` 的 `search()`，每条对应一种静默的错法）：
  ① 只有两路都在场才融合——单路 RRF 与 BM25 不是同一套分数语义，混着回下游，
  `decisions.py` 的「只允许越读越高分」闸会跨轮比两套量级；② 不在 RRF 分数空间里
  再造断崖（0.25 照搬到 ≈0.016 的融合分上等于清空候选）；③ 向量侧绝不进 `build()`
  （那是懒刷新热路径，"快且不联网"是评测依赖的性质），也不拿对不上语料的向量融合。
- **可观测**：`/health` 的 dials 有 `rag_hybrid_enabled` / `rag_hybrid_active` /
  `rag_state` / `vector_missing`（**开关开着 ≠ 真的在融合**——后者还要凭据齐、盘上有索引、
  索引与语料对齐）。`rag_state` 是把前两格合成一句话的那一格：线上首建那 6 秒的真实组合
  `enabled=true / active=false / missing=0` 自己读不出病因（当时第一反应是"全就绪却不
  生效"，实为 `warming`），所以病因必须由机器点名——取值 = `off` / `active` /
  `missing_credentials` / `warming` / `vector_missing` / 装载或写入错误码；
  `rag_search` 工具把本轮实际路线（hybrid/lexical/degraded + 原因码）放进
  `ToolResult.meta`，出口文本逐字不变。降级一律有名有姓（missing_credentials /
  warming / no_vectors / stale_view / query_embed_failed / vector_missing），不静默。

### 9.2 首次上线实测（20261005，12 篇语料 / 22 query）

两臂同日交替跑（`eval/recall_eval.py`，评测即线上实现，报告在 `eval/report/runs/`）：

| 指标 | 词法 | hybrid | 读法 |
|---|---|---|---|
| recall@1 / @3 / @5 | 0.92 / 1.00 / 1.00 | 0.92 / 1.00 / 1.00 | **打平**（22 条里没有一条因融合改变名次） |
| MRR | 0.96 | 0.96 | 打平 |
| noise_hit_rate | 0.89 | **1.00** | 变差——见下 |
| mean_candidates | 3.36（正例 3.08） | **5.00（正例 5.00）** | 供给端变宽 |

结论按"**没变差**"读（22 条太少，单条涨跌是噪声），真正要看的是两个结构性代价：

1. **候选供给变宽**：词法路的相对断崖把候选压到平均 3.36 条，融合后 = 两路并集截 `top_k`，
   实测稳定 8 条（`rag_search` 默认 top_k=8），后五条是 `test3` 这类无关行——20260920 批 d
   的"省吃俭用"被抵掉一半。读侧的「只允许越读越高分」闸能吸收一部分，但**供给已经发出去了**。
2. **`kind=empty` 这条结构性诚实没了**：语料外的问题（如问一个站内根本不存在的主题）在词法下
   `hits=[]` ⇒ `ToolResult.kind=empty` ⇒ 计划里的「检索无结果」分支；融合后向量路总能凑够
   top_k 条低分候选 ⇒ **不再有"什么都没找到"这个形状**，agent 少了一条如实拒答的结构性支点。
   `noise_hit_rate` 0.89 → 1.00 量到的就是这件事。

所以：**能力已就位、开关出厂关**。语料长到词法开始漏召回（§8 的触发条件：>100 篇或
BEIR 对比显示掉点）再拨开——届时这两个代价要一并处理，最可能的做法是在融合路径上按
**排名**再截一道（RRF 分数量级 ≈0.016，照搬 0.25 断崖等于清空，见 §9.1 硬规则②）。

增量行为同日对真 provider 实测（隔离目录）：语料不变 → `reused 265 / api_calls 0`；
改一节 → `embedded 1 / api_calls 1`；整篇删除（43 chunk）→ `embedded 0 / api_calls 0`，
count 322→279；两次代数之后恢复该篇 → `embedded 37`（超出 `PRUNE_KEEP_GENS=2` 窗口的行
已 prune，属已知代价）。

### 9.3 想拿公开数据集量"我的检索好不好"？先说清楚它量不了

公开检索集的语料动辄十万到百万级、query 数千到数万条、指标是 nDCG@10——它们量的是
**检索器/嵌入模型的通用能力**，不是"你这个十来篇语料的站内搜索"。在这点语料上唯一有
意义的基线是 `eval/recall_eval.py` 那 22 条（评测即线上实现）。公开集的正确用法有三个：
（a）横向量嵌入模型（同一个数据集上换模型，比 nDCG）；（b）标定 RRF 参数（k、各路剪枝深度）；
（c）验证融合实现本身有没有写对。**别拿它的绝对值当自己站点的分数。**

**C-MTEB 的中文检索子集**（BEIR 格式：`corpus` / `queries` / `qrels` 三件，评测取 nDCG@10）：

| 数据集 | 测试 query 数 | 类型 |
|---|---|---|
| T2Retrieval | 24,832 | 通用网页段落（最大、最像"技术博客"） |
| DuRetrieval | 4,000 | 通用（百度问答式） |
| EcomRetrieval | 1,000 | 电商 |
| MedicalRetrieval | 1,000 | 医学 |
| CovidRetrieval / CmedqaRetrieval / MMarcoRetrieval / VideoRetrieval | 各 1k–10k | 领域检索 |

下载（ModelScope，parquet）：

```bash
uv run --no-project --with modelscope modelscope download \
    --dataset C-MTEB/T2Retrieval --local_dir ./data
uv run --no-project --with modelscope modelscope download \
    --dataset C-MTEB/T2Retrieval-qrels --local_dir ./qrels
```

现成工具（同样走 `uv run --no-project --with ...`，**绝不进产线 venv**）：

- `ranx`：RRF 及 20 余种融合算法、nDCG/MRR/Recall 多档、`Qrels`/`Run`/`evaluate`/`compare`，
  还有 `optimize_fusion`（直接搜融合权重/参数）；
- `pytrec_eval`：TREC 官方实现的指标；
- `ir_datasets`：一行加载 BEIR / MIRACL / mMARCO-zh 等；
- BEIR 官方脚本（仓库自带 `beir` 评测入口）。

**最贴合本仓语料的路线（下一步，尚未做）**：拿现有 22 条 query 当锚点，从自己的文章里
合成 100–200 条 query + qrels，算 nDCG——这是唯一能回答"换成向量之后**我的**检索变好没有"
的办法。合成 query 必须人过一眼，否则是自证（模型出的题它自己必然答得上）。

⚠️ 上面这些数据集与 numpy/ranx 一律在隔离环境里跑（同 `rag/graph_build.py` 的范式）：
本机 `.venv` **就是产线 venv**，多装一个包就等于给线上多一个依赖。
