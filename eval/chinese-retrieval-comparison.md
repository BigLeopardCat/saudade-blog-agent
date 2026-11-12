# 中文检索与博客章节证据对照

本报告固定数据、BGE 模型、切片和候选预算，对比 BM25、纯向量与 RRF 混合检索。
只评估检索器候选和返回位置，未应用工具结果 token 裁剪、实时公开性验证、正文读取或回答生成。
结果不能替代 Agent 任务成功率；本轮没有调用 LLM Judge。

## 数据与标注

| 数据 | 候选语料 | 查询 | 用途与限制 |
| --- | --- | --- | --- |
| MTEB DuRetrieval 固定子集 | 9,209 篇 | 200 | 来自源 dev；包含全部选中问题的原始相关性标注、8,192 篇固定背景及词法干扰候选；不是官方全库成绩 |
| 博客章节证据 dev | 全部 10 篇公开文章 | 13 | 项目开发标签，含 11 个有证据问题 |
| 博客章节证据 test | 同一完整公开快照 | 57 | 54 个有证据问题；按问题家族隔离，仅作为本轮公开评估集，不声称独立盲测 |

公开集固定 MTEB revision `8d9b3d6a6a62b7fd2d4df0821d612366dc52d14e`。
问题由 SHA-256 固定排序抽取，保留每题全部原始分级标注；不按长度筛选或截断文档。
候选缩减和按 BM25 选择干扰项改变了任务难度；未标注不等于负例。
源卡片未声明语料许可，仓库不再分发公开数据正文。准备入口校验三份原始 parquet 的固定 SHA。

博客快照与问题见 [数据说明](fixtures/blog_evidence/README.md)。70 题中 65 题含精确原文证据，
5 题是虚构标识符的无答案观察；家族按固定哈希分为 dev/test，本轮没有根据分数调参。
问题和范围由项目 Agent 编写，加载器只验证源版本、节内坐标与精确原文，尚待独立人工语义复核。
历史架构与项目介绍按当时文档内容提问，不当作当前生产事实。指定证据位置不是全部相关位置。

## 固定配置

- BGE small zh v1.5 Q8_0，512 维；GGUF SHA：`5a88d266870fbd27c6f329df60de80e2d4cf3bbd5e6f080bd5c1b2e5abb12039`。
- llama.cpp b11829，CLS pooling、L2 归一化；独立本地实例、1 线程、1 slot、nice 15。
- 完整输入按固定 tokenizer/profile 的 512 token 预算细分；正文不由评测器截断。
- 复用生产章节切片、BM25S、文档聚合及 RRF；每路候选 24，交付 top-8，评分 k=1/3/8。
- 独立 PostgreSQL 16.15 / pgvector 0.6.0，精确余弦检索；独立 collection，不读写生产向量集合。
- 硬件与业务共存，不是隔离容量测试。每个数据运行顺序测词法、向量、融合；向量阶段包括查询
  embedding、数据库访问及结果聚合，不能把它当作纯 SQL 时间。评测进程 RSS 不包含独立模型和数据库进程。
- dev 首次构建博客 collection，test 复用相同语料向量；这是无标签的语料编码复用，不是 test 标签训练。

所有数据文件哈希、源代码摘要、模型空间、实际维度、输入片段数、嵌入新增/复用和逐题耗时保存在原始报告。

## 检索结果

公开集（200 题，未用于本轮调参）：

| 检索方式 | Success@1 | Success@8 | Recall@8 | nDCG@8 |
| --- | ---: | ---: | ---: | ---: |
| BM25 | 80.0% | 94.5% | 0.7190 | 0.7375 |
| 纯向量 | 91.0% | 98.0% | 0.8798 | 0.8874 |
| RRF 混合 | 91.5% | 97.0% | 0.8412 | 0.8602 |

纯向量相对 BM25 的 Recall@8 平均提高 0.1607，查询级配对 bootstrap 95% 区间
[0.1228, 0.1951]。混合相对纯向量下降 0.0385，描述性区间 [-0.0595, -0.0163]；
其 nDCG@8 也下降 0.0272。混合的 top-1 略高，并不能证明更完整的候选窗口也更好。
这些是固定缩减候选库下的比较，不外推到原始全库或生产成功率。

博客 test（57 题，其中 54 题有指定证据、3 题无答案观察）：

| 检索方式 | 文章 Success@8 | 文章 nDCG@8 | 指定证据平均定位覆盖 | 全部指定证据均覆盖 |
| --- | ---: | ---: | ---: | ---: |
| BM25 | 100% | 0.9932 | 95.37% | 51/54 = 94.44% |
| 纯向量 | 100% | 0.9841 | 86.11% | 45/54 = 83.33% |
| RRF 混合 | 100% | 1.0000 | 97.22% | 52/54 = 96.30% |

这里的定位覆盖指返回范围能够取回选定原文，不表示原文已送入模型或已被读完。
小语料 top-8 下文章命中很容易，章节指标才暴露差异。混合比 BM25 仅多覆盖一题，
配对差值区间跨零；不能称为稳定优势。dev 的 11 个有证据题完整覆盖分别为
BM25 10/11、纯向量 8/11、混合 11/11；dev/test 分开保留，不合并宣传独立成绩。

### 证据位置预算敏感性

文档窗口相同，不代表证据量相同。两路各保留每篇最多 3 个位置，混合会合并这些位置。
在全部 57 次 test 查询中，每次平均返回位置数为 BM25 17.18、纯向量 19.53、混合 31.75。
因此原始混合覆盖率的提升也包含更多位置的代价，不能直接宣称等 token 成本更优。

保留同一冻结文章排名，按原有顺序将每篇位置截到 3 个，以现有指标函数离线重算：

| 方式 | 原生位置预算：全部证据覆盖 | 每篇前 3 个位置：全部证据覆盖 |
| --- | ---: | ---: |
| BM25 | 94.44% | 94.44% |
| 纯向量 | 83.33% | 83.33% |
| 混合 | 96.30% | 92.59% |

这是一项显式预算诊断，不是重新检索、标签驱动的最佳位置选择或生产策略改动。
它说明下一步应先验证位置选择和上下文预算，不能只凭原生覆盖率决定增加重排模型。

### 分阶段耗时与索引成本

| 数据 | 词法阶段 P95 | 向量阶段 P95 | 融合阶段 P95 |
| --- | ---: | ---: | ---: |
| 中文公开集，200 次 | 83.0ms | 308.7ms | 1.67ms |
| 博客 test，57 次 | 88.0ms | 132.7ms | 2.57ms |

向量阶段还包含首次输入细分、空间指纹和位置聚合等准备，不能归为纯 SQL 或模型服务时间。
公开集该阶段最大值 10.57 秒、博客最大值 1.76 秒，冷准备长尾保留；当前分段不足以精确
归因这两条最大值，不扣掉它们美化结果。数据共享机器且没有并发容量测试。

公开集最终评分复用了上一轮已提交的 10,955 个唯一输入向量：索引更新未调用 embedding，
200 个查询仍需要查询向量。博客 dev 新嵌入 274 个唯一输入、28 次索引 API 请求；
test 复用同一语料向量，索引 API 请求为 0，查询 embedding 与索引构建分别统计。

| 尝试 | 进程总耗时 | 阶段结果 | 质量查询数 |
| --- | ---: | --- | ---: |
| 公开集第 1 次 | 32:55.30 | 嵌入完成，发布清理超时，事务回滚 | 未评分 |
| 公开集第 2 次 | 33:13.26 | 发布成功，首条查询遇到冷统计慢计划 | 未评分 |
| 公开集第 3 次 | 1:54.65 | 复用已提交向量，首次完整评分 | 200 |
| 博客 dev | 0:57.98 | 构建及评分完成 | 13 |
| 博客 test | 0:16.06 | 复用语料向量及评分完成 | 57 |

前两次基础设施失败留在运行账中，不被包装成成功运行，也不混成问题答错。
首次失败已计算 10,955 个输入，观察到约 306 万本地引擎 token，未提交的结果随事务回滚；
其 HTTP 请求数未留存，保持未知。第二次冷构建有 1,096 次索引 API 请求。
整个独立模型实例包含探针、失败与成功运行共 22,457 个输入、约 621 万引擎 token，
没有截断；进程累计 CPU 约 3,859 秒。这里是本地计算观察，不是服务商账单或人民币节省。
最终评测进程峰值 RSS 公开集约 578MiB，冷构建约 1,118MiB；复用缓存和冷构建不是等条件内存对照。

## 失败阶段与错例

归因复用原始三路 run，不调用回答模型：top-k 未命中已标注相关文章、多文必要证据不全、
命中文章但指定原文范围未覆盖。未捕获完整候选池，无法只凭这些分类区分初召回与最终排序截点。
公开集 BM25/纯向量/混合分别有 11/4/6 题未在 top-8 命中已标注正相关文档。
混合相对纯向量的 Recall@8 逐题为 12 题提升、38 题下降；nDCG@8 为 39 题提升、68 题下降。
代表例子保留在源审计中：

- `2a353d...`「含娟的成语」：纯向量 Recall@8=1.0、混合=0.2。
  词法特征实际为「含娟、成语、娟的、的成」，没有独立「娟」；这是分词/特征选择边界，
  不能归咎于 BM25S 库本身，也没有据此新增单题规则。
- `649d02...`「为什么京东商城打不开」：纯向量 Recall@8=0.5、混合=0；
  保留了已标注正例被融合挤出窗口的原始排名，未标注词法候选仍不直接判成无关。

博客 BM25 的 3 个指定位置缺口，目标章节分别排文内第 4、第 5、第 4，落在每文 3 个位置之外。
混合补回了 `blog-042` 的图谱指标位置，但 `blog-032`（OTA 信息认证）和 `multi-03`
（Git 分支与 asyncio Future）仍缺部分指定位置。后者已经命中两篇文章，却未覆盖 Future 定义，
说明多文文章命中不等于必要证据齐全。源审计保留了章节排名、来源版本和人工复核提醒。

5 道无答案题三路都返回了候选；没有生成回答，无法据此判断模型会误答还是正确弃答。
候选供给仅作观察，不评为幻觉或弃答失败。查询级配对 bootstrap 仅作描述；
共享文档与问题家族存在相关性，不作生产概率或 SLA 证明。

## 规模实验发现的索引发布问题

第一次公开集已完成本地嵌入，但在发布事务清理旧向量时触发 10 秒数据库语句超时，未进入查询评分。
保留失败日志、资源统计和输入计数，不把失败花费补零，也不把它作为答错用例删除后计算通过率。

根因是活动成员查询关联了外层向量维度，会逐行执行；新集合刚批量插入时统计还未更新，
普通反连接也可能选到重复扫描计划。修复将活动成员写成无相关子查询的 `(key, dimension)` 集合，
并为成员外键添加联合 B-tree 索引，避免删除时每行外键检查全表扫描。没有提高语句超时。

独立 SQL 检查使用 20,000 条合成二维向量，不调用模型、不计检索质量：完整发布约 2.01 秒，
清理一半约 0.70 秒，单独清理计划执行约 23.6ms；执行计划确认成员子查询仅运行一次。
此外，新集合在自动统计前的普通关联会选择慢计划；第二次真实运行在 12:12:02 查询超时，
自动统计于 12:12:07 完成，同一未修改查询之后约 82ms。距离计算改为成员到向量主键的标量点查，
仍在 PostgreSQL 内计算精确余弦并按文章聚合。最终 20k 合成检查首次查询约 0.163 秒，
计划显示每次点查使用 collection/key/dimension 三列主键条件，避免集合级重复扫描。

这些是本机单次观测，不作跨硬件延迟承诺。规模集成测试同时验证保留维度、删除旧键、
新集合查询与完整快照。测试通过返回列的 vector 类型 OID 检查没有向 Python 拉取原始向量，
不依赖 SQL 文本长相。

已有表在部署新代码前，以迁移账号显式运行 `rag.manage setup` 补索引，见
[向量存储升级](../docs/deployment.md#向量存储)。运行账号和查询请求不执行 DDL。
数据库发布事务失败时，本批尚未提交的成功嵌入也会回滚；首次实验确实产生了这笔重建成本。

## 复现

在独立开发环境检出报告对应提交，安装锁定工具链：

```sh
uv sync --locked --extra rag-data --extra embedding-tokenizer
```

按[固定资产与输入配置](../docs/embedding-inputs.md)准备相同 BGE GGUF、tokenizer、profile 和独立模型服务。
将凭据仅放入环境变量，设置 `EMBEDDING_API_KEY` 和独立可丢弃数据库的 `SAUDADE_RAG_EVAL_DSN`。
以有迁移权限的账号初始化该评测库，随后使用评测账号；不要指向生产数据库。

```sh
RAG_VECTOR_DSN="$SAUDADE_RAG_EVAL_DSN" SAUDADE_IGNORE_ENV_FILE=1 python -m rag.manage setup
```

下载固定公开源文件并准备同一子集：

```sh
python - <<'PY'
import subprocess
from pathlib import Path
from eval.prepare_duretrieval import SOURCE, REVISION, FILES
root = Path('eval/cache/du-pinned')
for name in FILES:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['curl', '--fail', '--location',
                    f'{SOURCE}/resolve/{REVISION}/{name}', '-o', str(path)], check=True)
PY
python -m eval.prepare_duretrieval --source-dir eval/cache/du-pinned \
  --output eval/data/du-fixed --queries 200 --background 8192 --pool 10000
```

运行三路，模型服务 URL/profile 路径按独立环境填写。三个数据运行使用相同模型与检索参数：

```sh
SAUDADE_IGNORE_ENV_FILE=1 python -m eval.model_retrieval_run eval/data/du-fixed \
  --split dev --base-url http://127.0.0.1:19183/v1 --model bge-small-zh-v1.5-q8_0 \
  --profile /path/to/bge-q8.json --expected-dim 512 --collection eval-du-replay \
  --output /tmp/du-replay.json
# 将 dataset 改为 eval/fixtures/blog_evidence，分别使用 --split dev/test，
# 两次共用 --collection eval-blog-replay，输出到不同文件。
python -m eval.retrieval_comparison eval/data/du-fixed /tmp/du-replay.json \
  --split dev --cutoff 8 --output /tmp/du-attribution.json
```

原始报告中的 `arms.run` 和 `arms.locations` 允许不调用 embedding 重算指标：

```python
import json
from pathlib import Path
from eval.rag_dataset import load
from eval.offline_rag_eval import evaluate
report = json.loads(Path('/path/to/frozen-report.json').read_text())
data = load(Path('/path/to/matching-dataset'), report['split'])
assert data['hashes'] == report['input_hashes']
for name, arm in report['arms'].items():
    replay = evaluate(data, arm['run'], report['config']['cutoffs'], locations=arm['locations'])
    assert replay['summary'] == arm['summary']
```

重新调用模型可能受到硬件、数值实现及运行负载影响，时间不保证相同；冻结 run 的指标应一致，跨平台浮点比较允许 `1e-12` 误差。

## 冻结产物与结论边界

- [公开集原始三路报告](report/baseline_du_retrieval_20261112.json)、[配对归因](report/baseline_du_attribution_20261112.json)、[代表错例](report/baseline_du_source_audit_20261112.json)。
- [博客 dev](report/baseline_blog_evidence_dev_20261112.json)、[博客 test](report/baseline_blog_evidence_test_20261112.json)、[配对归因](report/baseline_blog_evidence_attribution_20261112.json)。
- [章节缺口源审计](report/baseline_blog_evidence_source_audit_20261112.json)、[位置预算诊断](report/baseline_blog_location_budget_20261112.json)。
- [运行账、代码/数据校验、SQL 计划及失败消耗](report/baseline_rag_benchmark_operations_20261112.json)。

三个有效报告使用检索代码 `168e9e45c264d8ed3eb7ef965f170e7afe3f92b3`，相关源文件哈希已核验。
270 道查询的三路指标已从冻结 run 与位置数据重算，汇总完全一致；没有重新调用模型或修改标签。

本轮支持的结论是：公开中文子集验证了语义路的收益，也显示等权 RRF 会退化；小博客的
文章级高分不能替代章节定位评估，混合覆盖还受位置数量影响。当前没有足够证据宣布某一
策略在所有场景最好。后续如调融合或位置预算，先固定开发目标、成本和新验收材料，再做对照。
回答事实性、模型可见上下文与真实业务效果依旧未在本轮验收，语义审查保持暂停。
