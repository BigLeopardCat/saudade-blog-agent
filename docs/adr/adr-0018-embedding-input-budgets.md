# ADR-0018：以固定 tokenizer 约束完整 embedding 输入

- **状态**：已采纳；原记录的提出状态已由实现承接，向量持久化沿用当前 PostgreSQL 方案。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/538b84c56820979d8dbb49dcbf7ff0938a396c93/docs/adr/adr-0018-embedding-input-budgets.md)。

## 背景与决策

字符上限不能证明 token 上限，截短查询还会把另一个问题当成原问题检索。
可选的本地 tokenizer profile 固定模型匹配的文件及校验和，使用 Hugging Face Tokenizers，
不为计数加载完整模型，不在请求中下载资产，不自行实现 tokenizer。

文档输入完整计入指令、标题、路径、正文及特殊 token。向量侧只细分超预算的原切片，
通过编码 offsets 回取原文，保留小节、字符范围和重叠；BM25 基线不随模型预算改变。
查询完整计数，超限时明确退回词法，不截断后发送或缓存。

profile 的语义配置、tokenizer 和窗口版本进入空间签名，内容键覆盖实际请求。
配置无效只影响可选向量路；没有匹配 profile 的 API 可以继续使用，但不声称有本地精确计数保证。

## 取舍与验证

tokenizer 和实际服务的 normalization、pooling 等设置须匹配，签名不能自行证明服务没有漂移。
细分增加向量数量和建库成本；切换空间需要重建，不能猜测旧向量身份。
当前存储见 [ADR-0017](adr-0017-versioned-rag-evidence.md)。

安装、模型限制和验收见[输入预算说明](../embedding-inputs.md)。
以完整输入覆盖、源范围、超限回退及相同输入不重复嵌入为回归依据；
质量比较还须控制模型和切片策略，不能把两者的收益混在一起。
