# 参与开发

Python 3.12+；依赖和工具版本由 `pyproject.toml` 与 `uv.lock` 固定。

```sh
uv sync --locked
uv run --frozen pytest -q
node --test frontend/tests/*.test.mjs
```

## 测试边界

默认 pytest 禁止网络连接，忽略部署 `.env`，不调用模型、不连接本机或生产数据库。

- `tests/runtime/`：模型循环、状态、预算、取消与恢复。
- `tests/domains/`：业务工具、授权、准备与确认。
- `tests/providers/`：提供商协议；`tests/integration/`：显式外部依赖。
- 根目录保留共享夹具和跨模块契约。

跨仓测试使用 `.github/host-contract.json` 固定的公开博客源码；CI 自动检出。
本地可设置 `SAUDADE_PARENT_REPO` 指向该版本，`SAUDADE_REQUIRE_PARENT=1` 要求其存在。
缺少外部依赖必须显示 skipped，不能记为通过。

PostgreSQL 测试只使用显式 `SAUDADE_CKPT_TEST_DSN` 或 `SAUDADE_SCHEDULING_TEST_DSN` 指向的可丢弃实例；会创建和清理数据，不能使用部署 DSN。CI 提供临时服务。

聊天布局检查已纳入公开 CI。使用 Node 24.22.1，在 `frontend` 下执行 `npm ci`、`npx playwright install --with-deps chromium` 和 `npm run test:browser`；该入口使用 `frontend/tests/chat-ui.browser.mjs`。也支持 `PLAYWRIGHT_MODULE` 和 `CHROMIUM_EXECUTABLE` 指向已有安装。不访问模型和线上 API。

## 验证改动

优先验证用户行为及边界：错误身份拒绝、目标正确、未确认不写入、失败不编造结果、取消和恢复不重复执行。新增工具需要验证完整 ToolNode/middleware 接线，不能只断言函数返回类型。

```sh
uv run --frozen ruff check --select F821 --no-cache .
uv run --frozen python eval/type_gate.py
uv run --frozen python eval/lint_gate.py
bash scripts/dev/package_smoke.sh
bash scripts/dev/coverage.sh
```

真实模型、小批量与全量线上评测独立于普通 PR CI，见[评测说明](docs/eval-observability.md)。

## 提交与评审

分支使用 `codex/` 或清晰的功能前缀。提交格式：`type(scope): 中文说明`，标题不超过 60 字符。
类型：

`feat` · `fix` · `refactor` · `perf` · `test` · `docs` · `chore` · `ci`

例如 `fix(chat): 完整显示图片并限制预览高度`。使用 `bash scripts/dev/install-hooks.sh` 安装共享钩子；CI 同样验证。

PR 说明问题、最终行为、验证和限制。勿提交密钥、真实私信、确认令牌或本机配置；安全漏洞走 [SECURITY.md](SECURITY.md)。

## 文档

README 保留定位和起步；`docs/` 解释当前架构、操作与契约，配置默认值以代码和 `.env.example` 为准。重要取舍归入对应架构说明，不为每次修复追加 ADR 或日报。用户可感知的变化汇总至 `CHANGELOG.md`，未来方向写入 `ROADMAP.md`。

使用教程可逐步扩展到 Wiki，但仓库中的快速开始、配置、兼容要求和发布验收必须可以随代码评审和版本固定。截图使用合成数据，不含真实账号、私信或密钥。
