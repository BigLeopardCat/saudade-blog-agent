# ADR-0004：公开契约源码固定版本，PR 检查无需私人凭据

- **状态**：已采纳。
- **核对日期**：2026-11-09。
- **历史来源**：[删除前记录](https://github.com/BigLeopardCat/saudade-blog-agent/blob/538b84c56820979d8dbb49dcbf7ff0938a396c93/docs/adr/adr-0004-cross-language-guard-in-ci.md)。

## 背景与决策

跨仓测试如果读取宿主主线、私人 checkout 或生产服务器，旧 PR 就无法稳定复验，外部贡献者也无法完成检查。
CI 从 `.github/host-contract.json` 读取公开宿主仓库和完整提交 SHA，使用只读权限检出必要源码，
核对真实版本和稀疏路径；契约缺失或不兼容应失败。

PR、主线 push 和手动检查使用同一离线测试入口。不因 fork、机器人或维护者身份跳过契约断言，
不使用 `pull_request_target` 执行不可信代码，不向外部 PR 注入发布凭据。
默认禁止网络的测试通过应用内客户端、MockTransport 或自己启动的回环夹具验证协议；
PostgreSQL 集成使用 CI 临时实例。

## 取舍与验证

固定源码保证协议复验，不代替真实模型与实际链路验收。需要真实账号、模型和语料的验收走独立入口。
跨仓升级先发布兼容宿主，再更新契约锁和 Agent；破坏性变化须明确两侧版本及发布顺序。
组件 pin 漂移提示不能代替固定版本测试，文档改动也不要求更新看板娘资源 pin。

当前流程见[评测与观测](../eval-observability.md)，实现见 `.github/workflows/eval.yml`，
回归见 `tests/test_ci_suite_list.py` 和 `tests/test_widget_pin_drift.py`。
