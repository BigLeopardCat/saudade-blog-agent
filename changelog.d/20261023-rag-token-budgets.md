- 增加可选的固定 tokenizer 输入 profile，使用 Hugging Face Tokenizers 计算完整
  embedding 输入预算；向量侧细分长片段并保留原文范围，不改变 BM25 的切片和排名。
- 查询去掉 512 字符静默截断，完整查询加入指令后再验预算，超限明确降级词法。
  tokenizer/指令/预算/版本等进入空间签名，避免复用不匹配向量；无 profile 的 API
  和原缓存保持兼容，无效 profile 不阻断 BM25。
- 增加离线源范围、查询、故障及增量回归，留档真实 Qwen 服务上的完整覆盖与
  重复更新零 embedding 调用；不将功能验证当作检索质量或目标机器的性能成绩。
