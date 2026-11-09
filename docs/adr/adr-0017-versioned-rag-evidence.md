# ADR-0017：版本化语料、完整索引与正文证据约束

- **状态**：语料与证据约束已采纳；原文件向量代和 manifest 已由 PostgreSQL/pgvector 替代。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/538b84c56820979d8dbb49dcbf7ff0938a396c93/docs/adr/adr-0017-versioned-rag-evidence.md)。

## 背景与保留的决策

分页缺少版本及完整性信号时，故障可能伪装成末页，构建者也可能把不同版本的正文拼接。
公开语料接口在只读 repeatable-read 事务中取得页面和摘要，后续分页绑定同一 revision。
Agent 验证内容指纹、总数、游标和完整覆盖后才发布新索引，不静默退回旧列表接口。

词法与向量检索保留文章标题、小节路径、原文范围和来源。检索命中只帮助选择读取位置；
候选摘要或索引摘录不能冒充实时正文证据。正文按内容版本和范围读取，
每次仍须由宿主复核公开性，续读保留实际游标和解析版本。

## 后续承接

原记录的内容寻址文件代与原子 manifest 用于防止向量、行映射分开覆盖。
当前向量持久化使用 PostgreSQL 独立 rag schema 及 pgvector，
通过完整 membership 和短事务发布一致视图；旧文件缓存不再参与读取。
空间身份、真实输入内容键和禁止混用不同语料版本的原则继续有效。

宿主内容 hash 不提供单调版本 fencing，写方互斥不能证明提交的一定是最新来源；
查询核对指纹，拒绝混用并明确降级。部署先提供兼容宿主接口，再切换 Agent。

当前细节见 [RAG 设计](../rag-design.md)；
回归见 `tests/test_rag_contracts.py`、`tests/test_rag_evidence.py` 和
`tests/integration/test_rag_postgres.py`。索引能加载不等于检索质量通过。
