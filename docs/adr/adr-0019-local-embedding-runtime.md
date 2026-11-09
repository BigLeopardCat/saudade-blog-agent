# ADR-0019：本地 embedding 复用 llama.cpp，并保留 API

- **状态**：本地运行时选择已采纳；其他引擎仍为候选，本文不声明当前部署状态。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/538b84c56820979d8dbb49dcbf7ff0938a396c93/docs/adr/adr-0019-local-embedding-runtime.md)。

## 背景与决策

小规格服务器需要控制模型内存、CPU 和部署依赖，无本地推理条件的安装也要保留托管 API。
BGE 与 Qwen 的 GGUF 路径复用固定版本 llama.cpp 及其 OpenAI 兼容接口，
不为每个模型新增推理网关。模型、runtime 和 tokenizer 固定来源、revision 与校验和。

官方 Qwen GGUF 和第三方 BGE 转换须区分来源。tokenizer normalization、pooling 和归一化
须与实际服务对齐，先验证完整输入，再验证质量与目标机资源。
API 是显式配置选项；本地失败不能自动改用收费 API。

## 后续承接与取舍

原记录的“本轮不引入向量数据库”是当时范围限制，当前持久化已采用 PostgreSQL/pgvector，
见 [ADR-0017](adr-0017-versioned-rag-evidence.md)；这不改变 embedding 服务的选择。
原低流量试点与机器读数不作为今天仍在运行或生产容量达标的证据。

转换产物可能与官方原权重有数值差异。TEI、ONNX/OpenVINO 等仅在目标机质量、
兼容性或资源测量显示需要时重评；不能按框架名称直接替换，也不能放宽输入上限掩盖漂移。
更换模型或服务配置先预检并构建匹配空间，不复用身份不明的向量。

固定资产和部署验收见[输入预算说明](../embedding-inputs.md)、
[部署](../deployment.md)；回归见 `tests/test_rag_assets.py` 和
`tests/test_rag_operations.py`。HTTP 探活不证明检索已生效。
