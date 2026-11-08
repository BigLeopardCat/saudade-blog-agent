# 固定语料检索评测

检索评测复用生产 RagIndex、BM25S 和 RRF，使用 BEIR 数据格式、TREC run 和
ir-measures 指标。它检查候选与排序；最终回答、引用和权限由 Agent HTTP 评测及语义复核检查。

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

--top-k 控制交付结果数；--candidates 控制每路候选池，默认等于 top-k。
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

当前没有独立 LLM 查询重写器或 cross-encoder reranker。先用逐题证据判断错误发生在
查询、召回、排序还是正文读取，再比较改写/重排带来的质量、时延与费用，不默认增加模型调用。

参考：[BEIR](https://github.com/beir-cellar/beir)、
[ir-measures](https://ir-measur.es/en/latest/getting-started.html)、
[MIRACL](https://github.com/project-miracl/miracl)。
