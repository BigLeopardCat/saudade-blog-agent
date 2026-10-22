# 工具级菜单 POC：把技能模板换成真工具表，harness 撑不撑得住

**一句话结论**：**撑不住——但撑不住的地方不是"执行"，是"选哪个工具"和"多步算不算完成"。**
只读射程内，工具臂（`GOLDEN_ARM=tools`）在可比子集上 **0.6725**，同一批用例同一模型同一轮，
`graph` **0.9825**、`react` **0.9766**；19 条 regression 硬层 **53/57**（另两臂 57/57）；
工具轮数 **489 vs 235**（2.1 倍）。⇒ 手写模板在射程内不是"叙述纪律"那一层的事，
它同时在干**路由**和**定序**这两件被低估的活。**第二轮（写族）暂不开**，
先看 §5 的两条可操作结论。

> 这篇是**记录型**：一次实验的读数与失败形状。**不改生产**——`graph.py` / `planner_llm.py` /
> `workflow_*` 一个字节没动，新臂只 import；POC 代码在 `feat/tool-menu-poc` 上。

## 1. 要答的问题与跑法

**问题**：生产 planner 的动作空间是 50 条手写技能模板（`agent/workflow_defs/`），模板再展开成
**固定工具序列**（`agent/workflow_plan.py::instantiate_plan`）。两条成本：① 维护面；
② **每加一个能力就要写一条模板——而这份成本不随模型变强而下降**，与"工具要少而可组合"相反。
所以问：**把菜单换成按角色过滤的真工具表、让模型自己组合调用，整条 harness 撑不撑得住？**

**跑法**（[eval/experiments/menu_arm_ab.py](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/eval/experiments/menu_arm_ab.py)，三臂交错）：

```
.venv/bin/python eval/experiments/menu_arm_ab.py --reps 3 --arms graph,react,tools \
    --provider deepseek --retry-outage 2 --tag poc_menu_ds_r3b
```

- **射程 = 只读 + 导航 + 检索族**（81 条预注册 id，[eval/experiments/poc_menu_ids.txt](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/eval/experiments/poc_menu_ids.txt)，
  跑前提交、跑后没改）。写族 30 条不在第一轮；**菜单本身没有按射程收窄**（70 个工具并成
  "几个可组合的"本身就是另一种手写分组，那是第二轮）。
- **模型 = DeepSeek**。qwen 在启动当天中途 `403 AccessDenied.Unpurchased` 污染了 5 遍，
  换 provider 重跑（`--provider` 只给子进程，`.env` 与生产服务一个字没动）。
- 三臂同一批、逐遍交错，3 遍是**下限**不是奢侈（单遍不可判读：相邻两次同配置红集只重叠几条）。

## 2. 读数——先过许可闸，再读

### 2.1 许可闸：`comparable = False`，`pass_rate` 原始值不许引用

`eval/arm_capability.py --arm tools --probe` ⇒ **退出码 3**，`comparable = False`：

```
  forbid_frame_prefix   23 断言   0 正向兄弟   0 归档见到   ❌
  forbid_task_state      1 断言   0 正向兄弟   0 归档见到   ❌
```

两族是**纯否定**断言，而本臂**结构上产不出**它们指涉的东西 ⇒ 整族恒真，白送。
这不是"它做对了"，是"它做不到，所以判据问不出来"——同 [react-line-experiment.md](react-line-experiment.md)
§六 那条病。**消费方式**：取 `--print-skip-ids` 那份名单（24 条，其中 **5 条**在预注册表里，
已真的跑过），**两臂都**用同一份名单收窄分母再比——不许"子集跑 vs 全量跑"。

### 2.2 可比子集（去 5 条不可证伪 + 回归组另计，各 3 遍）

| 臂 | 可比 | 点估计 | CI95 | regression 硬层 |
|---|---|---|---|---|
| `graph`（生产受限规划） | 168/171 | **0.9825** | [0.9497, 0.9940] | 57/57 |
| `react`（技能菜单，ReAct 循环） | 167/171 | **0.9766** | [0.9414, 0.9909] | 57/57 |
| `tools`（**工具菜单**，POC） | 115/171 | **0.6725** | [0.5991, 0.7384] | **53/57** |

逐遍原始计数（80 条真跑的）：

```
graph  79 / 79 / 79          react  79 / 79 / 78          tools  60 / 58 / 56
```

**三遍的极差**：graph 0、react 1、tools 4 —— 工具臂不只是更低，还**更抖**。

### 2.3 三对判决（`landing_gate.ab_compare`，容差 0.005）

| 对 | 判决 | 下界 before → after |
|---|---|---|
| `react↔tools`（**菜单形状**，最干净的一对） | `regressed` | 0.9452 → 0.5899（Δ **-0.3553**） |
| `graph↔tools`（本题） | `regressed` | 0.9529 → 0.5899（Δ **-0.363**） |
| `graph↔react`（参照，带缺资产 confound） | `regressed` | 0.9529 → 0.9452（Δ **-0.0077**） |

⚠️ 这里的 before/after 是 **Wilson 下界**，不是点估计（所以和 §2.2 的 0.9825 对不上是正常的）。
⚠️ `graph↔react` 只差 0.0077、刚过容差，且这一对**带 confound**（main 上的 react 臂是欠配版，
资产对齐那 19 个提交在一个未 push 的分支上）⇒ 只当参照，不当结论。
⚠️ `ab_compare` 的 `per_case_fell` / `per_case_rose` 里那两个数字是**红率不是通过率**——
`↓`＝红率降＝变好。驱动的打印已加表头点明（`fix: A/B 驱动的逐条差集标成红率语义`），
因为第一版按通过率读**每一条极性都是反的**。

## 3. 失败形状：五族（这节比通过率值钱）

66 条红（25 条不同用例 × 3 遍，其中 18 条 3/3 全红）分下来是五种：

### A 类：菜单太大 ⇒ 在近义工具里选错（**主导族，13 条用例 / 32 条红**）

**先纠一处归因**：`task_intents` / `task_hold` / `task_drop` **不是博客功能提供的工具**，
它们是 agent 自己的**跨轮任务记账口**（伪函数，`agent/planner_llm.py` 那三个），
被 [agent/tool_menu.py](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/agent/tool_menu.py) 照搬进工具菜单（admin 73 件里有这 3 件）。所以这一族红
**不是"博客工具太乱"**，是**工具臂把技能臂的"申报通道"搬进菜单时，让"申报"和"动作"
在同一个平面上竞争**。模型对"带我去首页"挑了 `task_intents`：

```
nav_home               红红红   calls=['task_intents','task_hold','task_intents','task_hold','task_intents']
   失败判据：缺少 NAVIGATE: 命令帧 / 缺少 AUTO_NAVIGATE: 命令帧
   回复逐字：「…我这边只收到系统返回的『工具不在本轮可执行清单里』…需要我再试一次带你回首页吗？:委屈:」
```

⚠️ **这个洞在开发期就撞到过，补丁没压住**——`agent/tool_prompt.py::pseudo_menu_text` 的头注
逐字记着第一版把它们从菜单**文本**里整个滤掉，结果 `nav_home` 上"模型连着四轮只调
`task_intents`、一次 `navigate_to` 都没调"，于是单列一段、第一句钉死"**它不代替动作**"。
**全量 3 遍跑下来 `nav_home` 仍然 3/3 红、形态逐字相同** ⇒ 提示词层面的补丁对这条无效。

⚠️ **还有一处两臂词汇串了**：`task_intents` 的 schema 描述至今写着"workflow（你打算用哪个
**本领**办它，填上面那些**本领名**之一）"——而工具臂的提示词明写"**没有'技能'这一层**"。
同一份上下文里两句话互相打脸，而模型的说明入口是 schema（提示词让它"用法见它们各自的
schema"）。**这是一处具体的、可修的缺陷**，不是"模型不行"。

同族还有 `nav_request_phrase` / `nav_direct_no_confirm_promise` / `nav_no_false_zero_tool_claim` /
`nav_board_position_from_notice` / `nav_comment_position_from_notice` / `nav_param_anchor_about` /
`nav_device_console` / `zako_nav_request_refused`（这条更重：**本来一个工具都不该调**，它调了
`task_intents`）；`multi_step_effect_then_nav` / `mix5_cmd_nav_read_three_way` 是同一族的跨步版本。

**另一副面孔是"调了菜单里没有的名字"**（1.1%：455 次调用里 5 次）。`todo_multi_step_serial`
该调 `get_article_detail`，调了 **`read_article`——那是技能名**（`agent/workflow_defs/` 里的；20261021 起它住 `read_article.toml`），
工具菜单里没有它。⇒ 模型手上仍有一份**技能名**词汇表（来源待查：`task_intents` 的
"本领名"描述是嫌疑之一），而工具臂的菜单认不出它。
近义名同族：`guestboard_talk_double_source` 该调 `list_talks`，调了 `list_guestbook`。

**这一族里"工具描述不清"占多少？** 逐条读了描述：`list_guestbook` 写"留言板（河灯集）"、
`list_talks` 写"说说（动态/碎语）"、`search_notes` 与 `rag_search` 各自写了分工——
**描述是够的**。人看不懂那两条缺的是**"河灯集与说说在这个站上是两样东西"这个站点知识**，
不是缺描述。⇒ **"工具重叠"这个说法要收窄**：它们不是重复实现（合并会丢能力），
是**平铺且没有路由信号**。

> 这一族**在技能臂上不存在**，因为技能名就是意图名（`navigate` / `content_query`），
> 模板替模型做完了"意图 → 工具"的翻译。工具臂把这个翻译**还给了模型**，而模型在
> 67 个工具里做得不好。

### B 类：多步序列漏步（8 条）

模板给的是**固定工具序列**；模型自己组合会只做第一步就收尾：

```
admin_todo_list_includes_pending_review  该三件，只调了 list_dashboard_todos
   fails: checker 验收回执缺少工具 get_moderation_status（exec：['list_dashboard_todos']）
          checker 验收回执缺少工具 list_quota_requests（exec：['list_dashboard_todos']）
todo_multi_step_serial      未调用工具 get_article_detail（已调用：['read_article','navigate_to']）
multi_step_effect_then_nav  checker 验收回执缺少工具 navigate_to（exec：['toggle_effect']）
task_state_resume_settle    checker 验收回执缺少工具 toggle_effect（exec：[]）
```

### C 类：检索族——**查了，但文本契约没落**（4 条）

这是最该注意的一族：`rag_noise_docker` / `rag_fingerprint_pin` / `rag_project_files` /
`cq_query_embedded_articles` 全都**真的调了** `rag_search` + `search_notes` + `get_article_detail`
（有的调了 6 次），红的是**回复正文**：

```
rag_noise_docker   文本缺少任一关键词 ['没有','没找到','暂无','未收录','没写','没有相关']
cq_query_embedded_articles   文本缺少任一关键词 ['ESP32']
```

⇒ 不是"没查"，是**「查不到时要如实说没有」「答到点上」这些散文契约没有落点**。
工具臂没有 `reply_contract` / `complete_when` 的位置。

**这条与仓里既有的结论是同一个发现、另一条轴**：
`saudade-agent-surface-carries-contract`（「技能面在替契约干活：删规划契约后检索族成片红 ⇒
说明搬进函数 schema」）记的是**规划契约**那次；这次换的是**菜单形状**，红的还是检索族。
两次数到同一族 ⇒ 这不是巧合，是**检索族的判据依赖"有人替它写一句"**。

### D 类：任务帧登记缺失（`mix4_cmd_and_write_one_breath`）

一次话里两件事（一件上卡、一件登记），工具臂只做了上卡那件：
`没上卡的那件事没有留下任务登记：期望有一帧 __TASK__ 的 goal 含 '明天交房租'`。

### E 类：`graph` 独有的红（`format_count_number_only`，3/3 红；另两臂 3/3 绿）

这是**生产臂自己的路由 bug**，不是工具臂的问题，但必须记：主人说"只回我一个数字"，
graph 的 planner 选了 `list_categories` + `list_tags` 而不是 `list_notes`/`search_notes`：

```
graph 文本：「查过分类和标签了，但这两份数据里没有直接给出全站文章总数…这个数字我这轮确认不了喵」
graph 判据红：未调用任一检索工具 ['list_notes','search_notes']（已调用：['list_categories','list_tags']）
```

反方向只有这 1 条 ⇒ 工具臂**不是全输**，它在少数"菜单太窄导致选错"的地方反而对。

## 4. 成本：省掉的模板没有变成更少的轮次

| 臂 | 挂钟 p50 | 工具轮数合计 | 工具调用合计 | planner 决策轮 | 缓存命中率 |
|---|---|---|---|---|---|
| `graph` | **3.7s** | **235** | 291 | 483 | 92.4% |
| `react` | 5.0s | **235** | 307 | — | — |
| `tools` | 5.0s | **489** | 455 | — | — |

**工具轮数 2.1 倍**。`graph` 与 `react` 的轮数**逐字相同（235）**——两臂菜单一样，只是循环形状不同。
工具臂多出来的 254 轮全是**绕圈**（`nav_home` 一轮里 `task_intents`/`task_hold` 来回 5 次）。

⚠️ **`react` / `tools` 的 token 与缓存三格恒 0，这不是"用了 0"**：`llm_done` 与
`native_decision` 两个 trace 事件只在 `graph.py` 的生产节点里记，`react_arm` 的决策节点一个都不记，
而它**真的调了 LLM**。⇒ 跨臂只比挂钟与工具轮数；要把它变成可比的，得先给 react 臂补 `llm_done`
（本轮不做，如实记成缺口）。

## 5. 结论：模板在射程内值多少钱

**它值两样东西，都不是"叙述纪律"**：

1. **意图 → 工具的路由**（A 类，13/25 条用例）。技能名就是意图名，模板把这层翻译
   **做在菜单里**；换成工具表，这层翻译**还给了模型**，而 70 件平铺下它做不好。
   ⇒ **可操作，按代价从低到高**：
   ① **先修 A 类那处词汇串线**（`task_intents` 的 schema 描述还说"填上面那些**本领名**"，
   而工具臂的提示词写着"没有'技能'这一层"）——这是一处两句话互相打脸的文本缺陷，
   不是模型能力问题，**也是唯一一处"改了肯定不亏"的**；
   ② 再**把菜单按意图分组收窄**（"少而可组合"里的"少"）——**但这正是第二轮要证的**，
   分组本身也是手写，别把它当成免费的。
2. **多步任务的定序与"算不算完成"**（B + D 类，9/25 条用例）。工具没有"这件事要三步才算完"
   这个概念，checker 的验收回执因此缺件。

**它不值什么**：执行、闸门、验收、回执那一整段——`authz` 角色过滤、同意闸、参数引用
`$tool[i].field`、checker PASS/BLOCK、gate、零工具 narrator，**在工具臂上一条都没退化**
（`WorkflowExecutor.run_specs` 两臂逐字同源）。⇒ **harness 的"执行半"是稳的，稳的是骨架不是路由。**

**第三样（C 类）两边都不是**：检索族的文本契约既不在模板里也不在工具 schema 里，
它悬在提示词里——**这是第二轮之前就该单独修的**，与菜单形状无关。

**写族要不要另做一轮**：**暂不开**。理由不是风险，是**第一轮已经把结论给出来了**——
A 类（近义工具选错）在只读射程里就已经让 pass_rate 掉 31 个点，写族只会更差
（写参数校正器、`_WRITE_TARGET_CHANNEL`、7 个校正器全部读技能级 `plan_obj["params"]`）。
**先做"按意图分组收窄菜单"这一刀，再谈写族。**

## 6. 边界（不许读大）

- **射程**：只读 + 导航 + 检索。写族 30 条一条没测。
- **`comparable = False`**：本节的 `pass_rate` 只在 §2.2 那份收窄分母上成立；
  **原始 80 条分母的读数不许引用**。
- **模型 = DeepSeek**，不是生产缺省。qwen 那次停摆是环境事故，与结论无关。
- **底座 = `main`**，`react` 臂是欠配版 ⇒ `graph↔react` 那一对带缺资产 confound。
- **3 遍**是下限：25 条不同用例里 7 条是"部分遍红"，这些的逐条读数 n=3，**只当线索不当结论**。
- **`CAPABILITY_DENIAL_OBJECTS` 按技能名索引**，而工具臂的 `workflow` 是从工具名倒推的粗标签
  ⇒ gate 的能力否认那一族在本臂上**可能不开火**，而 `arm_capability` 不追踪它
  （要求型族，不是 `forbid_*`）⇒ 是个只能靠人读报告发现的静默洞，**本轮没测**。

## 7. 复现

```bash
# 离线锁（缺省档零行为变更 + 四个接缝接线）
.venv/bin/python tests/test_arm_seams.py

# 许可闸（必须 comparable=True 才配引用 pass_rate；False ⇒ 退出码 3）
.venv/bin/python eval/arm_capability.py --arm tools --probe
.venv/bin/python eval/arm_capability.py --arm tools --probe --print-skip-ids

# 三臂交错（3 遍是下限）
.venv/bin/python eval/experiments/menu_arm_ab.py --reps 3 --arms graph,react,tools \
    --provider deepseek --retry-outage 2 --tag poc_menu_ds_r3b
```

读数落 `eval/report/baseline_poc_menu_ab.json`（驱动的汇总）与 `eval/report/runs*/*.json`
（逐遍原始报告，`runs` / `runs_react` / `runs_tools`）。

**环境三件**（本机跑 golden 的硬前提，缺一条读数就是假的）：cwd 必须是**主仓**
（`.env` 是相对路径）、`PYTHONPATH` 指向 worktree、身份通道显式导出
（`GOLDEN_ADMIN_UID=756 GOLDEN_USER_UID=722`）。驱动已把这三件写死在 `run_arm` 里。

**POC 代码**：[agent/tools_arm.py](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/agent/tools_arm.py)（工具臂）、[agent/tool_prompt.py](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/agent/tool_prompt.py)（工具臂系统提示）、
[agent/react_arm.py](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/agent/react_arm.py) 的四个接缝（`prompt_builder` / `menu_builder` / `plan_builder` /
`executor_factory`，缺省全 `None` ⇒ 逐字节回落）、`agent/react_line.py::WorkflowExecutor.run_specs`。
变更记录见 `changelog.d/20261020-tools-arm-seams.md`。
