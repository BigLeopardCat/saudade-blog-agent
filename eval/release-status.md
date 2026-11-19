# 发布验收

各版的固定提交、配套博客、测试范围和已知限制随 [Releases](https://github.com/BigLeopardCat/saudade-blog-agent/releases) 保存。

- [v0.3.2](https://github.com/BigLeopardCat/saudade-blog-agent/releases/tag/v0.3.2) 收录 #492 的运行时简化。该版使用离线回归与安装包校验，未重跑真实模型。
- [v0.3.1](https://github.com/BigLeopardCat/saudade-blog-agent/releases/tag/v0.3.1) 的发行说明记录对应候选的 245 项文本功能契约、复算范围和已知质量问题。附件为 wheel、源码包、锁定依赖、manifest 和 SHA256SUMS。
- [v0.3.0（历史）](https://github.com/BigLeopardCat/saudade-blog-agent/releases/tag/v0.3.0) 的 `release-acceptance.json` 与 `release-provenance.json` 对应该版冻结证据，包含当时使用的语义判定、用量、缓存和端到端耗时。

当前发布规则以[发布验收标准](../docs/release-acceptance.md)及其机器配置为准。权限、
未确认写入、隔离、功能契约和证据完整性须通过验收。模型语义评审是可选观察。新版报告单列用量、调用、
延迟、首轮动作及测量缺口。既有冻结报告按各版本的口径解释。

HTTP 读取与确认准备、隔离执行回放、真实浏览器反馈及部署恢复分别报告。HTTP 准备通过不表示生产业务已写入，组件测试不能替代长期运行可用性的证明。已知不支持的视觉场景另列。

原始 HTTP、trace、评审材料和运行配置保留在私有验收目录，公开附件不含账号登录、确认凭据或私有内容。此前冻结报告保留原判定，不用当前规则回填通过。

历史执行链路证据见 [固定场景记录](e2e-closeout-20261113.md)；早期机器报告位于 [归档](report/archive/README.md)，不代表当前版本的完整验收。当前规则与复现入口见 [发布验收标准](../docs/release-acceptance.md) 和 [评测说明](../docs/eval-observability.md)。
