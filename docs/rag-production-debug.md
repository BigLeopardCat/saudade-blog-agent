# RAG 本地 embedding 与生产机调试

类型：现状型（工具和操作步骤）；末节的资源与质量门槛是待目标机验证的验收条件。

本文适用于另一台 **4 核、4GB、Linux x86_64** 服务器，Python 3.12、systemd ≥249。
本轮只在开发机验证；生产机部署与业务灰度尚未执行。先部署到隔离发布目录，保留当前服务版本。
评测结果和来源见 [中文模型对照](../eval/t2-chinese-baseline.md)，输入预算见
[embedding-inputs.md](embedding-inputs.md)。

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
