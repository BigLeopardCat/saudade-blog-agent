# 本地BGE部署与验收：2026-10-27

目标机已启用BGE Q8低流量试点。生产Agent仍使用OpenCode Go的DeepSeek low生成回复，
embedding改由本机独立服务提供；RAG为BM25和向量结果的RRF融合。模型不可用时退回词法，
不自动调用付费embedding API。向量图谱没有在本轮重建。

## 配置与可复现资产

- 4vCPU Xeon Platinum8255C，AVX2/AVX-512/VNNI；实际内存约3.6GiB，无GPU。
- 固定llama.cpp b11829；BGE-small-zh-v1.5 Q8使用已登记的第三方GGUF转换，
  不是BAAI官方量化权重。runtime、权重、官方tokenizer的revision和SHA256见
  [资产清单](../deploy/embedding/artifacts.json)。
- 原生512维，CLS pooling、L2归一化；查询前缀、完整512-token预算和normalizer修正
  固定在`/opt/saudade-embedding/bge-q8.json`。5组逐token对齐均通过。
- `saudade-embedding@bge-q8.service`只监听loopback18081，使用独立API key、systemd
  credential及DynamicUser。无密钥和错误密钥均返回401，密钥没有进入代码或命令行。
- 单推理线程、并发1、CPUQuota100%、CPUWeight20、Nice5；MemoryHigh1GiB、
  MemoryMax1280MiB、禁用服务swap。Agent通过可选drop-in弱依赖模型，模型停止不停止Agent。
- Agent加载私有`/etc/saudade/agent-embedding.env`，独立索引位于
  `/var/lib/saudade-agent/rag-bge-q8`。原有博客公开地址、对话模型及身份配置保留。

这沿用现有OpenAI兼容embedding客户端、LangChain/LangGraph运行时和检索入口，
没有新建模型网关或通用任务平台。TEI仍为候选，不写成已部署。

## 功能与故障验收

| 检查 | 结果 |
| --- | --- |
| 当前公开语料 | 13篇文章，231个向量输入 |
| 首次建库 | 231次本地调用，512维，missing=0 |
| 原样重跑 | 231条复用，新增0，调用0 |
| 复制索引后改变一个切片 | 新建1、复用230、本地调用1 |
| 复制索引后移除该切片 | 活跃230、调用0 |
| 模型停止 | 有词法结果，route=degraded/query_embed_failed |
| 严格要求混合的故障查询 | 返回非零退出码，没有假报hybrid成功 |
| 模型恢复 | 查询恢复hybrid，missing=0 |
| Agent健康检查 | ready=true，rag_hybrid_active=true，rag_state=active |

增量修改和删除使用索引副本与合成切片，没有修改生产文章。这验证索引行为，
不等同于真实文章编辑、发布与可见性变化的完整端到端验收。
最初运维shell中的HTTP代理使本机预检超时，保留失败记录；设置localhost的NO_PROXY后通过。
生产systemd服务不继承该shell代理。

## 性能和质量边界

单线程配置的100次本地请求（包含长中文、中英混合）无失败：P50约30ms、P95约275ms，
最高578ms。模型进程RSS/HWM约61MiB，cgroup读数和文件映射归属不同，不拿较小的
cgroup MemoryCurrent冒充进程RSS。此前双线程配置的数据也保留，未以复跑覆盖。

同时采样宿主公开语料接口各100次：无模型请求时P95为7.70ms，同时embedding时为8.67ms。
绝对差约0.96ms，增幅约12.5%，未严格达到此前拟定的≤10%目标。样本是单机顺序探针，
不是稳定容量或SLA证明；当前按维护者授权用于低流量试点，后续仍需更稳定的并发与业务验收。

16条人工标注站内开发查询使用现有检索实现与ir-measures比较，未调融合权重：

| 模式 | Top1命中至少一篇标注文章 | Recall@3 | nDCG@10 |
| --- | --- | --- | --- |
| 词法 | 15/16 | 1.0000 | 0.9758 |
| BGE纯向量 | 16/16 | 0.9479 | 0.9903 |
| 当前RRF混合 | 15/16 | 1.0000 | 0.9808 |

查询和标注在调用前固定。这是小型开发冒烟集，不是留出集、官方榜单或泛化质量证明。
融合不保证胜过纯向量，也不因服务可用就宣布任务质量已达企业指标。

## 实现边界与后续工作

BM25与RRF是标准算法，目前BM25分词、倒排和打分仍由项目实现；RRF约二十行排名与
来源位置合并逻辑，公式为两路各加`1/(k+rank)`，排名从1开始。见
[Elasticsearch的RRF说明](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion)。
中文n-gram、少量同义扩展与候选相对截断属于本项目策略，不能宣称是标准框架默认行为。
后续应以成熟BM25库替换通用索引与打分内核，保留语料、权限、版本和证据接口，
用固定任务及资源对照决定切换；不因接了LangGraph便把整套RAG称为框架实现。

检索实际模式和降级原因写入请求trace的`rag.retrieval`事件，不包含查询、端点或凭据。
完整运维步骤与退役/切换见[调试手册](rag-production-debug.md)。回退时移除本次安装的
`local-embedding.conf`，daemon-reload并重启Agent恢复原配置，再停用模型实例；保留索引方便恢复。

原始预检、建库、复用、故障、资源与16条查询报告保存在运行机
`saudade-bge-*-20261027`材料中。它们与此前其他机器的模型对照分别留档，不混合计分。
