# ADR-0004：跨语言守卫在 CI 里真跑——父仓以只读凭据稀疏 checkout

- **状态**：已采纳（20260928）
- **背景文档**：`.github/workflows/eval.yml`（接线落点）、`tests/_parent_repo.py`（三态守卫）、
  `scripts/nightly_regression.sh`（夜间那一半）
- **影响范围**：`.github/workflows/eval.yml`、`.gitignore`（`_parent/`）、`tests/_parent_repo.py`
  （注释/说法）、`tests/test_ci_suite_list.py` ⑥（接线判据）
- **判据**：`tests/test_ci_suite_list.py` ⑥（接线存在 + 凭据不当字面量 + 不许展开进日志 +
  **守卫读的每个父仓路径都落在稀疏锥里** + 秘密名与本文同源）
- **凭据**：仓库 secret `PARENT_REPO_TOKEN`（**人**创建并轮换，本仓任何文件里都不出现它的值）

## 背景

agent 侧有七处守卫断言「Rust 那半真有这个臂 / 读这个键 / 渲染这个字面」（`__ERROR__` 帧
形状、`render_exec_row` 读回执顶层的 `action`、`talks.rs` 的审核失败分支、`agent_reply_of`
读 `success`……）。它们此前各写各的 `if (父仓/chat.rs).exists(): 断言 else: print("⏭ 跳过")`。

20260928 统一成 `tests/_parent_repo.py` 的三态（找得到 → 断言；找不到 + `SAUDADE_REQUIRE_PARENT=1`
→ 红；找不到 + 没设开关 → 响亮跳过），**夜间门禁立刻设上了那个开关**。但 CI 没有：

- agent 仓是 **public**、父仓 `BigLeopardCat/Saudade-Blog` 是 **private**，而 CI 只 checkout
  agent 仓 ⇒ **那七处在 CI 里恒跳过**；
- 而这恰恰是它们最该跑的地方：Rust 那个仓库的改动**不经 agent 仓的 CI**，跨语言漂移要么在
  夜间（本机 04:00）被发现，要么**根本没有东西会发现**；
- 跳过还是**静默**的：本机绿、CI 也绿，两侧谁都没真比过——同族失效在 20260928 那批里已经
  抓到三次（`.env` 取值、父仓读不到、机器相关分支），这是第四次，只是形态不同。

## 决策

CI 里把父仓**拉下来真判**，凭据用**一枚只读 PAT**：

```yaml
env:                                      # job 级：step 的 `if:` 里读不了 secrets
  PARENT_REPO_TOKEN: ${{ secrets.PARENT_REPO_TOKEN }}
steps:
  - 凭据自检：空 ⇒ `::error::` + exit 1   # 配置问题红在**一处**，不要让七处守卫各自红一遍
  - actions/checkout@v4（repository: BigLeopardCat/Saudade-Blog, token: secrets.PARENT_REPO_TOKEN,
                        path: _parent, sparse-checkout: src/routes, fetch-depth: 1,
                        persist-credentials: false）
  - 落位自检：test -f _parent/src/routes/chat.rs
  - uv run python tests/run_all.py        # env: SAUDADE_PARENT_REPO=<ws>/_parent
                                          #      SAUDADE_REQUIRE_PARENT=1
```

四条纪律，各自对应一种失效：

| 纪律 | 不这么做会怎样 |
|---|---|
| 凭据缺失 ⇒ **红**，不是跳过 | 「没配就静默跳过」正是这道守卫此前失效的方式本身 |
| `SAUDADE_REQUIRE_PARENT=1` **在 CI 也设**（与夜间同一条） | 拉下来了但读不到（锥配错/路径改名）会退回响亮跳过，而响亮跳过在 CI 里没人读 |
| 稀疏锥 + 浅克隆 | 只为把面与时间收小；**锥够不够由判据管**（见下） |
| 凭据只进 checkout 的 `token:`，绝不展开进日志 | 泄漏面：CI 日志是公开仓库的一部分（agent 仓 public） |

**判据不是"名单里有这几行"，而是扫源码得出的**：`tests/test_ci_suite_list.py` ⑥ 从
`tests/*.py` 里扫出所有 `_parent_repo.read("…")` 的路径，逐个判是否落在 `sparse-checkout`
声明的锥里（cone 模式：锥 + 各级祖先 + 仓根）。新加一条读别处源码的守卫而忘了改锥，
会红在**接线**上，而不是让那条守卫因为「文件不在」而红——后者的红看着像「Rust 那边没改」，
有人会顺手把守卫删掉。

## 否决的备选

| 备选 | 为什么没选 |
|---|---|
| **保持"只在本机/夜间"** | 零新增凭据，但跨语言漂移只能等夜里 04:00，且夜间红是**异步**的（要有人看 `~/agent_regression.log`）。漂移的代价是线上某一帧静默消失，等一夜不值 |
| **父仓转公开** | 最省事，但对一个含用户数据与业务源码的仓来说，这不该由 CI 的便利性来推动 |
| **把期望值 vendored 进 agent 仓**（抄一份 Rust 侧的期望文本 + 定期同步） | 直接制造第二份事实源。这套守卫存在的理由就是「两边各写一份、改一处忘另一处」；把期望抄进来等于把这个形状制度化 |
| **SSH deploy key**（父仓加只读部署密钥 + agent 仓存私钥） | 可行且同样只读。没选是因为它要两处配置（密钥 + known_hosts）且轮换更繁琐；PAT 是一处、可设过期、可随时吊销 |
| **父仓 CI 反向触发 agent 仓** | 只能覆盖「Rust 改了」这一半，agent 侧改了也要判；两边都要，复杂度翻倍 |

已知代价，写明白：**细粒度 PAT 不能按路径授权**——令牌的读权限是整个父仓，而守卫只需要
`src/routes`（稀疏锥只是让 CI **落盘**的少）。所以凭据本身按「最小必要 = 只读、只这一个仓、
设过期」来收。

## 配置步骤（人做，一次性）

凭据的值**不进本仓、不进任何提交、不打印在对话里**：

1. 父仓 `BigLeopardCat/Saudade-Blog` → Settings → Developer settings → **Personal access tokens
   → Fine-grained tokens → Generate new token**；
2. **Repository access**：`Only select repositories` → 只勾 `Saudade-Blog`；
3. **Permissions → Repository permissions → Contents: Read-only**（**只要这一项**，别的都留
   `No access`）；
4. **Expiration**：设一个（90 天）——过期即 CI 红在凭据自检那一步，红的文字直接指回本文；
5. 把值存进 agent 仓的 secret（命令会让粘贴，不回显）：
   `gh secret set PARENT_REPO_TOKEN --repo BigLeopardCat/saudade-blog-agent`

**轮换**：重跑第 1-4 步生成新票 → 重跑第 5 步覆盖 → 删旧票。三步都不需要改代码。

## 残余风险（都知道，不假装没有）

1. **比的是父仓默认分支的 HEAD**：Rust 的改动还没并进默认分支时，CI 会红而本机（工作区带着
   那份改动）是绿的。这是**可见的**红，不是静默的绿——按纪律，改 Rust 那一侧要先把改动并进
   默认分支，再看 agent 侧 CI。
2. **fork 里没有这个 secret** ⇒ fork 自己的 CI 会红在凭据自检那一步。对这个 public 仓而言
   这是可接受的：它明确说了「这道闸要配置才能跑」，而不是假装跑过了。
3. **凭据范围比需要的大**（整个父仓的只读，不能只授 `src/routes`），用设置过期与
   「只勾这一个仓」来收。
4. **锥与守卫的对应关系是判出来的**：新守卫读新路径要一起改 `sparse-checkout`（⑥ 会红着
   告诉你），这是刻意的摩擦。

## 实测

- **本机预演**（20260928）：把父仓按 CI 的同一形态拉一份（`--depth 1` + `sparse-checkout
  set src/routes`），然后
  `SAUDADE_PARENT_REPO=<那份> SAUDADE_REQUIRE_PARENT=1 .venv/bin/python tests/run_all.py`
  → **60/60 通过**；逐个跑七处守卫，`⏭ 跳过父仓断言` 命中数 **0/7**（= 都真判了）。
- **CI 首跑**（20260928，run `36435335977`，提交 `335f8ca`）：**绿**。现场读数——父仓 checkout
  与落位自检两步通过；离线套件 `60/60`；日志里 `跳过父仓断言` 命中 **0**（= 七处守卫在 CI 里
  **全部真判**），例如「父仓 `render_exec_row` 按 doc_type 分名词（board/talk/announcement
  三臂）」「父仓 talks.rs 的审核失败分支确实返回「转人工待审」(0, None, None, None)」「Rust
  那半读回执顶层的 `action`」。**这些行在 CI 里此前一次也没出现过**——那七处守卫过去在这里
  恒返 `None`，两侧的绿谁也没证明过对方。
