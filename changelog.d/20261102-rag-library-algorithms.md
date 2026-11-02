- RAG 通用算法改用 scikit-learn 的公开文本 analyzer、LangChain 官方字符切片与 RRF、
  Hugging Face Tokenizers 的 token 窗口和 SciPy 余弦距离；保留公开语料、版本、来源范围与增量持久化适配。
  候选显示排名，混合结果不再暴露未由官方接口提供的分数。分块策略与库版本纳入向量空间，
  升级后旧空间拒绝混用，需在隔离目录准备一次新索引后切换；故障继续降级词法。
