# 发布验收

各发行版的固定提交、配套博客版本、测试结果及已知限制见
[Releases](https://github.com/BigLeopardCat/saudade-blog-agent/releases)。
[v0.3.0](https://github.com/BigLeopardCat/saudade-blog-agent/releases/tag/v0.3.0)附件记录该版的冻结验收；
后续源码或实际部署是否达到相同结果须分别验证，不能沿用旧成绩。

- `release-acceptance.json`：确定性与语义检查、模型用量、缓存、端到端耗时及未达效率目标。
- `release-provenance.json`：源码与宿主版本、CI、部署和回滚核对，以及私有原始报告的指纹。
- `SHA256SUMS`：公开验收附件的校验值。

当前发布规则以[发布验收标准](../docs/release-acceptance.md)及其机器配置为准。权限、
未确认写入、隔离、严重事实错误和证据完整性须通过验收。新版报告单列用量、调用、
延迟、首轮动作及测量缺口。既有冻结报告按各版本的口径解释。

HTTP 读取与确认准备、隔离执行回放、真实浏览器反馈及部署恢复分别报告。HTTP 准备通过不表示生产业务已写入，组件测试不能替代长期运行可用性的证明。已知不支持的视觉场景另列。

原始 HTTP、trace、评审材料和运行配置保留在私有验收目录，公开附件不含账号登录、确认凭据或私有内容。此前冻结报告保留原判定，不用当前规则回填通过。

历史执行链路证据见 [固定场景记录](e2e-closeout-20261113.md)；早期机器报告位于 [归档](report/archive/README.md)，不代表当前版本的完整验收。当前规则与复现入口见 [发布验收标准](../docs/release-acceptance.md) 和 [评测说明](../docs/eval-observability.md)。
