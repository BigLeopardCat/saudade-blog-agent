# RAG 检索增强问答：架构设计与实现总结

> 博客看板娘 agent 的 RAG 能力（2026-08-30 落地）：访客问博客内容（"Git 和 SVN 有什么区别"
> "ESP32 的 OTA 怎么配置""留言板里有人聊过 RAG 吗"），agent 从线上可见语料检索定位、
> 精读全文后作答。
> 本文是面试展示材料：架构 → 选型 → 实现 → 工作流 → 问题与解决 → 评测 → 自评。

> **⚠️ 定位变更（20260901/20260902，正文保留为历史设计记录）**：本文正文描述的是
> 20260830 的 **rag_query 技能两段式**设计——20260901 定位重构后已废除（见
> [问题记录 1.32](../agent-architecture.md) 前置部分与 `agent-architecture.md` §6.5）：
> 1. **20260901 定位重构（RAG 定位错了）**：把说说/留言拉进检索语料是污染（碎碎念无参考
>    价值），RAG 正确用法是**文章检索的前置任务**而非意图类型（"除了 chat 就是 rag_search"
>    是错误路由）。rag_query 技能废除，检索池只收文章；content_query 扩容承接全部内容
>    查询：知识型 → 执行层自由 ReAct 自选 rag_search/search_notes 定位 + get_article_detail
>    精读全文（"检索管发现，工具管精读"不变，正文 §2 的架构原则仍成立）；数据/列表型 →
>    list_guestbook/list_talks 等数据工具直查，不走检索。
> 2. **20260902 planner 显式点名工具**：查"留言板/说说里有没有人聊过/写过 X"由 planner
>    PARAMS.tools 显式点名（白名单 _EXPLICIT_TOOLS，双源必须成对），经 TOOLS 行强制 +
>    reflector 逐工具核验——根治"planner 对、model 零工具编造"（233815）。
> 3. **检索评测口径更新**：recall_eval 现 21 条 queries = 12 条 recall 正例 + 9 条噪声
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

不采用主流"管道式 RAG 直答"（检索 top-k chunk 直接拼 prompt——快但 chunk 断裂、无法精读全文），
而是**检索只负责定位，解读永远走工具精读全文**：

```
用户问题
  → [检索] rag_search(query)        只返回候选：type/id/标题/命中小节/分数，不含全文
  → [解读] get_article_detail(id, doc_type)   精读候选全文（note/talk/board/announcement）
  → [回答] 基于全文作答，无候选/无关时诚实拒答
```

- **索引粒度 ≠ 读取粒度**：索引按 markdown 小段切 chunk（召回精）；读取用全文（解读准，不被切片污染）。
- **引用可追溯**：回答可关联具体文章/说说 ID（trace 记录工具轨迹，四端可对账）。
- 两条硬纪律由系统强制，不靠模型自觉：
  - **两段式 TOOLS 行**（技能模板）：TOOLS 行固定 `rag_search` + `get_article_detail`，
    reflector 检查点强制两段都出现在工具轨迹中，缺失即 REVISE——堵"只检索不读全文"。
  - **诚实拒答**：检索无结果、或候选与问题无关时如实告知，回复契约明确"不得编造"。

## 3. 技术选型（每个选择都有数据/约束支撑）

| 决策 | 选择 | 理由与备选 |
|---|---|---|
| 检索算法 | **词法 2/3-gram 子串匹配 + BM25** | 检索 eval 实证 recall@1=1.00 **打满当前语料**（34 文档）。纯 2-gram BM25 只有 0.43（gram 太碎、idf 失效）；词法基线先上（红牌清单"先 top-k 基线"），**向量留作 L1 BEIR 基准的对比项** |
| 中文分词 | **CJK 连续段拆 2/3-gram**（子串匹配近似） | 不引 jieba/词向量——3.7GB 机器不跑本地 embedding；2/3-gram 覆盖 2 字词与 3 字词的共现，混合语料（中文博客+英文技术词）按 `[一-鿿]+|[a-zA-Z0-9_\.]+` 分型 |
| 存储 | **内存倒排索引**（纯 Python dict） | 语料 34 文档，全量重建 <100ms，不做增量；线程安全（锁 + 原子替换），重建失败沿用旧索引。不引 sqlite-vec/向量服务（万级语料才需要考虑） |
| 语料源 | **Rust 公开 API**（/api/public/*） | agent 保持无 DB 依赖架构；可见性过滤由 Rust 层保证（is_public=1 AND status!='draft'，talk/board approved=1）——与前台严格一致，**draft/private 不进语料** |
| 刷新 | **10 分钟懒刷新**（TTL 600s） | 全量重建幂等且便宜，不做增量/CDC；博客写少读多，10 分钟延迟可接受 |
| 工具化 | **技能注册表两段式**（rag_query 技能） | 复用既有"技能模板 + reflector 质检"体系（与导航/特效同构），TOOLS 行强制序列依赖 |

## 4. 实现（rag/search.py，~180 行）

```
RagIndex
├── build()      _fetch_corpus()（走 Rust API，note 逐篇拉全文）→ chunk_note()（markdown 标题切分，
│                >2000 字才切）→ tokenize() → 倒排 postings + 每 chunk TF + idf + avgdl → 锁内原子替换
└── search(q)    TTL 懒刷新 → tokenize → 命中的 gram 逐 chunk BM25 打分
                 → 文档级聚合（by_doc 取最高分 chunk + sections 汇总）→ 排序截 top_k
```

- **BM25**：k1=1.2, b=0.75，idf 用 `log(1 + (N - df + 0.5) / (df + 0.5))`（平滑零概率）。
- **文档级聚合是核心细节**：索引粒度是 chunk，候选粒度是文档（解读走全文）——
  chunk 分数直接排序会让长文刷屏 top-k，聚合后每文档一个候选、附带命中小节定位。
- **工具层**（tools/base.py）：`rag_search`（调 search()，返回候选 JSON）；
  `get_article_detail` 泛化 `doc_type`（note/talk/board/announcement），
  talk/board/announcement 无单条端点，从列表接口按 key 过滤（列表已带全文）。
- **技能层**（agent/skills.py）：rag_query 技能定义两段式 plan；实例化时第二段参数填
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

- **L1 检索 eval**（eval/recall_eval.py）：**直接 import 线上 rag/search.py 的 search()**
  ——评测即线上实现，不另写模拟实现（防"评测绿、线上烂"）。21 条 queries 与 golden
  rag_* 用例同源出题（12 条 recall 正例 + 9 条噪声，20260901 语料净化后调整口径），
  报告 recall@1/@3/@5 + MRR + noise_hit_rate → eval/report/runs/。
  实证：词法基线 recall@1=1.00、MRR=1.00、12 条 recall 正例全部 rank=1。
- **L2 端到端 golden**（现 22 条 rag_* 用例）：recall 正例（从文章出题，断言知识词命中）+
  noise 组（语料外问题，断言诚实拒答），与导航/特效等用例同池，全量 55 条回归
  （20260902 最新 51/53 批次 PASS，两 FAIL 均为预存 flake/断言分歧，非改动引入）。
- **trace 可观测**：每轮对话落 JSON trace（工具序列 + 分段耗时），RAG 失败可直接
  读 trace 归因（模型没调工具？检索没命中？读错候选？）。

## 7. 遇到的问题与解决（浓缩版，详见 docs/问题记录.md 1.19-1.25）

| 问题 | 根因 | 解决 |
|---|---|---|
| 中文检索零命中 | CJK unigram 被 `len>=2` 过滤 | 拆 2/3-gram，recall 0.79→1.00 |
| 纯 2-gram BM25 只有 0.43 | gram 太碎、idf 失效 | 2/3-gram 混合 + 词法基线定论；向量留 BEIR 对比 |
| 语料构建三坑 | status 实际是 'public'；talkKey serde 改名；列表接口空正文 | 排除 'draft'；talkKey 字段；note 逐篇拉详情 |
| 长文刷屏 top-k | chunk 级评分违背文档级候选语义 | 文档级聚合（最高分 chunk + sections） |
| 模型只检索不读全文 | 两段序列依赖无强制 | TOOLS 行两段式 + reflector 检查点强制 |
| talk 候选无法读全文 | get_article_detail 只支持 note | 泛化 doc_type，从列表接口按 key 过滤 |
| 单条概率波动 | LLM 随机性跳过工具 | 归因为波动（重跑 PASS），接受，reflector 概率性兜底 |

## 8. 面试项目自评

### 亮点（可深挖的叙事）

1. **评测驱动、评测即线上实现**：检索 eval 直接测 `rag/search.py` 的 `search()`——
   不写模拟实现、不测近似代码，指标就是线上行为；21 条 queries（12 正例 + 9 噪声）
   与 golden rag_* 用例同源出题，recall@1=1.00 是词法基线的实证结论（不是拍脑袋）。
2. **决策有数据**：不上向量是 recall_eval 打满后的工程决策，不是能力缺失——
   词法已满足当前语料，向量升级有明确的触发条件（BEIR 基准对比）。
3. **agentic RAG 两段式**：检索只定位、解读走工具全文——索引粒度与读取粒度分离，
   引用可追溯；两段式由技能模板 + reflector 质检**系统强制**，堵"只检索不读全文"
   是 golden 真实失败驱动出来的（44/48 → 47/48 的修复链完整记录在问题记录）。
4. **边界与安全**：语料=线上可见内容（与前台严格一致，draft/private 不进语料）；
   noise 用例强制诚实拒答（"博客里没有 Docker 部署教程"是合格的 RAG 行为）。
5. **踩坑史完整**：tokenize/BM25/接口字段/可见性/工具覆盖——每个问题都有根因+修复+评测验证，
   是"做过工程"而非"跑过 demo"的证据。

### 薄弱点（面试官会问，准备答案）

| 问点 | 诚实回答 |
|---|---|
| 为什么不用向量？ | 词法在当前语料 recall@1=1.00 已打满（数据实证）；向量在 34 文档语料上收益为零却引入模型部署/量化成本（3.7GB 机器约束）。升级触发条件明确：语料规模增长或 BEIR 基准显示词法掉点 |
| 语料才 34 篇？ | 是。个人博客语料天然小；架构按"几百篇仍用内存倒排（全量重建 <100ms），万级再考虑向量服务"设计；评测方法（recall@k/MRR）与工具契约不随语料规模变化 |
| 检索质量如何证明？ | 检索 eval（recall@1=1.00/MRR=1.00/噪声命中率）+ golden 端到端 rag_* 22 条（全量 55 条回归把关）；指标进 eval/report/runs/ 留档 |
| 和 LangChain 的 RAG 有什么区别？ | 没有现成检索组件：手写倒排+BM25（~180 行，评测即线上）；两段式是技能注册表模板的实例，与防幻觉体系（reflector 质检）共用一套机制 |

### 一句话叙事

> 在 3.7GB 单机上给博客看板娘做了检索增强问答：评测先行（检索 eval 直接测线上实现、
> recall@1=1.00 实证词法打满）→ agentic 两段式（检索只定位、工具精读全文，模板+质检
> 系统强制不跳步）→ 语料可见性与前台严格一致、噪声样本强制诚实拒答——每个失败
> （中文分词、BM25 碎 gram、接口字段、工具覆盖缺口）都有根因和修复链，完整记录在案。

## 9. 剩余规划（触发条件驱动；原 P0 三行已落地，见 ✅ 标注）

按"有明确触发条件才做"原则排级，全部已记入问题记录/评估：

| 优先级 | 项 | 触发条件 / 动机 |
|---|---|---|
| P0 ✅（20260901 回流跑通） | **失败用例回流评测集**（20260831 事故教训）："RAG测评体系"真实用户 query 进 recall_eval（期望命中 note:19）+ golden 端到端；每次线上失败/误答补进评测集——**rag_eval_system 已回流 recall_eval QUERIES**（recall_eval.py 注释自标"P0 失败用例回流第一单"）、golden 端到端同步覆盖，回流机制跑通；常态化"失败即补集"流程待做 | 事故复盘发现：真实 query"RAG测评体系建立 运行 使用"下架构文档只排 rank 5（通用词稀释），但该 query 不在任何评测集里——**检索稀释无人值守**（问题记录 1.26） |
| P0 ✅（20260831 视野对齐已落地） | **反射器视野快照 + REVISE 准确率统计**：trace 落盘反射器实际看到的轨迹摘要（输入快照），REVISE 判定按检查点类型（工具缺失/内容无关/越权…）分类统计准确率——**反射器视野对齐已落地**（收到结构化轨迹快照、1200 字符截断上限、rag_search 完整结果落盘 tools 节点 trace），REVISE 判定分类统计仍未做 | 1.26 事故：反射器因截断盲区误判正确行为为错误，REVISE 链把模型带偏——判错方向不可见，没有快照就无法区分"判错"与"截断误判" |
| P0 ✅（已落地） | **语料在位性检查**：golden/recall 期望命中文档（expected 的 note/talk id）在评测启动时校验是否仍在语料，不在则告警/标记漂移——**eval/corpus_check.py presence_check 已实现**，run_golden.py 与 recall_eval.py 启动即跑 | 1.27 事故：指纹文章改写为 OBC 文档后两条 golden FAIL + recall@1 1.00→0.86，表现像"模型退化"实为期望失配——语料是评测集的隐式依赖（语料走 live API 不在 git），改语料 = 改测试 |
| P1 | **BM25 轻量改进**：query 通用词降权（停用词表 / IDF 加权），缓解混合 query 下"建立/运行/使用"稀释（1.26 实证架构文档 rank 5 的根因） | recall_eval 出现 recall@3<1.00 或线上复现稀释案例；比上向量便宜一个量级 |
| P1 | **向量 + RRF 升级**：双路召回 BM25 + 向量，RRF 融合 | 语料 >100 篇或 BEIR 基准对比显示词法掉点。**RRF 是融合手段不是召回手段——无向量路时谈 RRF 无意义，升级必须两路同时落地**。**20260831 POC 已跑（eval/recall_vector_poc.py，结论：暂不切换）**：① 本地轻量向量模型（bge-small-zh ONNX int8 ~60-80MB 常驻）技术上可行，但机器内存亚健康（VSCode Server 占 ~1.8GB、swap 已用 1.5GB、available ~1.1GB），每 100MB 都要精打细算；② 优先方向阿里百炼向量 API（text-embedding-v4，1024 维）——key/base_url 复用 QWEN_API_KEY/QWEN_BASE_URL（同一百炼工作空间，零新增配置）、零本地内存；③ **POC 数据（22 条 query，32 文档/265 chunk）**：词法 recall@1/3=0.92/0.92、MRR=0.94 vs 向量与 RRF recall@1/3=0.92/1.00、MRR=0.96——**唯一差异=1.26 回流用例 rag_eval_system**（词法 rank4 → 向量/RRF rank2），向量把架构文档从"通用词稀释"中救回 top-3 但未到 rank1（talk:23"RAG 本质"留言语义相关排第一，属合理语义判断；agentic RAG 下 rank2 完全可用）；其余 21 条两路全打满无区分。**结论**：当前语料向量收益 = 1 条困难用例，按触发条件维持 BM25 主路，向量/RRF 作为升级预案；embedding 全量 265+21 条 ~11s/几分钱，落盘缓存 eval/cache/（gitignore），增量只需对文本变化 chunk 重嵌；查询侧纯 Python 余弦（32 文档全量点积微秒级，无需 numpy） |
| P2 | **超长文档按节读取**：get_article_detail 扩展 section 参数，单篇超长按"文章X第Y节"精读，不全文注入 | 单篇 >4k token（当前最长架构文档 2-3k 可控）；检索候选 sections 已带回命中节（1.26 改造后），落点现成 |
| P2 | 模型候选选择校验：reflector 检查点 2（候选选择合理性）——1.26 证明模型层选择（选 rank 5 不选 rank 1）靠语义判断，无法程序化强制 | 依赖 P0 的反射器视野改造先落地 |
