# 参与开发

共享会话规则见 [AGENTS.md](AGENTS.md)。Python 3.12+；依赖和工具版本由 `pyproject.toml` 与 `uv.lock` 固定。

下面命令用于 CI 或独立开发环境；共享生产主机优先复用已有工具，运行定向检查。

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

PostgreSQL 测试只使用显式 `SAUDADE_CKPT_TEST_DSN`、`SAUDADE_SCHEDULING_TEST_DSN` 或 `SAUDADE_RAG_TEST_DSN` 指向的可丢弃实例；会创建和清理数据，不能使用部署 DSN。CI 提供临时服务。

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

采用 [Conventional Commits 1.0](https://www.conventionalcommits.org/zh-hans/v1.0.0/)。维护者默认使用中文描述，类型和 scope 使用英文；接受英文贡献，不要求逐行双语。

```text
type(scope): 描述
type(scope)!: 不兼容变更的描述
```

类型使用小写：

`feat` · `fix` · `refactor` · `perf` · `test` · `docs` · `style` · `build` · `ci` · `chore` · `revert`

- 标题不超过 60 个字符，说明最终变化；模块明确时使用小写 scope，如 `chat`、`auth`、`rag`、`ui`、`eval`。整个仓库的改动可省略 scope。
- `feat` 表示新增能力，`fix` 表示修复缺陷，`perf` 表示性能或成本改善，`refactor` 表示保持行为的结构调整。`style` 仅用于代码格式，界面变化按功能或修复归类。
- 正文按需说明原因、必要取舍和影响，与标题空一行；可以使用自然段或列表。保留真实贡献者信息和标准 Git trailers。
- 不兼容的接口、配置或数据变化使用 `!` 或 `BREAKING CHANGE:`，并在 PR 中说明迁移与发布顺序。

示例：`fix(chat): 恢复暂停任务后的执行状态`、`perf(rag): 减少重复向量化请求`、`docs: 更新本地开发说明`。

功能、修复、接口、依赖、数据库和 CI 改动通过 PR；维护者的小范围文档勘误可直接提交主线。分支使用清晰的功能前缀，编码助手默认使用 `codex/`。

PR 标题与主线提交使用同一格式，默认 Squash Merge。PR 内的临时提交不要求逐条整理；合并时由维护者确认最终标题和正文。本地钩子和 CI 共享 `.githooks/commit-msg`；PR 校验标题，主线校验新提交。

PR 描述围绕最终实现：

- 说明具体问题、改动后的行为及必要取舍；小改动一两句话即可。
- 记录实际验证命令、结果及未运行或跳过项。CI 通过、已合并和已部署分别表述。
- 涉及跨仓契约、数据迁移或发布时，列出配套 PR、固定版本、发布顺序和必要回滚步骤；不涉及的模板段落直接删除。
- 关联实际处理的 issue；保持描述与最终差异一致，省略开发过程、废弃方案和与评审无关的工作记录。

提交前检查差异、注释和敏感信息。安全问题按 [SECURITY.md](SECURITY.md) 报告，凭据及真实私有用户内容留在私有环境。

本地安装钩子：`bash scripts/dev/install-hooks.sh`。

## 文档

README 保留定位和起步；`docs/` 解释当前架构、操作与契约，配置默认值以代码和 `.env.example` 为准。重要取舍归入对应架构说明；跨版本仍有价值的理由、边界和替代方案保留在[架构决策记录](docs/adr/README.md)，后续变更明确取代关系，清理流水账时不一并删除。不为每次修复追加 ADR 或日报。用户可感知的变化汇总至 `CHANGELOG.md`，未来方向写入 `ROADMAP.md`。

使用教程可逐步扩展到 Wiki，但仓库中的快速开始、配置、兼容要求和发布验收必须可以随代码评审和版本固定。截图使用合成数据，不含真实账号、私信或密钥。

## 跨仓协作与交接

本仓主线为 `main`，博客主线为 `cn_sora_blog`。优先使用用户指定的基线；每个任务创建独立
工作树，开始和提交前核对仓库、分支与其他会话改动。交接使用已推送分支、完整 commit
和配套 PR，分别记录该版本的检查结果、尚未完成项与真实部署版本。

`.github/host-contract.json` 固定宿主测试源码；博客的 `frontend/widget.lock.json` 固定
本仓看板娘提交及两棵资源子树。两个锁不要求互相指向 HEAD，也不代表生产服务版本。
更新任一组件子树时核对博客 pin；只改 Python 或文档无需同步 widget pin。宿主契约改变
才更新 host lock，且源提交必须已推送、公开可取回。

本地严格跨仓验证可从已有博客仓准备独立候选工作树，在本仓执行：

```sh
BLOG_SOURCE=/path/to/Saudade-Blog
CONTRACT_ROOT="$(mktemp -d /tmp/saudade-host-contract.XXXXXX)"
HOST_REV="$(python3 -c 'import json; print(json.load(open(".github/host-contract.json"))["revision"])')"
git -C "$BLOG_SOURCE" fetch origin "$HOST_REV"
git -C "$BLOG_SOURCE" worktree add --detach "$CONTRACT_ROOT" "$HOST_REV"
test "$(git -C "$CONTRACT_ROOT" rev-parse HEAD)" = "$HOST_REV"
SAUDADE_PARENT_REPO="$CONTRACT_ROOT" SAUDADE_REQUIRE_PARENT=1 uv run --frozen pytest -q
```

完整套件仍交给 CI 或独立开发环境。候选接口联调可以显式选择开发工作树，但须记录其
实际提交和未提交差异，不能称为锁定版本回归。缺少宿主而跳过的用例也不能算通过。

三端协议、鉴权或确认机制变更，在配套 PR 中说明兼容、合并/发布顺序和回滚。
运行凭据、后台委托和操作员登录文件放私有目录；`.env.example` 只保留脱敏字段说明。
