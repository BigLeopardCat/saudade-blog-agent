# ADR-0004：公开契约源码固定版本，PR 检查无需私人凭据

- **状态**：已采纳，2026-10-23 更新
- **影响范围**：`.github/workflows/eval.yml`、`.github/host-contract.json`、跨仓断言
- **判据**：`tests/test_ci_suite_list.py`、`tests/test_widget_pin_drift.py`

## 决策

博客仓库已公开。Agent CI 从 `.github/host-contract.json` 读取公开仓库及完整提交 SHA，
使用 GitHub Actions 的只读权限检出必要源码，并验证真实提交及稀疏路径。
不再要求 `PARENT_REPO_TOKEN`，不因 fork、Dependabot 或维护者身份跳过跨仓断言。

PR、主线 push 和手动运行使用同一条完整 pytest 入口；源码缺失或契约不兼容直接失败。
不使用 `pull_request_target` 执行外部贡献者代码，也不向 PR 测试注入发布凭据。
契约版本更新必须随提交审查，防止另一个仓库主线变化影响旧 PR 的复验结果。
组件 pin 漂移提示读取公开资源，只作提示，不代替固定版本契约检查。

## 测试层次

常规 CI 在临时 runner 上执行静态检查、离线模型/HTTP 夹具、固定语料评测和构建。
Python 测试通过标准 pytest-socket 默认禁用网络 socket，仅允许异步框架需要的 Unix socket；
HTTP 使用 MockTransport 或应用内测试客户端；传输契约测试只放行回环地址并启动自己的临时服务。
测试固定回环代理例外，不访问维护者原有服务。
博客数据库集成测试使用 runner 内的临时 PostgreSQL 服务，数据与凭据不来自生产。
需要真实模型、真实语料或真实站点的验收使用已有独立入口，不能成为外部 PR 的必要条件。
部署仅在可信分支通过检查后使用发布凭据；不为外部 PR 发布服务。

## 契约升级

跨项目变更先发布向后兼容的博客接口，再更新契约锁及 Agent 调用方。
破坏性升级须明确两侧版本与部署顺序。固定源码不能替代真实模型行为及生产链路验收，
但公开 PR 的可合并性不依赖私人服务器在线或维护者提供密钥。
