# RAG embedding 的完整输入预算

配置与行为说明为现状；末节注明日期的实测结果为验证记录。

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
示例预算 512 是本轮功能验证配置，不表示模型原生上下文只有 512，也不是最佳质量参数。
服务上下文、batch/ubatch 必须能容纳完整输入；单条预算不替代服务的总批量限制。
timeout 需要在目标机测量后决定，开发机慢请求不能用于放宽生产延迟门槛。

profile 文件在进程内按路径缓存，按不可变部署文件使用；修改同一路径的文件后重启
Agent。切换模型/服务配置时也应重启，避免正在执行的后台任务跨配置运行。

## 文档、查询与故障行为

BM25 继续使用原来的 Markdown/字符切片。向量侧以其为基础，仅对超预算片段细分；
模型相关配置不改变词法基线。每个输入按 **文档指令 + 标题 + 路径 + 正文** 的完整
字符串计数，保留小节 ID、原文字符范围和窗口重叠。字符窗口直接切原文，不通过
token 解码重建文本，因此 Unicode、代码和尾部可以完整保留。标题仍使用既有的
400 字符上限、路径仍使用既有的 600 字符上限，完整标题和路径仍保存在索引元数据中。

查询使用完整的规范化文本，加入查询指令后再检查预算。删除原来的 512 字符截断；
超 token 预算时不发送、不截断，当前请求退回 BM25。为限制查询缓存内存，超过
32,768 字符的查询同样不进入向量路。不能把截短后的另一问题当作原问题缓存。

前缀已经耗尽预算、tokenizer 缺失/校验失败或 profile 非法时，向量路降级原因为
`embedding_profile_error`，词法构建和查询继续可用。查询自身超限记录
`query_embed_failed`。后台失败不会发布部分切片覆盖的视图；真实服务错误仍按
既有机制记录 missing 并重试。

没有匹配 tokenizer 的 API 可以继续不设置 profile，使用供应商输入校验及既有故障
回退；这条路径不声称有本地精确 token 保证。不能为该 API 套用 Qwen tokenizer。
本选项只作用于 RAG；首页词图仍使用独立的 `rag/embed_space.py` 契约，不应把图谱
缓存误认为本文的文档向量缓存。

## 增量缓存与切换

空间签名除模型/端点/请求维度外，增加 profile 签名。签名包含模型权重/量化声明、
tokenizer SHA、Tokenizers 版本、special-token 规则、查询/文档前缀、预算、池化、
归一化及窗口版本。内容键仍覆盖实际发送文本，查询和文档使用同一空间。

未启用 profile 时保留原内容键并兼容原 schema 2 清单。第一次启用或改变 profile
需要构建对应的新空间；同一空间中只嵌入新增/变化文本，完全一致的更新零请求。
不同 API/本地模型同时保留索引时配置各自的 `RAG_VECTOR_DIR`；不自动将一个模型的
查询向量发给另一模型的文档索引。

## 验证记录

离线回归覆盖 special tokens、配置内截断关闭、Unicode 原文覆盖、长查询、增量复用、
空间变化与配置故障下的真实生产入口。它通过 MockTransport 走现有 OpenAI SDK，
没有新的推理协议实现。

启动临时 Qwen 服务后，可显式运行真实端点验证：

```bash
.venv/bin/python eval/verify_embedding_budget.py \
  --profile eval/data/qwen-local/profile.json \
  --base-url http://127.0.0.1:18081/v1 --model qwen3-embedding-0.6b-q8_0 \
  --output eval/report/qwen-budget.json
```

它使用临时向量目录，并在结束时删除，不访问博客语料。会实际调用所选服务，
API 密钥从 `EMBEDDING_API_KEY` 读取；未设置时只使用临时本地占位值。

2026-10-23 的 [真实服务记录](../eval/report/baseline_qwen3_token_budget.json)：
1,804 字符/1,190 tokens 的中文样本保留全部原文与尾部，产生 4 个输入，完整 token
数为 512/512/416/129，全部成功、missing=0，查询/文档均为 1024 维。
相同内容第二次更新 embedded=0、API 调用=0。这是功能验证，不能作为检索质量成绩
或另一台 4 核 4GB 服务器的性能验收。模型服务在验证后停止。
