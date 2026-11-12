# 固定语料检索评测

检索评测复用生产 RagIndex、BM25S 和 RRF，使用 BEIR 数据格式、TREC run 和
ir-measures 指标。它检查候选与排序；最终回答、引用和权限由 Agent HTTP 评测及语义复核检查。
三路对照取工具结果裁剪前的检索器候选，不应用剩余上下文预算、实时公开性验证或正文读取；
这些成绩不等于模型最终可见的证据覆盖率，也不等于端到端任务成功率。
已完成的固定数据三路对照、失败归因和复现命令见[中文检索实验报告](chinese-retrieval-comparison.md)。

## 全库与成熟实现对照

已完成的 100,001 篇源文档、2,000 查询与 CMRC 已知来源诊断见
[全库实验报告](full-corpus-retrieval-comparison.md)。报告保留全库与实际可索引数差异、
各阶段失败、候选池归因和成熟实现控制；博客不参与本轮调参。

`prepare_duretrieval --full-corpus --queries 2000` 保留固定源的全部 100,001 篇文档与
2,000 个原始 dev 查询，不添加按 BM25 选取的干扰项，不筛短文。它仍不是独立业务盲测。
全库入口显式提高加载上限，不放宽普通评测默认的 10,000 篇保护。

以下命令用于独立环境。先按下文准备固定原始 parquet，将本地 embedding 配置放入环境变量
`EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`EMBEDDING_PROFILE`、`EMBEDDING_API_KEY`，并设置
独立可丢弃库的 `SAUDADE_RAG_EVAL_DSN`。模型服务的 tokenizer 参数必须与 profile 一致。

```sh
python -m eval.prepare_duretrieval --source-dir /path/to/pinned-source \
  --output /tmp/du-full --full-corpus --queries 2000
SAUDADE_IGNORE_ENV_FILE=1 python -m eval.full_corpus_cache /tmp/du-full \
  --output /tmp/du-artifacts --collection eval-du-full
SAUDADE_IGNORE_ENV_FILE=1 python -m eval.full_corpus_lexical /tmp/du-full \
  --mode chunk --output /tmp/du-artifacts/lexical-chunk.jsonl
SAUDADE_IGNORE_ENV_FILE=1 python -m eval.full_corpus_lexical /tmp/du-full \
  --mode document --output /tmp/du-artifacts/lexical-document.jsonl
SAUDADE_IGNORE_ENV_FILE=1 python -m eval.full_corpus_lexical /tmp/du-full \
  --mode document --tokenizer jieba --output /tmp/du-artifacts/lexical-jieba.jsonl
uv run --frozen --with scikit-learn==1.7.2 --with joblib==1.5.3 --with threadpoolctl==3.6.0 \
  python -m eval.full_corpus_dense /tmp/du-full \
  --artifacts /tmp/du-artifacts --collection eval-du-full
python -m eval.full_corpus_analysis /tmp/du-full \
  --artifacts /tmp/du-artifacts --output /tmp/du-full-report.json
```

数据库先由迁移账号执行 `rag.manage setup`。这些入口不连接默认部署库，缓存与查询只接受显式
评测 DSN 和 `eval-` collection。cache 入口只填充离线评测缓存，分批提交已完成输入；全部齐备后
才使用生产 repository 发布完整成员快照，不改变生产 VectorStore 的事务语义。

BM25 对照复用同一词法特征、参数和 BM25S，使用其公开 Tokenized 接口控制大语料内存。
原生评分与应用评分/文档聚合逐题核对，额外记录默认领域别名扩展的影响。整篇索引与切片索引
的差异同时包含词频统计、切片和聚合，不能归因成单个因素。此入口不测试完整 RagIndex
刷新或保留字符串 token 的构造器内存。
另以 Jieba 原生搜索分词（过滤纯空白、保留其余 token）建立整篇 BM25S 基线，帮助识别项目
自定义特征的影响；不带项目的归一化、词形还原、bigram 回退或别名扩展，不宣称官方基准协议。

向量对照用同一份持久化输入向量，在 scikit-learn 精确余弦与生产 PostgreSQL 查询间比较；
记录数值误差和近似并列次序差异，不把 Q8 与原模型的差异混为数据库误差。另比较先截取
100 个切片再去重与先按文章聚合再取 100 篇，区分切片预算和文章预算。

分析阶段冻结 24/50/100 三种每路候选窗口，并核对生产 RRF 与直接调用 LangChain RRF 的一致性。
源码级 qrels 只标注文章，不能用这些分数验收章节证据。博客夹具不作为本轮调参或业务发布门槛，
回答生成及 LLM Judge 均不在这组实验范围内。

DuRetrieval 不含答案位置标签。另可用固定 CMRC2018 dev 的原始答案跨度诊断已知段落内的定位：

```sh
curl --fail --location \
  https://raw.githubusercontent.com/ymcui/cmrc2018/c0eb1b6ba219847457e6af3180da722bbeb656af/squad-style-data/cmrc2018_dev.json \
  --output /tmp/cmrc-dev.json
python -m eval.prepare_cmrc_spans /tmp/cmrc-dev.json --output /tmp/cmrc-spans
# 对 /tmp/cmrc-spans 运行下文已有 model_retrieval_run 三路入口。
```

准备器验证文件 SHA，保留全部 848 个源段落和 3,219 个问题，使用原始目标段落作为 scope。
三个原始答案标注中，逐条校验坐标；原坐标错误时只恢复精确且唯一的原文位置。缺失/歧义标注
记入 manifest，仍保留问题，不计成正确定位。原始答案的替代标注共用一个 evidence group。
这是小规模已知来源定位诊断，既不是全库检索成绩，也不能证明答案跨度之外的上下文充分、
整章证据完整、正文已读或回答正确。语料许可和论文引用随 manifest 固定，不再分发正文。

对照或位置消融可给 `model_retrieval_run` 增加 `--query-cache /tmp/query-vectors`。
该离线模式一次批量编码去重后的查询并保存浮点向量，绑定数据/模型空间和文件 SHA；复跑时复用
精确相同的查询向量，只替换查询编码步骤，检索、聚合及位置返回保持原路径。报告独立记录初次
编码耗时、缓存读取耗时及 `frozen_query_replay`，检索计时排除查询编码，不能当作在线 API 性能。

已有位置报告可再做一次无模型、无数据库的整章范围反事实：

```sh
python -m eval.section_range_ablation /tmp/cmrc-spans /tmp/cmrc-report.json \
  --output /tmp/cmrc-section-ablation.json
```

它不更换命中文章、章节或排名，只把返回窗口扩成同一章节的完整源范围，并同时报告取文字符量。
这用于辨认窗口截断与章节选择错误；没有模拟工具裁剪、正文读取或最终回答，不能当成等预算收益，
也不能把窗口跨度未覆盖直接算为 Agent 执行失败。

## 无密钥离线回归

安装锁定 dev 依赖后运行；不读取部署 .env，不访问博客或 embedding 服务：

```sh
python -m eval.offline_rag_eval eval/fixtures/blog_retrieval --split dev --output /tmp/rag-dev.json
python -m eval.offline_rag_eval eval/fixtures/blog_retrieval --split test --output /tmp/rag-test.json
```

随仓语料是 MIT 许可的项目自建合成中文技术语料与固定参考标签：28 篇文档，开发集14题、公开评估集21题。
它覆盖长文末尾、重复标题、多文证据、口语改述、相近术语、难负例及无答案。
开发集包含已经用于别名扩展和契约开发的用例；公开 test 在本批实现评估前固定，
但作者可见、规模小，不能称作独立盲测、生产成功率或公开榜单成绩。
语料内容不依赖站点编号，贡献者可复现；新增问题必须有来源和相关性标注。

生产验收另外固定真实博客内容版本、问题及证据范围，脱敏后审查最终回答与实际费用。
生产需求决定验收分布，不代表普通 PR 必须访问维护者的服务器。
独立验收集应与调参材料隔离；用来调优后轮换，不能只换成 holdout 标签。

## 中英文词法分析

文档与查询共用 jieba 搜索分词、Snowball 英文词干和 Unicode 归一化。
技术标识保留有意义的标点，区分 C++、C# 和 C；中文单字可参与检索。
索引保留原词；查询优先内容特征，纯停用词查询保留完整特征以支持真实标题检索。
中文使用有界二字回退保留跨词边界的召回，同一原文位置的相同特征去重，不生成全部三字组合。
证据摘录使用分词时保留的原文坐标，大小写、重音、兼容字符及词干变化不改变源偏移。
分词策略与依赖版本记录于 lexical_backend；词法索引随进程重建，向量空间及已有向量不变。
中英文同语种检索与共享技术标识由词法覆盖，跨语言语义检索仍依赖向量能力。

```sh
python -m eval.offline_rag_eval eval/fixtures/bilingual_retrieval --output /tmp/rag-bilingual.json
```

这组材料是公开的中英文开发回归集，包含技术名词、英文词形、Unicode、中文单字和无答案，
不作为独立验收集或跨语言语义检索成绩。评估时同时保留现有中文技术集的逐题退化检查。

## 数据和指标

目录包含 corpus.jsonl、queries.jsonl、qrels/<split>.tsv 和 manifest.json。
文档/查询使用稳定字符串 _id，qrels 标明相关等级；manifest 记录来源、版本、许可和范围。
默认拒绝超过一万文档，需明确准备子集或提高 --max-docs，不能静默截前 N 篇。
无答案题必须显式 answerable=false 并有零分标注；没有标注不等于无答案。
多文问题可用 evidence_groups 表示必须覆盖的证据组，组内文档可以互相替代。

- Success@k：任意相关文档命中。
- R@k：命中的相关文档占所有已标注相关文档的比例。
- RR@k、nDCG@k：前排位置与分级相关性，交给 ir-measures 计算。
- EvidenceCoverage@k：必须证据组的覆盖比例，仅适用于显式标注题。
- CandidateReturnedOnUnanswerable：只记录候选供给，不是模型误答率。

默认 k=1/3/8，8与生产工具共用 rag.DEFAULT_TOP_K。无答案题不进入排名指标分母。
按 category 分桶、保留逐题结果和每项样本数，不把文档命中当成正文已读或答案完整。
未标注文档不能据此断言不相关。公开小集不提供生产性能或置信区间保证。

## 真实博客章节证据

[`blog_evidence`](fixtures/blog_evidence/README.md) 冻结全部公开文章和源范围标注。
除文章级 qrels，还保存 `evidence_spans` 的必要组/可替代精确原文范围；加载时核对
内容 SHA、解析版本、章节和字符坐标。`scope_document_id` 用于已知文章内检索。
三路运行器保留真实返回位置，额外报告 `PassageEvidenceCoverage@k` 与
`AllPassageEvidence@k`。只有 TREC 文章排名而无位置时，这两项保持未评估。

标注由项目 Agent 编写，源坐标校验不代替独立人工语义复核。这个数据集用于定位文章
命中后的证据缺口；公开、体量小且问题相关，不能据此宣称通用 RAG 或生产成功率。

## 实际 embedding 与混合检索

先通过 `rag.manage setup` 初始化评测库，并将其 DSN 放入 `SAUDADE_RAG_EVAL_DSN`；
不要使用生产 DSN/collection。这个显式入口会调用指定服务并写入显式可丢弃 PostgreSQL 的独立 collection，普通 pytest/PR CI 不调用它：

```sh
python -m eval.model_retrieval_run eval/fixtures/blog_retrieval --split test \
  --base-url "$EMBEDDING_BASE_URL" --model "$EMBEDDING_MODEL" \
  --profile /absolute/path/to/profile.json --expected-dim "$EMBEDDING_DIM" \
  --collection eval-blog-fixed --output /tmp/rag-model.json
```

默认沿用生产字符切片、profile 的 token 预算细分、embedding批次配置及8候选窗口。
查询指令、池化、归一化、模型版本和 tokenizer 必须与目标服务一致，不能只匹配维度。
向量服务失败、索引不全或空间错误时停止，不把退化为词法的结果冒充混合检索成绩。
输出同时保留 BM25、vector、hybrid 三路的原始 run 和相同 cutoffs 下的指标。

--top-k 控制交付结果数；--candidates 控制每路候选池，默认与生产共用
`min(100, max(20, 3 * top_k))`；默认 top_k=8 时每路取24个候选。
--k 只控制评分截点，须不超过 top-k。扩大池或改变切片是显式研究配置：

```sh
# 追加到上面的同一条命令；两臂使用相同冻结数据和目标模型配置。
--chunk-chars 384 --candidates 20 --top-k 10 --k 1 3 10
```

报告区分 production_retrieval_defaults 与 research_override，记录完整配置与指纹、
数据/代码哈希、实际词法和向量片段数、增量索引复用、查询缓存初始状态及分阶段耗时。
query_seconds 包含词法、向量与融合；单顺序客户端不代表并发容量，热缓存和冷缓存分开解释。

其他检索器可以导出标准六列 TREC run，通过同一入口评分：

```sh
python -m eval.offline_rag_eval /path/to/data --run /path/to/model.run \
  --run-info /path/to/model-info.json --output /tmp/model-report.json
```

run-info 需要 name、revision、非空 config；配置记录模型和输入处理，不写入凭据。
未知文档/查询、重复结果、非有限分数均失败；缺失查询按空结果计入，不能从分母消失。

[BEIR SciFact 验证记录](scifact-local-baseline.md)使用英文科学语料，可补充通用检索控制，
不代表中文博客效果。公开中文语料可选择许可明确的固定 MIRACL 子集，保留背景与难负例；
无需将测试材料发布到博客，下载和真实模型执行均显式选择。

## 中文公开基准对照与归因

`eval.prepare_duretrieval` 复用已有固定抽样与 BEIR 读取器，从固定 revision 的
[MTEB DuRetrieval](https://huggingface.co/datasets/mteb/DuRetrieval) 准备中文对照。
仅处理本地已下载 parquet，逐文件 SHA-256 不符时拒绝准备；下载来源、路径与校验和
见该模块常量。原始语料正文不提交仓库，MTEB 卡片未声明语料许可的事实保留在 manifest。

```sh
python -m eval.prepare_duretrieval --source-dir /path/to/pinned-du-parquets \
  --output eval/data/du-fixed --queries 200 --background 8192 --pool 10000
```

按固定 seed 选问题，保留这些问题的全部原始相关性等级，再加入固定背景文档及词法干扰项。
不按长度筛题、不截断正文；未标注候选不能当成负例。默认准备的是约九千文档的源 dev
子集，不能报告为官方十万文档完整基准、未见测试集或生产任务成功率。

对它和博客证据集分别使用上面的 `eval.model_retrieval_run`，保持模型、profile、切片、
候选池、top-k 和 k 相同，使用独立评测数据库/collection，不访问生产向量集合。
每个进程顺序测词法、向量、融合；向量阶段包含查询 embedding 和数据库访问，
不把这段时间误算成纯数据库检索，也不把整个三路共测时间算给某一单路。

```sh
python -m eval.retrieval_comparison eval/data/du-fixed /tmp/du-report.json \
  --split dev --cutoff 8 --output /tmp/du-attribution.json
python -m eval.retrieval_comparison eval/fixtures/blog_evidence /tmp/blog-report.json \
  --split test --cutoff 8 --output /tmp/blog-attribution.json
```

归因器校验报告与数据哈希，保留每题三路的结果、配对提升/退化数与查询级描述性 bootstrap。
失败分为 top-k 未命中相关文章、多文证据不全、命中文章但指定证据位置未覆盖；它们是
可观测阶段。当前不保存每路完整候选池，不能仅据此区分初召回不足和输出截点排序失误，
也不能推断 embedding 模型根因。相关问题家族的统计不代表独立生产采样。
无答案候选只记观察，回答事实性与弃答正确性保持未评估，不调用 LLM Judge。

当前没有独立 LLM 查询重写器或 cross-encoder reranker。先用逐题证据判断错误发生在
查询、召回、排序还是正文读取，再比较改写/重排带来的质量、时延与费用，不默认增加模型调用。

参考：[BEIR](https://github.com/beir-cellar/beir)、
[ir-measures](https://ir-measur.es/en/latest/getting-started.html)、
[MIRACL](https://github.com/project-miracl/miracl)。
