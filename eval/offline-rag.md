# 固定语料检索评测

本入口复用生产 RagIndex 的切片和 BM25，指标采用 ir-measures（底层成熟 IR 工具）。
不实现新的检索器、向量库或指标算法。它评测检索，不给最终回答的正确性打分。

## 本地运行

安装锁定的 dev 依赖后，在仓根运行：

```bash
python eval/offline_rag_eval.py eval/fixtures/offline_smoke --output /tmp/rag-smoke.json
```

随仓数据为人工编写的三个文档、三个查询，仅验证管道，不能作为模型性能成绩。
运行不下载数据、不读取部署 .env、不访问博客、不调用 embedding 或创建向量目录。
生产 RagIndex 的数据加载和向量预热增加可选注入点，原调用方保持既有行为。

## 数据契约

采用 BEIR 通用文件格式，直接支持按其格式准备的开源语料：

- corpus.jsonl：每行 `_id`（非空字符串）、`text`，可带 `title`。
- queries.jsonl：每行 `_id`、`text`，可加 `category` 分桶。
- qrels/test.tsv：表头 `query-id`、`corpus-id`、`score`，以 TAB 分隔；支持非负整数分级相关性。
- manifest.json：`schema_version:1`，以及非空 `name/source/revision/license/scope`。
  scope 明确原始全量、固定子集或人工夹具。许可在导入前自行核对，工具不授予再分发权。

`--split dev` 读取 qrels/dev.tsv。只评测该 qrels 中的查询；queries 可包含其他 split。
缺失文档、重复 ID/qrel、空输入、非法分数直接失败。默认最多一万文档；
超过上限拒绝运行，使用显式准备并留档的子集，或调整 --max-docs，不能静默取前 N 篇。

无正相关标注的查询必须显式设置 `answerable:false`，并至少有一条零分 qrel。
不能把“没有标注”误认为“已知无答案”。其结果只报告是否返回候选，不推断模型是否误答。

多篇问题可增加 `evidence_groups:[["A","A-alternative"],["B"]]`：
每组是可替代的正相关文档，不同组均需覆盖。成员必须已有正相关 qrel。
文档标注不自动升级为 chunk 或答案证据范围标注。

## API 与本地模型对照

其他检索器导出标准 TREC run（六列：qid、Q0、docid、rank、score、tag）：

```bash
python eval/offline_rag_eval.py /path/to/dataset \
  --run /path/to/model.run --run-info /path/to/model-info.json \
  --output /tmp/model-report.json
```

model-info.json 必须包含 name、revision、非空 config。config 应记录模型/权重、
量化、tokenizer、指令、池化、归一化、维度、切片与检索参数，禁止写入 API 凭据。
API 与本地模型走相同评分入口；没有 run 记录的查询按空结果处理，不能从分母消失。
未知查询/文档、重复结果、NaN/Infinity 失败。以 score 排序；同分按文档 ID 升序，
标准指标与证据覆盖使用同一确定顺序。原始 run 分数保存在报告内。

当前入口只内置生产 BM25。模型服务、API embedding、向量检索生成 run 是后续阶段，
不应把导入模型 run 的能力说成已部署模型。

## 指标和报告

- Success@k：任意正相关文档命中（也常称 Hit@k）。
- R@k：真正的正相关文档召回率，不是 Success。
- RR@k、nDCG@k：排名质量，均由 ir-measures 计算。
- EvidenceCoverage@k：命中所需证据组的比例，仅适用于显式标注的查询。
- CandidateReturnedOnUnanswerable：只描述候选供给，不能当作回答错误率。

默认 k=1/3/10。无答案项不进入排名指标分母；报告逐题结果和每个指标的真实样本数。
缺少 evidence_groups 的查询不进入证据覆盖分母。按 category 分桶，不把不同能力混成总分。
未标注项不当作“确定不相关”的事实；公开集标注不完整及模型预训练可能见过该集的局限，
必须在解读结果时说明。

报告包含语料/查询/qrels/manifest 摘要、检索代码摘要、切片版本、词法参数、
ir-measures 版本、原始 run、模型 run 来源信息、分桶和逐题结果。
内置 BM25 另记录构建与逐查询秒数；外部 run 不伪造本机推理耗时。
默认保留生产候选断崖，--no-cliff 可做独立对照。

下一阶段用 DuRetrieval、MIRACL 中文和 BEIR 小规模英文任务生成固定子集，
保留难负例与背景文档；子集成绩单独命名，不冒充官方全量榜单。
随后用有人工证据标注的技术文档集验证长文、多篇、无答案与最终引用。

参考：[BEIR](https://github.com/beir-cellar/beir)、
[ir-measures](https://ir-measur.es/en/latest/getting-started.html)。
