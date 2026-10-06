# 参数调优实验：不同模型 / 不同旋钮（20261006）

> 这份报告回答一件事：**把 planner 的旋钮拨到别处，agent 会变好吗**。
> 载体是 `eval/param_matrix.py`（本批新建）；读数落在 `eval/report/param_matrix.jsonl`
> （append-only，每跑完一遍追加一行）。
>
> ⚠️ **本文的所有结论都受本仓那两条纪律约束**，不满足就别引：
> ① **一臂最少两遍**——同一份代码实测过 10 红 vs 3 红（`20261005_234824` vs
> `20261006_002223`），单跑读数不可判读；② **读计数不读通过率**——聚合没退化 ≠
> 没有一条变坏。

## 一、载体与它刻意不做的事

`eval/param_matrix.py` 与既有的 `eval/golden_arm.py` 是**两回事**，别混：

| | `GOLDEN_ARM` | `param_matrix` |
|---|---|---|
| 换的是什么 | **哪一套循环**（graph / ReAct） | **同一套循环的旋钮** |
| 怎么换 | 新建 `agent.<臂>_arm` 模块 | **环境变量覆盖**（pydantic-settings 里环境变量优先于 `.env`） |
| 为什么 | 两种拓扑各有自己的模块 | 改代码跑矩阵 = 每臂一次提交，既不可比又留不下读数 |

它**不**写 `last_run.json`、不碰 `eval/golden/**`、不动 `TARGET/FLOOR/ENTRY`、不动分母。
判据的变更仍只归 `eval/run_golden.py` 一条路；本模块只**拨旋钮、读数**。

> ⚠️ **上面那半句（不写 `last_run.json`）在本报告成稿时是句空话**（20261006 晚已修，
> 见 `docs/问题记录.md` §1.52）。当时本模块**不给子进程设 `GOLDEN_ARM`** ⇒ 臂名恒 `graph`
> ⇒ 留档写进**生产档那个目录** `eval/report/runs/`，`landing_gate` 的 readiness / 慢性红榜
> 按目录整扫，把这批调参读数**当成生产档夜间读数**收进窗口（最近 12 份"全量"里 9 份是调参跑、
> 最近 5 夜窗口 100% 是调参跑）。**本报告的结论不受影响**（每个数字都从
> `param_matrix.jsonl` 逐臂读的，没走窗口），但当时**引这张榜排的红榜口径是脏的**。
> 现在调参档跑的是 `matrix` 臂，留档落 `eval/report/runs_matrix/`，那句话才**变成真的**。
> 顺带一条准确的记录：那 9 份留档的 `full_run` 全是 `False` ⇒ **`last_run.json` 一次都没被
> 覆盖**（闸开着、恰好没响）——"没响"不等于"设计对了"。

每个臂必须带 `GOLDEN_ADMIN_UID=721 GOLDEN_USER_UID=722`（`IDENTITY_ENV`）——不带
就只有 138/119 的分母，与历史读数不可比。`_env_for` 会**先剥掉外层壳里的
`PLANNER_*` / `LLM_PROVIDER` / `LLM_SEED` / `GOLDEN_*` / `*_MODEL`** 再铺臂的覆盖：
否则一次手误的 `export PLANNER_TEMPERATURE=0.9` 会静默渗进每一个臂，而表上看不出来。

## 二、臂的定义

| 臂 | 覆盖 | 它问的问题 |
|---|---|---|
| `live` | （不改旋钮） | 对照臂：线上正在跑的那一套 |
| `t0.0` | `PLANNER_TEMPERATURE=0.0` | 确定性档（本批设为默认） |
| `t0.2` | `PLANNER_TEMPERATURE=0.2` | 回退臂：逐字节复现 20261006 之前的写死值 |
| `think` | `PLANNER_NATIVE_THINKING=1` | 规划开思考买"分类更准"的代价 |
| `ali-ds` | `QWEN_MODEL=deepseek-v4.1-flash`（其余全不动） | 跨模型那一格**正确的走法**（主人 20261006 指正）：阿里那套 API 的同一个 `base_url`/key 上就有 deepseek 档，换的只是**模型名** |
| `ds-chat` | `LLM_PROVIDER=deepseek` + `DEEPSEEK_MODEL=deepseek-chat` | ⚠️ **官方端点**，不是本栈换 deepseek 的走法——留作"型号对、通路不对"的对照（见 §五） |
| `ds-flash` | `LLM_PROVIDER=deepseek` | ⚠️ **官方端点**的**负控**：已知跑不了（见 §五） |

> ⚠️ **`ds-chat` / `ds-flash` 两条读数不能回答「这个栈换 deepseek 行不行」**——它们走的是
> DeepSeek **官方端点**（`DEEPSEEK_BASE_URL`），那是另一条通路。**回答那一格的是 `ali-ds`**。

> ⚠️ **本表里没有 `text` 那一档，也不可能有**：接口层的 `planner_engine` 拨盘 20261004 已删
> （`config/settings.py:104-110`、`eval/dial_matrix.py:13-15`、源码锁 `tests/test_ci_suite_list.py:128`）。
> 任何地方再出现「text 档」的读数，都是 **20260927 那批的历史对照**，不是可拨的档位。

## 三、温度 A/B：结论是**中性**

两臂**交替**各跑两遍全量（134 分母），与 8 跑基线的窗口对照：

| 口径 | 基线（8 跑窗口） | `t0.0`（2 遍） | `t0.2`（2 遍） |
|---|---|---|---|
| 形态级成对分歧 | 18.86%（379/2010） | **21.1%**（32/152） | **18.4%**（28/152） |
| 技能级成对分歧 | 13.18%（265/2010） | **12.5%**（19/152） | **13.2%**（20/152） |
| 采样层红数（逐遍） | 均值 5.875（σ≈3.27） | 7 / 4（均值 5.50） | 4 / 6（均值 5.00） |
| 采样层下界（逐遍） | — | 0.8961 / 0.9258 | 0.9258 / 0.9058 |
| 硬层 | ✅ | ✅ | ✅ |
| p50 / p95 秒 | — | 6.7 / 19.1 | 6.5 / 19.4 |
| 工具调用合计 | — | 192 / 195 | 193 / 203 |

**读数怎么念（这一节比表本身重要）**：

- 三个口径、两个方向**都没有一致的偏移**：技能级分歧 `t0.0` 略低（12.5% vs 13.2%）、
  形态级分歧 `t0.0` 反而略高（21.1% vs 18.4%）——两个方向相反，说明差异落在噪声里。
- 红数的**臂间差 0.5 条**，而同一臂两遍自己就差了 2–3 条，基线窗口的 σ≈3.3。
  以 n=2/臂 的检定力，**小于约 4 条红的效应本来就测不出来**。所以正确的读法不只是
  "没提升"，而是"**这次实验没有能力发现小的提升**"——两句话都要说，只报前者等于
  把"测不出"说成"不存在"。
- 三遍都动的唯一一条是 `admin_tag_move_question_no_popup`（见 §四）。

**结论：这枚旋钮在这个服务商上是中性的。** 按事先说好的口径（"没提升也没下降就这样吧"）
保留 `0.0`。回退路径一行：`PLANNER_TEMPERATURE=0.2` 即逐字节回到 20261006 之前。

## 四、被证伪的归因（本节是这份报告里最该被读到的一段）

动手前我的假设是：「8 跑窗口里 134 条有 41 条换过 round 0 技能，其中 27 条**输入逐字
相同**却换了分支 ⇒ 纯采样 ⇒ 温度是唯一能治它们的旋钮」。这个归因**没有被 A/B 证实**，
而且它当初的证据本身有缺口：

- 那版"输入逐字相同"的比对**只归一化了 `current_time=`**，没盖住 `planner.context` 里
  `page_ctx` 的**台账年龄字符串**（`· 已执行: 10-06 04:41（3 小时 43 分前·已过期）`）——
  那串字**逐分钟在变**，所以"逐字相同"这个前提很可能压根不成立。
  （这条候选**也只是候选**，没有被单独验证过。谁要接着查，先做一个便宜的对照：
  把 `page_ctx` 里年龄串抹成一个常数再跑 8 遍，看 41 条降不降。）
- 复查 41 条不稳用例里，只有 **1** 条命中 `is_own_read_question` / `is_site_corpus_question`；
  而同族里另有 **14** 条命中它们、却已经是 6/6 稳定落在 `content_query`。所以
  `data_question_no_tool` 那条纠偏**从未触发不是因为路死了，是因为人群是空的**——
  零杠杆，这个方向已排除。

**因此**：`config/settings.py` 里留下的注释**只许说"这枚旋钮是中性的"，不许再说
"温度能治路由抖动"**。残留抖动的来源另找。

### 顺带发现：一半的"路由不稳"是记账差异

两个臂里都有大量用例在

```
chat|stop|空          ← 一个函数都没点（会被 no_call_nudge 纠偏一次）
chat|tool_calls|chat  ← 显式点了 chat
```

之间翻——**落点是同一个 chat，用户可见行为也相同**。这就是为什么形态级看着吓人
（18–21%）而技能级只有 13%。**两个口径都要看**：差在形态级、平在技能级的那些不是
决策乱跳，是记账。

### 唯一一条系统性红

四遍（两臂各两遍）的红集**只交出交集一条**：

```
t0.0 rep1: rag_git_snapshot, rag_arch_components, admin_tag_move_question_no_popup,
           admin_board_audit_reviewed_refusal, own_favorite_add_not_logged_in,
           own_mark_read_not_logged_in, aggregate_two_docs_compare
t0.2 rep1: image_two_colors, guestboard_talk_double_source, challenge_claim_phantom_nav,
           admin_tag_move_question_no_popup
t0.0 rep2: rag_project_files, cq_no_bare_claim_techdoc, admin_tag_move_question_no_popup,
           admin_board_unresolved_target_honest
t0.2 rep2: rag_git_svn, repeat_ask_no_verbatim, admin_tag_move_question_no_popup,
           admin_announcement_question_no_popup, admin_board_unresolved_target_honest,
           admin_tag_create_ambiguous_target_no_write
```

- **`admin_tag_move_question_no_popup` 4/4 红**——它不是噪声（4/4 是**本实验自己**的读数，
  不依赖任何窗口）。它在"温度"这个因子上**完全不动**，说明它的病灶在别处，与采样无关。
  > 成稿时这里还引了一句「同时是 `landing_gate --red-rank` 榜上的 3/10」——**那个排名作废**：
  > 它算在**被调参档污染的窗口**上（见 §一的 ⚠️）。清干净之后同一批红条目排在
  > `8/62`（并列名次也变了）。要引排名，用清干净之后的榜重算一次。
- 除它以外**每一条红都只出现在 2/4 或 1/4 遍里**，且几乎没有跨臂交集。
  这正是"单跑读数不可判读"的样本：**红数在 4–7 之间晃，红的身份每次都不一样**。
  想按条目做 A/B，2 遍远远不够——至少 6–8 遍起。

> 这条不是本次实验的产物，是它的**副产品**：把四遍红集并排一放，"哪条是真红、
> 哪条是噪声"第一次有了一个便宜的判据（出现频次），而不必等 8 跑窗口。

## 五、跨模型那一格：`deepseek-flash` 跑不了，而且**不是旋钮的问题**

第一次预检 `ds-flash` 臂 0/2，两次都是 HTTP 400：

```
The `reasoning_content` in the thinking mode must be passed back to the API.
```

根因：`deepseek-flash` 是**推理模型**，多轮工具调用时要求把上一轮的
`reasoning_content` 原样回传；而本仓的 `with_tool_call_pairs` 只回填
`tool_calls` 的配对，**不回填 `reasoning_content`**。所以这是一处**代码层不兼容**，
不是"改个环境变量就能换的模型档"——把它当配置项拨过去，只会得到一个必然的 400。

- `ds-flash` 因此**保留在 `ARM_SPECS` 里当负控**：它每次都以同一个 400 立刻失败。
  谁要是把"换个模型"当成纯配置动作，跑这个臂是最快的反例。
- 跨模型那一格改由 `ds-chat`（`deepseek-chat`，非推理档）承担；3 条预检 1.000。
- **要真上推理档**，得先让 `with_tool_call_pairs` 回填 `reasoning_content`——
  那是一处独立的代码改动，不在本批。
- ⚠️ **但这一节**（和上面那个 400）**只说明「DeepSeek 官方端点」这条通路不通**，
  **不说明"这个栈换不了 deepseek"**。主人 20261006 指正的走法是**在阿里那套 API 上换模型名**
  （`QWEN_MODEL=deepseek-v4.1-flash`，同一个 `base_url`/key、不切 `LLM_PROVIDER`）——
  臂 `ali-ds`，读数见 §六。

## 六、`think` 与跨模型两臂（读数已到齐；`ali-ds` 除外）

线上是**关着思考**跑的（`planner_native_thinking` 是 native 三项里的待拍板项）。
`think` 买的是"分类更准"，付的是 `max_tokens=1200` 里思考链先吃掉一截——
**这是一个有明确代价的假设，不是"免费变好"**。

| 臂 | 逐遍 | 采样层红数 | 点估计 | 下界 | 硬层 | resets | p50 / p95 秒 | 工具调用 |
|---|---|---|---|---|---|---|---|---|
| `think` | rep1 | 7/134 | 0.9552 | 0.9058 | **❌**（回归组 `followup_entity_slot_ambiguous`） | 1 | 14.8 / 47.1 | 179 |
| `think` | rep2 | 8/134 | 0.9403 | 0.8866 | ✅ | 4 | 16.9 / 42.8 | 191 |
| `ds-chat` | rep1 | **41/134** | 0.7239 | 0.6427 | **❌** | 3 | **2.9 / 8.4** | 163 |
| `ds-chat` | rep2 | **43/134** | 0.7164 | 0.6349 | **❌** | 4 | **2.9 / 8.5** | 162 |
| （对照）`live` | rep1 | 10/134 | 0.9254 | 0.8681 | ✅ | 0 | 6.3 / 18.6 | 202 |

**怎么念**：

- **`think` 不是免费的，而且两遍里一遍硬层红。** planner 端到端 p50 从生产的 ≈6.3s 抬到
  **14.8 / 16.9s**（2.4–2.7 倍）、p95 到 47s；rep2 的下界 0.8866 **低于入口档**（`below_entry`）。
  以 n=2/臂 的检定力，7 vs 8 这条分差**不足以定论"更差"**——但 §1.9 那批旧数据（max 档上
  思考与不思考**逐条完全相同**、思考只贵 4.6 倍）已经不支持开它。**结论：维持生产关思考。**
- **`ds-chat` 是本批唯一一条 `collapse`**：42 条红、两遍几乎逐字相同（41 vs 43），红集覆盖
  `rag_*` 整族。**它快（p50 2.9s，不到生产的一半）——快是因为它不干活**：工具调用总数
  162/163 vs 生产 202，检索族成片零工具。这正是 §四末那段"**最低分歧＝最差臂**"的又一次现身。
  ⚠️ 但它**不能**回答「这个栈换 deepseek 行不行」——见 §五末与下条。

### `ali-ds`：跨模型那一格**正确的走法**（本次**未取得全量读数**）

主人 20261006 指正：**DeepSeek 不需要切 `LLM_PROVIDER`**——阿里那套 API 的**同一个
`base_url`/key 上就有 deepseek 档**，换的只是模型名（`QWEN_MODEL=deepseek-v4.1-flash`）。
上面两条 `ds-*` 走的是 DeepSeek **官方端点**：**型号对、通路不对**，它们连着 §五那个 400
只说明官方端点的契约与本仓不兼容。

- 预检（`--limit 8`）：**8/8 PASS、resets 0、p50 4.6s** ⇒ **通道本身是通的**。
- **全量读数本批没拿到**：那轮 `ali-ds` 只写出 2 份 trace（14:14:07、14:14:11）就停了。
  内核查到两次 **OOM**（`14:11:30` node；`14:27:14` python `MainThread`，anon-rss 1.67GB），
  外加会话结束把外层 shell 一并收走。**这是 3.7GB 机器上的环境事故，不是模型通路的问题**
  ——预检 8/8 走的就是同一条路。
- 所以跨模型那一格的结论**只有一句：待跑**。要引它，先拿 `ali-ds` 的全量读数。

> 跑法：`.venv/bin/python eval/param_matrix.py --arms think ali-ds --reps 1`
> 读数：`eval/report/param_matrix.jsonl`；看表：`--report`
> ⚠️ 一轮 ≈20–25 分钟；别与 04:00 夜跑并发，也别连开两轮（`/tmp/golden_cases` 会互踩）。

## 七、本批**没做**的事

- 没动 `TARGET` / `ENTRY` / `FLOOR`，没动分母，没新增/删除 golden 用例。
- 没把任何"未验证的通路"设成默认：`llm_seed` 默认 **0 = 不设**是刻意的
  （设 seed 等于换一条采样通路，服务商侧的对齐/批处理都可能变，没有 A/B 不该设成默认）。
  它留在配置里是**为了本实验**——`LLM_SEED` 可逐臂拨，和温度分开成两个因子。
- 没修残留抖动。**归因已被证伪的那一条不许再引用**（§四）。
- **没拿到 `ali-ds` 的全量读数**（OOM 中断，只有预检 8/8）⇒ **跨模型那一格本批没有结论**，
  见 §六末。别拿 `ds-chat` 的 42 条红代它答这一格。
- 没动内部计划文本协议（`plan_encode` / `parse_plan`）——**那一层不在本报告的射程里**，
  与接口层的 `planner_engine` 拨盘是**两回事**（后者 20261004 已删）。
