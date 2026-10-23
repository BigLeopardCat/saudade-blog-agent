# RAG 本地 embedding 与生产机调试

类型：§0–§5 为现状与操作步骤；§6–§8 为待目标机验证的门槛、引擎候选与验收记录。

本文适用于另一台 **4 核、4GB、Linux x86_64** 服务器，Python 3.12、systemd ≥249。
本轮只在开发机验证；生产机部署与业务灰度尚未执行。先部署到隔离发布目录，保留当前服务版本。
评测结果和来源见 [中文模型对照](../eval/t2-chinese-baseline.md)，输入预算见
[embedding-inputs.md](embedding-inputs.md)。

## 0. 两台机器的配置与验证边界

开发机实测为 **2 vCPU、Xeon E5-2695 v2 @ 2.40GHz、约 2,468MiB RAM**，
Ubuntu 24.04.3 / Linux 6.8.0-147-generic、Python 3.12.3、systemd 255；有 AVX，
未暴露 AVX2/AVX-512。开发环境有约 3,327MiB swap，不能将其计入模型的常驻 RAM 预算。

用户提供的目标机为 **4 vCPU、Xeon Platinum 8255C @ 2.50GHz、KVM、单 NUMA 节点**，
暴露 AVX2、AVX-512、VNNI，未暴露 AVX512_BF16 / AMX；内存按此前的 4GB 规划。
操作系统、systemd、实际可用内存与磁盘尚未采集。完整对照和盘点来源见
[评测环境表](../eval/t2-chinese-baseline.md#环境补充与目标机差异2026-10-23asiashanghai)
及 [环境 JSON](../eval/report/baseline_embedding_environments.json)。
这些指令集允许评估不同的 CPU 后端，但不构成速度或内存验收结果。

在目标机部署前记录以下输出；命令不打印环境变量、凭据或进程启动参数：

```bash
TZ=Asia/Shanghai date --iso-8601=seconds
lscpu
free -m
uname -srmo
systemd --version
df -h .
.venv/bin/python --version
```

核对 cgroup 资源控制是否生效，并记录现有业务负载；记录单位须分清 GB/GiB/MiB。
公开报告只摘录与评测有关的字段，不附主机名、网络地址或服务的完整 Environment。

## 1. 维度和存储

`EMBEDDING_DIM=0` 表示不请求降维，采用返回的原生维度，不是零维：BGE-small-zh-v1.5
为 512，Qwen3-Embedding-0.6B 为 1024。二者均已实测。当前文章规模无需为节省向量存储
降维：10,000 个 float32 向量约占 19.5/39.1 MiB（512/1024 维，仅原始向量）。
维度大小不能跨模型代表检索质量；先保留原生维度，根据固定评测选择模型。
Qwen 支持 Matryoshka 降维，但降维不会缩小模型权重，这轮没有做降维质量实验。
不能直接截断 BGE 向量，也不能把不同模型的向量混用。

本项目 RAG 目前使用本地不可变向量文件代、内容缓存与进程内精确余弦检索。
PostgreSQL 用于 Agent checkpoint，不是当前 RAG 向量存储；没有 pgvector 迁移要求。
模型、端点、请求维度、输入 profile 构成空间身份。正文/标题变化只嵌新输入；删除文档
只更新活跃集合。换模型/profile 是一个新空间，首次需要建库；重复执行应为零新增请求。

## 2. 运行时与资产

复用 llama.cpp 的 OpenAI 兼容 embedding 服务；没有新增自研推理网关。
优先调试 **BGE Q8**（中文资源成本较低），BGE F16 为量化对照，Qwen Q8 为质量候选。
评测不足以决定所有站点的最终模型，API 通道继续保留。

固定版本、原始模型/转换仓库、SHA-256 和下载大小上限统一放在
[artifacts.json](../deploy/embedding/artifacts.json)。BGE 使用第三方 GGUF 转换，
不是 BAAI 发布的官方 GGUF。它的默认大小写/重音设置与官方 tokenizer 有差异，
模板通过 llama.cpp `--override-kv` 显式设置两个 normalizer 选项为 false。
Qwen 的输入 profile 另显式启用 NFC（官方 tokenizer 的行为），用标准 Unicode 库
规范化实际 embedding 输入以对齐 GGUF 服务；原文、章节偏移不变。
这个服务配置也写进 profile 身份；不得省略模板中的 `TOKENIZER_OVERRIDES`。
升级 runtime、权重、tokenizer、pooling 或归一化后重新预检并使用新 profile/索引目录。

以下命令在目标机的仓库发布目录执行，不要在正在跑业务的虚拟环境内 `uv sync`：

```bash
uv sync --locked --no-dev --extra embedding-tokenizer
sudo install -d -m 0755 /opt/saudade-embedding
sudo .venv/bin/python scripts/prepare_embedding_assets.py \
  --directory /opt/saudade-embedding --model bge-q8 --download
sudo .venv/bin/python scripts/prepare_embedding_assets.py \
  --directory /opt/saudade-embedding --model bge-q8 --verify-only
sudo chmod -R a+rX /opt/saudade-embedding
```

`--download` 是显式网络操作；默认只校验本地资产。运行文件按版本目录安装，重复准备
不会覆盖正在执行的二进制。校验失败即停止，不能忽略 SHA 错误继续启动。
Qwen/F16 分别用 `--model qwen-q8` / `--model bge-f16`，一次只运行一个实例。
生产下载应由维护者执行；Agent 和模型服务运行账号都不应有资产目录写权限。

## 3. 模型服务

```bash
sudo install -d -m 0755 /etc/saudade
sudo install -m 0644 deploy/embedding/saudade-embedding@.service /etc/systemd/system/
sudo install -m 0600 deploy/embedding/bge-q8.env.example /etc/saudade/embedding-bge-q8.env
sudo sh -c 'umask 077; openssl rand -hex 32 > /etc/saudade/embedding-api-key'
sudo systemctl daemon-reload
sudo systemctl start saudade-embedding@bge-q8
sudo systemctl status saudade-embedding@bge-q8 --no-pager
```

仅首次创建密钥；已有密钥轮换需同步 Agent 配置。不要将密钥放进命令行、提交或报告。
模板只监听 `127.0.0.1:18081`，启用 API key、禁用 Web UI，以 systemd credential 传密钥。
DynamicUser 只读资产；并发 1、2 个推理线程、512-token 完整输入上限。
CPUQuota=200%，MemoryHigh=1G、MemoryMax=1280M、无服务 swap。
这些是资源约束，不是性能验收结果。目标机若架构、系统版本或 CPU 指令集不同，先验证
官方二进制兼容性；启动失败时查 `journalctl -u saudade-embedding@bge-q8`。
日志可能有内部路径，上传前脱敏。

本轮未在目标机安装 unit。模板的系统级启动、cgroup 限额与重启恢复须在那里验收。
首次调试用 `start`；通过整套验收后再决定是否 `enable` 开机启动。

## 4. Agent 配置和操作命令

把下面字段写入 `/etc/saudade/agent-embedding.env`，仅实际 Agent 运行账号可读。
`BLOG_API_BASE` 必须指向自己的宿主后端公开 API 根路径，不能沿用仓库示例站点。
宿主需具有版本化 `/api/public/rag/corpus` 接口（宿主 RAG 协议改动已合并）。

```dotenv
RAG_HYBRID_ENABLED=1
EMBEDDING_MODEL=bge-small-zh-v1.5-q8_0
EMBEDDING_BASE_URL=http://127.0.0.1:18081/v1
EMBEDDING_API_KEY=填入上一节生成的密钥
EMBEDDING_DIM=0
EMBEDDING_PROFILE=/opt/saudade-embedding/bge-q8.json
EMBEDDING_BATCH_SIZE=1
EMBEDDING_TIMEOUT=3
RAG_VECTOR_DIR=/var/lib/saudade-agent/rag-bge-q8
BLOG_API_BASE=http://127.0.0.1:8080/api/public
```

`8080` 是示例端口，改成宿主实际端口。3 秒是初始故障等待上限，按目标机延迟验收调整。
用受保护的编辑器或脚本从密钥文件读取，禁止把密钥复制进 shell 命令或输出。
创建向量目录并设为实际 Agent 用户所有，保证有足够空间保留新旧向量代。
在现有 Agent systemd unit 的 drop-in 中增加
`EnvironmentFile=/etc/saudade/agent-embedding.env`；在放行业务前先独立运行下列命令。
CLI 以实际 Agent 用户、发布目录和同一虚拟环境执行，配置文件权限应允许该用户读取。

```bash
.venv/bin/python -m rag.manage --env-file /etc/saudade/agent-embedding.env \
  probe --expected-dim 512 --llama-tokenize
.venv/bin/python -m rag.manage --env-file /etc/saudade/agent-embedding.env prepare
.venv/bin/python -m rag.manage --env-file /etc/saudade/agent-embedding.env \
  prepare --execute --max-new 200
.venv/bin/python -m rag.manage --env-file /etc/saudade/agent-embedding.env \
  prepare --execute --max-new 0
.venv/bin/python -m rag.manage --env-file /etc/saudade/agent-embedding.env \
  status --require-vectors
.venv/bin/python -m rag.manage --env-file /etc/saudade/agent-embedding.env \
  query '替换成自己文章能回答的问题' --require-hybrid
```

第一次 `prepare` 是只读建库计划，不调用 embedding；`--execute` 才允许嵌入。
`200` 是示例新输入预算，按预览结果明确设置，不能直接无限放大。
预算约束新增/重试输入数，不是 API 账单金额（SDK 重试仍可能多发请求）。
第二次执行要求零新输入；原语料不变时 `api_calls=0`。严格命令失败返回非零退出码。
`probe --llama-tokenize` 比较 5 组 token ID（含中文、混合、特殊字符和接近上限文本），
不能只检查 HTTP 200 或向量长度。API 提供商不支持该端点时不加此选项。
输出 JSON 不含 key/端点；查询文本、命中文章仍可能含业务信息，不要直接公开真实调试输出。

`status` 只证明磁盘向量空间可用，不代表它与刚发布的文章一致；`query --require-hybrid`
会验证当前语料的指纹对齐和真实检索路线。Agent 的 `/ready` 检查不能代替向量就绪检查。
先完成独立预检，再按现有发布流程重启 Agent，核对 `/version`、`/ready` 和真实检索路线。

长文仍按标题路径和原文范围切分；向量窗口按完整输入（标题、指令、特殊 token）进一步
细分，不截断正文。多篇参考先检索文章，再按 revision/section/offset 分页读正文；
跨篇答案需逐篇取得证据，向量 Top-K 本身不构成答案。相关行为已有跨仓回归。

## 5. API、切换和回退

| 模式 | 配置要点 | 预检 |
|---|---|---|
| BM25 | `RAG_HYBRID_ENABLED=0` | `query` 应走 lexical，零 embedding |
| BGE 本地 | 原生 512，CLS + L2，bge-q8 profile | `probe --expected-dim 512 --llama-tokenize` |
| Qwen 本地 | 原生 1024，last + L2，qwen-q8 profile | `probe --expected-dim 1024 --llama-tokenize` |
| 远程 API | 自己的 model/base/key，原生维度与提供商一致 | `probe --expected-dim 实际维度` |

切本地模型时先停止旧模型实例，再安装对应 `.env.example`、启动新实例。Agent 的
`EMBEDDING_MODEL`、`EMBEDDING_PROFILE`、专用 `RAG_VECTOR_DIR` 同步切换。
Qwen 与 BGE 不共用目录；本地与 API 也不共用目录。API 不要求本地推理条件，且不需要
安装 PyArrow。已知 API tokenizer/输入预算可配置相应 profile；不匹配时不能套本地 profile。
API key 应按提供商的账号权限和配额管理；本轮没有使用用户的付费 API 做对照。

故障回退验收：保持向量目录，临时停止自己的模型实例，再执行 `query`，应返回词法结果
并在 route 中报告 degraded/query embedding 失败；严格 `--require-hybrid` 应失败。
恢复模型并验证 hybrid。彻底回退时设 `RAG_HYBRID_ENABLED=0` 并重启 Agent，确认 lexical；
保留缓存以便复原。不自动切付费 API，避免隐式费用。
发布异常则恢复上一份已验证代码与其配置；本次无数据库结构迁移。

## 6. 目标机验收和留档

以下是拟定门槛，**尚未在 4 核 4GB 服务器上通过**：

- 使用自己的语料完成 probe、预览、首次建库、零请求重跑、修改一篇仅补变化输入、删除后不再命中。
- 模型停止/重启、超长输入、无效 key、语料接口不可用均能观察到明确降级，恢复后可重新对齐。
- 单模型顺序请求，预热后至少 100 次查询记录 P50/P95；目标 embedding P95 ≤800 ms。
- 使用 `systemctl show` 的 MemoryCurrent/MemoryPeak/CPUUsageNSec 与进程 RSS/PSS 记录资源；
  目标稳定 RSS ≤1 GiB、峰值 ≤1.25 GiB、整机 MemAvailable ≥750 MiB。文件大小不能代替 RSS。
- 同时测宿主原有请求，目标 P95 增幅 ≤10%；若不满足，收紧 CPU/批次、改 BGE，或使用 API。
- 自有文章建 30–50 条人工标注回归：中文改写、中英混合、长文章定位、跨篇聚合、无答案，
  后续留一份冻结测试集；检索看 nDCG/Recall，跨篇看必需证据覆盖，生成看事实支持和引用正确性。

每次记录代码提交、模型/runtime/权重 SHA、profile 签名、实际维度、语料 revision、
新建/复用数、三路指标、延迟/内存、降级和恢复结果。公开语料评测不能替代站点自身验收。
未经这些检查，不把“服务能返回向量”标记为业务上线成功。

## 7. 引擎候选与缓存边界

本节记录 2026-10-23 的讨论与后续验证方案，**不表示 TEI/vLLM 已接入或测过**。
当前资产脚本、unit、`--llama-tokenize` 预检均针对 llama.cpp；本次文档更新不更换引擎。

| 方案 | 本项目状态 | 目标机验证顺序 |
|---|---|---|
| llama.cpp + GGUF | 已在开发机验证；固定 b11829 | 首先复测，作为回退和资源基线 |
| TEI CPU + 官方 BGE 权重 | 未安装/未评测 | 优先新增对照，先 FP32 参考，再讨论量化 |
| ONNX Runtime / OpenVINO INT8 | 未安装/未评测 | 当延迟或资源不达标时评估 VNNI 优化；使用成熟导出工具 |
| vLLM CPU pooling | 未安装/未评测 | 可选实验，证明相对收益后再决定部署 |
| 远程 embedding API | 保留显式配置通道 | 按提供商模型/预算独立预检；不隐式付费回退 |

llama.cpp 有 [Hugging Face 托管部署路径](https://huggingface.co/docs/inference-endpoints/engines/llama_cpp)，
不能只凭引擎名称判断生产适配。TEI 提供 [CPU 镜像和支持模型说明](https://huggingface.co/docs/text-embeddings-inference/en/supported_models)，
并有 [OpenAI 兼容 embedding 接口](https://huggingface.co/docs/text-embeddings-inference/en/quick_tour)。
vLLM 有 [CPU 后端](https://docs.vllm.ai/en/latest/getting_started/installation/cpu/)，
但其 [pooling 文档](https://docs.vllm.ai/en/latest/models/pooling_models/) 明确不保证比
Transformers/Sentence Transformers 更快。上述均为文档能力，具体版本与模型必须实测。
ONNX/OpenVINO 的导出与量化可用 [Sentence Transformers 的成熟工具](https://sbert.net/docs/sentence_transformer/usage/efficiency.html)，
不自行实现 tokenizer、pooling 或推理网关。引用的在线文档会更新，实施时固定镜像 digest /
依赖版本、官方权重 revision 与 SHA，记录对应文档版本；不要直接部署浮动 latest。

缓存须分别验收：

| 缓存 | 当前行为 | 目标机检查 |
|---|---|---|
| 查询向量 LRU | 每 Agent 进程默认 256 条，空间 + 规范化后的输入精确命中，无跨进程共享，重启丢失 | 相同查询首次请求、重复命中、重启后重新请求分别记录 |
| 文档向量缓存 | 本地持久化、内容与空间寻址；只补新输入，删除后更新活跃集合 | 不变语料重跑零请求，改一篇只补变化，删除后不命中 |
| KV / prefix cache | 模板 `--cache-ram 0` 关闭 llama.cpp 额外 prompt 缓存，不等于内部完全无 K/V 状态 | 不把 prompt 缓存与查询向量缓存混称；记录引擎实际参数 |

查询容量见 `EMBEDDING_QUERY_CACHE`；不要把 0 当作当前实现的“完全禁用”选项。
BGE 是双向编码器，embedding 没有自回归逐 token 生成阶段，生成模型的 KV 复用不是
当前首要优化点。Qwen 的可复用因果前缀也需运行时支持和收益测试，不能仅开启开关就计收益。
当前不引入 Redis；按结果缓存、增量计算、批次与 CPU 线程配置依次优化。

如果评测 TEI/vLLM，按以下步骤准备隔离实例：

1. 从官方 BGE-small-zh-v1.5 权重与 tokenizer 建立 FP32 参考，llama.cpp F16/Q8 单独标精度。
   同模型不代表相同数值；把后端差异与量化差异分别归档，维度仍为原生 512。
2. 初始推理线程预算 2、并发 1、少量 tokenizer workers，给博客留资源。
   CPUQuota=200% 是约 2 核算力预算，不等于固定绑在两个核上。
   vLLM 使用固定版本 CPU 构建与 pooling runner，先测小 BGE。
   Qwen 0.6B 的 FP32 权重按参数量粗算约 2.4GB，框架、激活和业务进程另计，不能只看权重能否装入。
3. 对齐 `/v1/embeddings`、认证、服务 model 名、输入预算、CLS/last、L2 与查询指令（只添加一次）。
   继续用独立 profile 与向量目录，模型文件换格式后不沿用旧 GGUF profile。
4. 明确拒绝超限输入，禁止静默截断。TEI 的在线 [参数说明](https://huggingface.co/docs/text-embeddings-inference/en/cli_arguments)
   默认启用自动截断，需按固定版本显式关闭；同时核对模型声明长度和批次 token 限额，防止配置不一致拒绝启动。
5. 通用 `rag.manage probe --expected-dim 512` 可以复用；`--llama-tokenize` 不能直接测试其他服务。
   为候选补充其 tokenizer 一致性预检，覆盖混合大小写、重音、特殊 token 和长度边界，不能跳过后宣称通过。
6. 在同一台目标机、同一语料/查询/qrels、相同分片策略下跑质量与资源对照。
   先一组模型服务结束再启另一组，记录缓存状态；检查建库、部分失败重试、降级与恢复。
   vLLM 的生成式 KV 默认值/预算不要套用到 pooling：按固定版本验证，legacy
   `VLLM_CPU_KVCACHE_SPACE=0` 也不能泛解为“所有 KV 已关闭”。
7. 使用 §8 表格留档，满足 §6 门槛再切换 Agent 配置，保留 llama.cpp 与原模型目录用于回退。
   目前没有 TEI/vLLM 发布模板；候选实例的启动、认证、限流、指标与资源约束均需单独验收。

## 8. 生产验证记录与交接

复制本表到本次发布的私有运行记录，所有空项初始均为“未执行”，不得用开发机结果代填。

| 验证项 | 要记录的证据 / 口径 | 目标机结果 |
|---|---|---|
| 环境 | 本机采集时间（含时区）、CPU flags、MemTotal/Available、swap、OS/kernel/systemd、磁盘 | 未执行 |
| 发布来源 | Agent 提交、引擎版本与镜像 digest、权重/tokenizer SHA、profile 签名、精度 | 未执行 |
| 输入契约 | 原生维度、pooling、范数、指令只加一次、NFC/大小写、完整输入边界与超限拒绝 | 未执行 |
| 认证和资源控制 | 无 key 拒绝；仅 loopback；CPU/内存/cgroup 限额实际生效；启动峰值 | 未执行 |
| 第一次建库 | 语料 revision、文档/片段/唯一输入数、新建/复用/缺失数、请求数、耗时 | 未执行 |
| 增量与恢复 | 不变语料零新增、改一篇/删一篇、失败重试、停模型词法降级与恢复 | 未执行 |
| 检索质量 | 固定短文桶、BGE 不限长桶、自有中文/混合/跨篇/无答案；nDCG/Recall/证据覆盖 | 未执行 |
| 冷启动 | 服务启动到可用的耗时、峰值内存；与预热后查询延迟分开 | 未执行 |
| 延迟 | embedding HTTP P50/P95、RAG 查询 P50/P95 分开；样本数、长度分布、并发和缓存状态 | 未执行 |
| 资源与业务影响 | RSS/PSS/VmHWM/VmSwap、cgroup MemoryCurrent/Peak、MemAvailable、si/so、业务 P95 | 未执行 |
| 发布决定 | 对照基线、达标/不达标、选用引擎与精度、回退版本及验证记录 | 未执行 |

每个引擎的预热后 embedding HTTP 延迟至少采样 100 次。命中/未命中查询向量缓存分开计；
检验模型推理时直接请求 embedding 服务，避免把 Agent LRU 命中当成引擎性能。
现有 `eval/model_retrieval_run.py` 的 `query_seconds` 包含向量读取与排序，不能改名为纯推理延迟；
24 查询公开桶用于质量回归，不能代替独立的 100 次延迟采样。文档向量首次新建与缓存复用
重跑也分别报告，不能拿 `api_calls=0` 的耗时充当首次建库速度。

模型运行时可用以下命令采样；不要公开完整进程命令行或环境：

```bash
systemctl show saudade-embedding@bge-q8 \
  --property=MainPID,MemoryCurrent,MemoryPeak,CPUUsageNSec,MemoryHigh,MemoryMax,MemorySwapMax,CPUQuotaPerSecUSec,Result,NRestarts
vmstat 1 10
```

用 MainPID 在 `/proc/<PID>/status` 读取 VmRSS/VmHWM/VmSwap，在 `smaps_rollup` 读取 Rss/Pss。
系统不支持 MemoryPeak 时记录该项“不可用”，补充进程 VmHWM，明确它是进程 RSS 峰值，
不能替代 cgroup 全组峰值；采样也不能保证捕获瞬时峰值。si/so 为换页活动，Swap 占用量只作上下文。
当前 §6 资源门槛是拟定验收值；本报告没有任何目标机“通过”结论。
