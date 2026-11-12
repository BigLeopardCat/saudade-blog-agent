# RAG embedding 的完整输入预算

向量功能须显式配置 `EMBEDDING_MODEL` 与 `EMBEDDING_API_KEY`，不借用聊天模型的
`QWEN_*` 凭据。缓存须带匹配的模型、端点和请求维度元数据；旧平铺缓存需要重建，
不猜测其空间。向量图谱产物同样须带模型和端点标识。

`EMBEDDING_PROFILE` 是可选的 JSON 路径，适用于本地或托管端点提供了匹配
`tokenizer.json` 的模型。未配置时保留现有 API 通道，不加载 tokenizer。
安装与验收使用独立开发环境；不要对正在提供服务的 venv 执行依赖同步。

```bash
uv sync --locked --extra embedding-tokenizer
```

使用 Hugging Face Tokenizers，按本地文件载入，不下载模型、不加载模型权重，
不调用 Transformers/PyTorch。tokenizer 的文件校验和必须匹配 profile。
JSON 内自带的 truncation/padding 在加载时关闭，计数包含配置启用的 special tokens。
不使用字符数或另一模型的 tokenizer 估算精确预算。

## 本地 BGE 生产安装

先完成[核心部署](deployment.md)，再启用向量检索。仓库自带的 systemd 模板面向 Linux x86-64，
固定 llama.cpp、GGUF 与 tokenizer 的来源和 SHA-256，见[资产清单](../deploy/embedding/artifacts.json)。
其他架构需要验证匹配的模型服务；不要直接运行不兼容的二进制。

以下是新的本地服务安装，不替换正在使用的模型目录或认证文件。
以已部署 Agent 的解释器准备依赖，无需 PyTorch：

```sh
AGENT_RELEASE="$(readlink -e /srv/saudade-agent/current)"
sudo "$AGENT_RELEASE/.venv/bin/python" "$AGENT_RELEASE/scripts/prepare_embedding_assets.py" \
  --model bge-q8 --directory /opt/saudade-embedding --download
sudo "$AGENT_RELEASE/.venv/bin/python" "$AGENT_RELEASE/scripts/prepare_embedding_assets.py" \
  --model bge-q8 --directory /opt/saudade-embedding --verify-only
ldd /opt/saudade-embedding/runtime/llama-b11829/llama-server
/opt/saudade-embedding/runtime/llama-b11829/llama-server --version
```

`--download` 显式访问 GitHub/Hugging Face；下载与解包均校验固定资产，不在 Agent 请求中临时下载。
`ldd` 不应有 `not found`，`--version` 应成功；不满足则先处理操作系统/CPU 依赖。
模型、tokenizer 和 profile 须对服务与 Agent 可读、不可由运行账号改写。

由管理员用 `openssl rand -hex 32` 生成独立认证值并私下保管。首次创建认证文件后，
通过 `sudoedit` 填入该值；已有认证文件直接沿用，不覆盖：

```sh
if ! sudo test -e /etc/saudade/embedding-api-key; then
  sudo install -o root -g root -m 0600 /dev/null /etc/saudade/embedding-api-key
fi
sudoedit /etc/saudade/embedding-api-key
sudo test -s /etc/saudade/embedding-api-key
```

systemd 用 `LoadCredential` 交给动态服务账号，不使用无认证公网服务。安装模板，检查后启动：

```sh
sudo install -m 0644 "$AGENT_RELEASE/deploy/embedding/bge-q8.env.example" \
  /etc/saudade/embedding-bge-q8.env
sudo install -m 0644 "$AGENT_RELEASE/deploy/embedding/saudade-embedding@.service" /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/saudade-embedding@bge-q8.service
sudo systemctl daemon-reload
sudo systemctl enable --now saudade-embedding@bge-q8
systemctl status saudade-embedding@bge-q8
```

认证文件必须先存在且非空。端口为 loopback `18081`；模板限制一线程、一个并行请求，
CPU/内存上限见单元，在目标机测量后调整，不能按网页在线人数加线程。
使用相同认证值对 `/v1/embeddings` 发送短文本，确认返回有限数值的 512 维向量，
再通过 `sudoedit /etc/saudade/agent-runtime.env` 加入：

```dotenv
RAG_HYBRID_ENABLED=true
EMBEDDING_BASE_URL=http://127.0.0.1:18081/v1
EMBEDDING_MODEL=bge-small-zh-v1.5-q8_0
EMBEDDING_API_KEY=填写上述独立认证值
EMBEDDING_PROFILE=/opt/saudade-embedding/bge-q8.json
EMBEDDING_DIM=512
EMBEDDING_BATCH_SIZE=1
```

Agent 配置中保存相同认证值，不让 Agent 读取 root 的原始 credential 文件。
[可选 drop-in](../deploy/embedding/agent-local-bge.conf) 只声明服务顺序，可安装为
`saudade-agent.service.d/20-local-embedding.conf`；不再加载第二份 Agent 环境文件。
重新执行固定版本发布，脚本自动安装 tokenizer 可选依赖并在激活前校验 profile；
启用 scheduler 时同时传 `DEPLOY_SCHEDULER=1`，避免后台进程沿用旧配置。

真实检索后检查 `/health` 的 `rag_state=active`、`vector_missing=0` 和检索 trace。
空语料、只启动服务或只调用 embedding API 均不能证明检索已生效。
切换模型使用独立索引目录并重建；不删除确认、会话或业务数据。

## Qwen Q8 配置示例

本项目的 [Qwen 示例](../eval/profiles/qwen3_q8_input_512.json) 对应
[官方 GGUF](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF)，
tokenizer 来自原始模型的固定 revision
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`。
在单独开发环境准备本地文件：

```bash
mkdir -p eval/data/qwen-local
cp eval/profiles/qwen3_q8_input_512.json eval/data/qwen-local/profile.json
curl --fail --location \
  https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/resolve/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3/tokenizer.json \
  --output eval/data/qwen-local/qwen3-tokenizer.json
```

下载是显式操作，产物目录已被 Git 忽略。loader 会在第一次使用时核对 SHA-256。
`tokenizer_file` 相对 profile 文件所在目录解析，也支持绝对路径。

```dotenv
RAG_HYBRID_ENABLED=true
EMBEDDING_MODEL=qwen3-embedding-0.6b-q8_0
EMBEDDING_BASE_URL=http://127.0.0.1:18081/v1
EMBEDDING_API_KEY=local-only
EMBEDDING_PROFILE=eval/data/qwen-local/profile.json
EMBEDDING_DIM=0
EMBEDDING_BATCH_SIZE=1
```

`local-only` 只用于未设置认证的本地临时服务；启用服务端认证时配置其实际密钥。
模型服务器启动方式见 [首轮服务验证](../eval/scifact-local-baseline.md)。
pooling/normalization 是对服务实际行为的声明，profile 不会代替服务配置这些参数。
示例预算 512 用于功能验证，不表示模型原生上下文只有 512，也不是最佳质量参数。
服务上下文、batch/ubatch 必须能容纳完整输入；单条预算不替代服务的总批量限制。
timeout 需要在目标机测量后决定，开发机慢请求不能用于放宽生产延迟门槛。

profile 文件在进程内按路径缓存，按不可变部署文件使用；修改同一路径的文件后重启
Agent。切换模型/服务配置时也应重启，避免正在执行的后台任务跨配置运行。

## 文档、查询与故障行为

BM25 继续使用现有 Markdown/字符切片。向量侧以其为基础，仅对超预算片段细分；
模型相关配置不改变词法基线。每个输入按 **文档指令 + 标题 + 路径 + 正文** 的完整
字符串计数，保留小节 ID、原文字符范围和窗口重叠。超预算输入通过 Hugging Face Tokenizers 的 Encoding.truncate 与 overflowing 生成
token 窗口，只修改本次独立编码结果；共享 Tokenizer 不启用全局 truncation。
应用用窗口 offsets 回取原文，不通过 token 解码重建文本，保留 Unicode、代码和尾部，
完整输入再次验证预算；无法保留来源或预算时明确降级。标题仍使用既有的
400 字符上限、路径仍使用既有的 600 字符上限，完整标题和路径仍保存在索引元数据中。

查询使用完整的规范化文本，加入查询指令后再检查预算；不做 512 字符截断。
超 token 预算时不发送、不截断，当前请求退回 BM25。为限制查询缓存内存，超过
32,768 字符的查询同样不进入向量路。不能把截短后的另一问题当作原问题缓存。

前缀已经耗尽预算、tokenizer 缺失/校验失败或 profile 非法时，向量路降级原因为
`embedding_profile_error`，词法构建和查询继续可用。查询自身超限记录
`query_embed_failed`。后台失败不会发布部分切片覆盖的视图；真实服务错误仍按
既有机制记录 missing 并重试。

没有匹配 tokenizer 的 API 可以继续不设置 profile，使用供应商输入校验及既有故障
回退；这条路径不声称有本地精确 token 保证。不能为该 API 套用 Qwen tokenizer。
本选项只作用于 RAG；首页词图仍使用独立的 `rag/vector_index.py` 契约，不应把图谱
缓存误认为本文的文档向量缓存。

## 增量缓存与切换

profile 的 `chunking_strategy` 可选值为 `token-window-v2`（默认）和
`sentence-paragraph-v1`。后者在既有结构块内复用 LangChain 递归切分器，按匹配 tokenizer
计长，优先段落、换行与中英文句子边界；完整输入仍须符合预算。无法可靠恢复重复文本坐标、
续词重新编码超限或无法覆盖全文时，使用已校验的 token 窗口兜底，不静默丢弃原文。
这不是基于另一个模型计算相似度的语义切分。固定公开全库和读文预算的
[对照结果](../eval/chunk-context-comparison.md)不支持自动切换默认子片策略；父段收益与读取成本另行记录。

省略该字段保留原有空间签名；显式切换句段策略会生成独立空间，须重新准备索引。
评测入口可在验证权重声明、tokenizer、前缀、归一化等编码配置完全一致后，按完整输入复用
独立评测 collection 的向量；这不代表生产自动完成跨策略迁移。

正文读取范围与 embedding 输入预算独立。`RAG_CONTEXT_MODE=window` 保留原始定位；
`parent` 扩展到同节父段，`neighbors` 进一步纳入相邻块，均保留原命中坐标。
`RAG_CONTEXT_TOKENS=1024` 是扩展的 tokenizer 计量目标；原始词法锚点更大时不缩掉锚点，
实际读取仍受单次字符上限与 Agent 结果预算控制。此计量不是聊天模型的实际计费 token。
扩展只使用已核实公开版本的索引快照，不新增正文 HTTP 请求；版本变化或 profile 不可用时
保留原定位，并在工具内容中明确 `context.applied=false`。

空间签名除模型/端点/请求维度外，增加 profile 签名。签名包含模型权重/量化声明、
tokenizer SHA、Tokenizers 版本、special-token 规则、查询/文档前缀、预算、池化、
归一化及窗口版本。分块库版本、分隔符、字符预算和重叠另纳入空间签名。
内容键覆盖实际发送文本，查询和文档使用同一空间。

持久化使用 PostgreSQL＋pgvector，配置 `RAG_VECTOR_DSN` 和 `RAG_VECTOR_COLLECTION`。
同一空间只嵌入新增/变化文本；完全一致的更新零请求。不同模型对照使用独立 collection，
不混用查询向量和文档空间；切换 collection 不改变当前生产索引。数据库初始化与运行账号
授权见 [部署](deployment.md#向量存储)。旧 f32/manifest 文件不再加载，不提供隐式导入。

## 验证

输入预算、缓存空间和故障回退分别使用离线回归验证。真实模型验证使用固定语料、profile、tokenizer 和服务版本，并记录召回质量、截断率、耗时及内存；开发配置不能直接当作生产最优参数。

验证工具：`eval/verify_embedding_budget.py`，离线样本见 `eval/profiles/`。使用前阅读 CLI `--help`；不得把一次延迟或召回样本当作完整验收。
