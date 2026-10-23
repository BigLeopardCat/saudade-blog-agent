# T2Retrieval 中文 embedding 开发对照

2026-10-23 的记录。本轮评测固定中文公开语料、真实模型和生产检索代码，不修改线上配置。
这是开发回归与部署可行性验证，目标 4 核 4GB 服务器尚未测试。

## 数据来源与抽样

来源 [MTEB T2Retrieval](https://huggingface.co/datasets/mteb/T2Retrieval)，固定 revision
`921dd3af6e78d1ae7ee0368aa8d7eaee02c8f08e`。源 dev 为 118,605 文档、22,812 查询、
118,932 条分级相关标注。dataset card 声明 Apache-2.0；这不是对原始网页文本权利的
独立确认。本仓只提交准备代码、来源和结果，不再分发语料正文；使用前按用途核查来源许可。

使用 SHA-256 排序、固定 seed 选择 24 个至少一个正相关标注的查询，保留这些查询的
**全部原始标注**（包括非正相关标注）及文档；另加 64 篇背景文档，并从 4,096 篇固定
候选池的 BM25 Top-50 中每查询选一个未标注候选（若存在）。未标注不等于负例；无候选
也不能删除查询。源 parquet 固定 SHA，流式读取，数据和模型下载均须显式授权参数。

| 桶 | 查询 | 文档 | 共同向量片段 | 输入范围 |
|---|---:|---:|---:|---|
| 短文 | 24 | 117 | 117 | 全部已标注文档与候选正文均 ≤128 字符，保留原文 |
| 不限长 | 24 | 227 | 776 | 不按正文长度过滤；最长 6,182 字符，保留原文 |

两个桶按同 seed **在不同的合格查询集合中抽样**，并非相同查询；禁止横向比较两桶分数。
短文资格要求该查询的每一篇已标注文档均满足长度限制，不能丢掉长正例再计算 Recall。
不限长桶正文中位数 606 字符。原文没有为速度做截断。

这是减少候选后的固定 dev 子集，**不是官方全语料基准**；候选远少于真实站点扩展后的库，
不限长桶三路 Top-1 都命中相关文档，说明它相当容易。高分不能用作生产准确率。
没有专门中英混合质量标签，也没有跨篇必需证据、无答案或生成事实性标签。
中文短文/完整文章检索已测；中英混合这里只验证 tokenizer 与服务功能，不声称质量已充分评估。

## 对照设置

同一 llama.cpp b11829、并发 1、2 线程、上下文 512；完整输入计入标题、查询指令和特殊 token。
BGE CLS + L2，Qwen last + L2；各自使用官方 tokenizer 和查询指令。
相同生产切片/聚合，共同窗口 384 字符；运行器会拒绝某模型仍需额外细分的样本，防止
切片不同导致模型对照失真。这个受控窗口不同于默认生产词法窗口，不能把收益全归模型。
候选深度 20、RRF k=60，BM25 保持生产默认断崖；使用 ir-measures，不另写指标公式。

BGE Q8/F16 来自 [CompendiumLabs 转换](https://huggingface.co/CompendiumLabs/bge-small-zh-v1.5-gguf)，
原模型 [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5)。
Qwen 使用 [官方 Q8 GGUF](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF)。
固定 revision、权重、runtime、tokenizer SHA 见 [资产清单](../deploy/embedding/artifacts.json)。
BGE GGUF 的 tokenizer 默认大小写/重音设置需按部署模板覆盖，修正后的 5 组 token ID
与官方 tokenizer 一致才进入最终对照；修正前的报告已被替换。Qwen profile 显式以标准 Unicode NFC 规范化请求，补齐其官方 tokenizer 与当前 GGUF
运行时在分解重音字符上的差异。成绩仅属于这组转换/runtime。

## 检索结果

短文桶（三个模型相同输入与 qrels）：

| 路线 | 实际维度 | nDCG@10 | Recall@10 |
|---|---:|---:|---:|
| BM25 | — | 0.4250 | 0.5000 |
| BGE F16 纯向量 | 512 | 0.8517 | 0.9444 |
| BGE Q8 纯向量 | 512 | 0.8530 | 0.9444 |
| Qwen Q8 纯向量 | 1024 | 0.8837 | 0.9583 |
| BGE F16 + BM25 RRF | 512 | 0.7158 | 0.9444 |
| BGE Q8 + BM25 RRF | 512 | 0.7166 | 0.9444 |
| Qwen Q8 + BM25 RRF | 1024 | 0.7141 | 0.9167 |

不限长桶（BGE 两个版本相同输入与 qrels）：

| 路线 | nDCG@10 | Recall@10 |
|---|---:|---:|
| BM25 | 0.8362 | 0.7759 |
| BGE F16 纯向量 | 0.9679 | 0.9468 |
| BGE Q8 纯向量 | 0.9679 | 0.9468 |
| BGE F16 + BM25 RRF | 0.9434 | 0.9343 |
| BGE Q8 + BM25 RRF | 0.9438 | 0.9343 |

Qwen 在当前旧 CPU 处理长输入耗时过高，未完成不限长桶；没有伪造该模型的长文成绩。
不根据 24 查询中约 0.03 的 nDCG 差断言 Qwen 显著优于 BGE；F16/Q8 的小差异也不能
当作量化提升质量。RRF 两桶均弱于纯向量，说明“融合必然更好”在本实验不成立。
这轮不调 RRF 权重，不把 dev 成绩当测试集优化；默认 hybrid 开关继续关闭，生产只做可回退调试。
后续在独立开发集验证融合策略，再用冻结测试集验收。

逐查询指标、排名、manifest、输入/代码哈希与实际 profile：

- 短文：[BGE F16](report/baseline_t2_short_bge_f16.json)、[BGE Q8](report/baseline_t2_short_bge_q8.json)、[Qwen Q8](report/baseline_t2_short_qwen_q8.json)。
- 不限长：[BGE F16](report/baseline_t2_bge_f16.json)、[BGE Q8](report/baseline_t2_bge_q8.json)。
- [服务观测](report/baseline_embedding_runtime.json)、[认证与增量操作](report/baseline_embedding_operations.json)。

BGE 的最终报告重跑复用已有文档向量，build_seconds 不能解释为首次建库性能；
Qwen 的最终报告在新 NFC profile 空间首次嵌入 117 个片段，详见 index_update。
query_seconds 为预热模型、每进程空查询缓存的顺序查询端到端耗时（含向量读取和排序），
不是纯推理吞吐。开发机 2 核/2,468MiB/无 AVX2，并发开发负载未隔离；不据此推算 4 核性能。
BGE Q8/F16 建库后单次 RSS 快照约 62/79MiB，仅进程快照，不是峰值或整机内存。
Qwen 先前探测峰值约 808MiB且发生换页，见 [首轮记录](scifact-local-baseline.md)。
目标机需重新测内存峰值、P95 与业务影响；文件小、向量维度低不等于性能验收通过。

## 复现

在隔离开发 venv 安装锁定 dev 依赖与数据 extra；生产推理只需 embedding-tokenizer extra。

```bash
uv sync --locked --extra rag-data
.venv/bin/python eval/prepare_t2_subset.py --source-dir eval/data/t2-source \
  --download --output eval/data/t2-zh-dev
.venv/bin/python eval/prepare_t2_subset.py --source-dir eval/data/t2-source \
  --output eval/data/t2-zh-short --max-document-chars 128
```

已有目录拒绝覆盖。离线复现去掉 `--download`，源文件需全部匹配固定 SHA。
每个模型按 [部署手册](../docs/rag-production-debug.md) 准备、校验、启动；同一台小内存机
一次只跑一个模型。以下是 BGE Q8 示例，评测密钥通过受保护环境注入 `EMBEDDING_API_KEY`：

```bash
.venv/bin/python eval/model_retrieval_run.py eval/data/t2-zh-short \
  --base-url http://127.0.0.1:18081/v1 --model bge-small-zh-v1.5-q8_0 \
  --profile /opt/saudade-embedding/bge-q8.json --expected-dim 512 \
  --index-dir eval/cache/t2-short-bge-q8 \
  --output eval/report/baseline_t2_short_bge_q8.json
```

另外两模型替换 model/profile/expected-dim（Qwen=1024），各用独立 index-dir。
不限长桶替换数据目录和输出名。报告保留全部查询，向量建库或查询 embedding 失败会中止；
合法的零召回仍按零分计算，不删除查询。TREC run 导出至 `eval/report/runs/<报告名>/`。
源语料、GGUF、向量缓存和运行日志均不入库。报告代码 SHA 指向本轮冻结的相关检索模块与 runner；
Qwen 使用资产脚本生成、声明 NFC 的 profile，在新 namespace 独立建库。

项目只有几篇文章也可建小而针对性的人工测试：先标文章、章节、所需证据和无答案，
持续加入真实失败查询。公开语料补充通用中文检索回归，自有案例覆盖业务语义。
最终上线调试和验收步骤见 [生产调试手册](../docs/rag-production-debug.md)。
