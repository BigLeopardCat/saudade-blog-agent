# 参与开发

这是博客看板娘「泠月喵」的 Agent 服务，使用 FastAPI、LangChain 与 LangGraph。
博客宿主提供账号身份、对话存储和浏览器交互；开发与默认离线测试不需要部署博客、
启动数据库、配置模型密钥或复制维护者的 `.env`。

## 1. 起步

```bash
git clone https://github.com/BigLeopardCat/saudade-blog-agent
cd saudade-blog-agent
uv sync --locked
uv run --frozen pytest -q
```

pytest 递归收集 `tests/`，新增测试无需修改 CI 或维护套件名单。新测试按职责放置：

- `tests/runtime/`：框架执行、会话、预算、记忆、流式与恢复。
- `tests/domains/`：领域工具、权限、确认准备与业务契约。
- `tests/providers/`：模型适配和服务商协议。
- `tests/integration/`：需要显式外部依赖的集成测试。
- 根目录保留共享夹具与尚待逐项整理的旧测试。

`tests/conftest.py` 在导入应用前加载明确的测试配置，清除应用部署变量，忽略工作目录
中的 `.env`；系统路径和工具环境仍保留。新增场景使用 `monkeypatch` 或专属夹具控制输入。
旧测试的全局状态隔离层继续保留，避免用例之间相互污染。
默认禁止网络 socket，HTTP 通过模拟传输验证。默认测试不会调用真实模型或连接本机数据库。

跨语言检查使用公开博客源码契约，不读取相邻的部署目录。CI 自动检出
`.github/host-contract.json` 固定的公开提交，并将缺失契约当作失败；本地没有该检出时，
依赖它的测试会显式报告 skipped。需要完整运行时，检出该提交并设置：

```bash
SAUDADE_PARENT_REPO=/path/to/pinned-blog-checkout SAUDADE_REQUIRE_PARENT=1 uv run --frozen pytest -q
```

PostgreSQL 集成测试需要显式设置 `SAUDADE_CKPT_TEST_DSN`，使用可丢弃的专用数据库。
它们会建表、删表或清理数据；不设置该变量就跳过，不尝试本机默认连接，也不使用部署 DSN。
真实模型与线上评测独立运行，不加入普通 PR 测试。完整口径见
[评测与可观测性](docs/eval-observability.md)。

## 2. 提交之前

```bash
uv run --frozen pytest -q
uv run --frozen ruff check --select F821 --no-cache .
uv run --frozen python eval/type_gate.py
uv run --frozen python eval/lint_gate.py
```

`.github/workflows/eval.yml` 对公开 PR、fork、机器人和主线使用相同检查，无需生产密钥。
依赖由 `uv.lock` 固定；测试使用四进程，JUnit 记录结果与耗时。CI 不发布生产服务。
可选覆盖率报告使用 `bash scripts/dev/coverage.sh`，目前没有覆盖率百分比门槛。

测试优先保护用户结果、权限、确认、幂等、取消、恢复、预算及协议。
源码检查应有明确的兼容或安全契约，不限制普通目录结构、类测试或实现细节。
删除重复和退役检查时说明原有风险由什么覆盖，不以减少测试数量为目标。

修改代码先确认职责归属，体量约束见 `eval/module_baseline.json` 与复杂度台账。
新需求优先放入对应模块；超预算模块按职责拆分，调整台账需要说明原因。

## 3. 提交约定

首行 `<type>[(scope)][!]: <一句话>`，**≤60 字符**。
`scope` 和 `!` 可选，例如 `fix(runtime): 保留并发轮次的完整历史`。

当前维护批次的 PR 标题、提交摘要及说明默认用中文，代码标识符和 type/scope 使用英文。
国际协作者可以使用英文描述，格式与验证要求相同；中英文文档分别维护。
`type` 用这八个（取自本仓七百余笔提交的实际分布，别自创）：

`fix` · `feat` · `docs` · `test` · `chore` · `refactor` · `ci` · `perf`

- `scope` 写受影响的模块，如 `runtime`、`tools`、`eval`；使用小写字母、数字或 `._/-`，
  不维护另一张范围白名单。标题描述行为或结果，避免「修复问题」「继续重构」等空话。
- 对公开接口、配置或数据格式有不兼容改动时加 `!`，正文说明影响和迁移方法。
- 简单改动可以只有标题；复杂或行为改动在空一行后说明原因、验证范围和已知限制，
  短段落或列表都可以。无需填固定表格，也不要在正文复述 diff。
- **不加 `Co-Authored-By` 之类的署名尾注**（本仓约定，与工具默认行为冲突时以本仓为准）；
- **版本号同步的改动不写进提交信息**；
- 一条提交只做一件事。同文件的不同批次改动要**拆成多个提交**，但注意
  `git diff -U0` 拆不了——按**语义**拆，不按行号拆；
- 提交信息里出现的内部编号（如「洞⑪」「批 G」）要**展开成人话**：
  读提交的人没有你脑子里那张表。

行为改动的正文写**为什么**，以及**判据在哪儿和实际验证结果**。改动对"agent 会怎么做"有影响的，
**不要改 [CHANGELOG.md](CHANGELOG.md)，在 [`changelog.d/`](changelog.d/README.md) 加一个碎片**：

```bash
.venv/bin/python scripts/dev/changelog.py --new <ascii-slug>   # 开一个今天的碎片
```

**为什么加碎片而不是直接写 CHANGELOG**：那个文件只有**一个**插入点（当日那一节的组首）
⇒ 任何两个同时开着的 PR 都必然在那里冲突（实测 PR #21 / #22 / #23 连着三次撞的都是同一处）。
碎片是"一个新文件"，两个 PR 各加一个，三路合并永远不冲突。汇总由人在合并时（或发布前）跑：

```bash
.venv/bin/python scripts/dev/changelog.py     # 折进 CHANGELOG.md 并删掉碎片
```

记什么/不记什么，[CHANGELOG.md](CHANGELOG.md) 的头注写得比这里细。

上面这几条里**机器判得了的那几条**（首行长度 / type 白名单 / 空行 / 署名尾注）由
`.githooks/commit-msg` 拦：本机 `bash scripts/dev/install-hooks.sh` 装一次，CI 的
`.github/workflows/eval.yml` 对本次 push 的每条提交跑**同一个文件**——规则本体就是这一节，
脚本只是它的机器化，别在那边另抄一份。**只装本机钩子不算数**：`core.hooksPath` 是 git 的
本地配置、不会被 clone 带走，新克隆与别人机器上一次都不会跑。

## 4. 判据：本仓最特别的一条规矩

**每个行为改动都要带一条判据，而且这条判据必须能抓住"它其实没生效"。**

这不是风格偏好。本仓的失效模式里最贵的一类是**「看着在跑、其实没跑」**：

- 判据恒真（"模块能 import"、"函数返回 list"、"目录建出来了"）——它们永远绿，
  于是给人"这块有测试"的错觉；
- 守卫静默跳过（找不到依赖就当通过）；
- 开关拨了但一次都没生效（配置读到了、代码路径没走到）；
- 改了一处忘另一处（两条通道到达同一件工具、三处字面要同步）。

所以写判据时请自问三句：

1. **把实现改坏，它会红吗？** 不红就是空断言。这不是修辞——很多套件里真的带着
   "红基线"（把被测函数打桩成恒真，断言此时必须失败），就是为了证明这条判据在测东西；
2. **它测的是行为还是形状？** 优先测用户可见的那个面。断言"内部常量等于 60"没有价值，
   断言"开关关掉时嵌入调用数 == 0"有价值；
3. **同名同义的用例会不会互相打架？** 两条同义用例期望相反 ⇒ 判据变成硬币，
   这时该撤掉机制断言、留用户可见面。

另外两条现成的纪律：

- **别把测试的红基线钉在 `git show HEAD`**——历史一变它就失去意义；
- **修复一个洞时，先问"契约改字面之后，这一格还装得下原来的病吗"**：改契约文本
  经常只是把病灶挪了一格。

## 5. 文档

- 任何**取舍**（不是"怎么做"，是"为什么这么做、当时还摆着什么选项"）写进
  [docs/adr/](docs/adr/)——体例、状态取值与索引见 [docs/adr/README.md](docs/adr/README.md)。
  **"决定不做某事"比"决定做某事"更该写**：代码里看不见一个没被写出来的功能，
  于是它每隔一阵子会被人重新提议一次。
- 长期但不做的方向：写进 [ROADMAP.md](ROADMAP.md)，并留下**可观测的触发条件**
  （"图片 ≥1000 张"是可观测的，"以后有需要再做"不是）。
- 事故级取证写 [docs/问题记录.md](docs/问题记录.md)：现场 trace id、复现路径、根因。
- 改文档时注意：`docs/*.md`、`README.md` 会被套件扫描，别在里面手抄一份会漂的取值。

## 6. 去哪找活

| 标签 | 含义 | 想要什么 |
|---|---|---|
| `good first issue` | 边界清楚、不需要跑起整套系统 | 判据、文档、确定性决策 |
| `help wanted` | 明确需要外部视角 | 见 issue 正文 |
| `future` | **方向认可、条件未到**——触发条件写在 issue 里，**issue 保持开着** | 条件满足了就来做 |
| `icebox` | 更远，连触发条件都还没有 | 讨论 |
| `needs-triage` | 还没分类 | — |
| `wontfix` | 明确不做 | 别做 |

`future` / `icebox` / `wontfix` 的区别值得记一下：**不是"没在写"就都一样**——
`future` 是迟早要做、等一个信号；`icebox` 是方向认可但没有信号可等；
`wontfix` 是想清楚了不做（关闭时用 `not planned`）。三者在 GitHub 列表上长得一样，
所以只能靠标签和 ADR 的状态字段区分。

## 7. 安全

不要在 issue / PR / 提交信息里贴 API key、JWT、真实用户 id、生产路径或任何凭据。
本仓是 **public** 仓库。安全问题请走私密渠道，见 [SECURITY.md](SECURITY.md)。

本仓有一个 trust boundary 值得先读一遍：[docs/agent-architecture.md](docs/agent-architecture.md)
（架构与各条防线的位置）。更细的那份《信任边界与加固》在**宿主后端那个仓**里、
不在本仓，因此外部贡献者读不到——这不是疏漏，那台机器上的具体坐标本来就不该公开。

## 8. 行为准则

见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
