# ADR-0011：上下文由图自己持有（原生 checkpoint 取代手写注入）——方向认可，采纳待写幂等账
> ⚠️ 20261018 又一批模块已改名（**记录型文档一律保留当时的名字，不回溯改写**）：`agent/absence.py` → `agent/absence_claims.py`、`agent/frames.py` → `agent/frame_contract.py`、`agent/probe.py` → `agent/runtime_probe.py`、`agent/decisions.py` → `agent/deterministic_plan.py`、`agent/entities.py` → `agent/entity_digest.py`、`agent/refs.py` → `agent/arg_refs.py`、`agent/workflow_defs/nav.py` → `agent/workflow_defs/nav_map.py`、`agent/workflow_defs/navigation.py` → `agent/workflow_defs/nav_skills.py`、`agent/workflow_defs/reports.py` → `agent/workflow_defs/report_skills.py`、`agent/context.py` → `agent/prompt_context.py`。**现状地图（每个模块一行）**见 `docs/agent-architecture.md` §2。

- **状态**：**已采纳**（20261018，代码面）——"三处一起动"已落地，见文末《20261018 追记：
  采纳落地》。⚠️ **代码落地 ≠ 生产在跑**：DSN 空则 `build_checkpointer()` 返回 `None`、
  图不挂 saver，行为与采纳前逐字节相同；跑没跑看 `/health` 的 `checkpoint_backend`。
  - 采纳前的状态是**已搁置**（20261016）：方向认可，**采纳的触发条件是批 2（写操作幂等账）
    落地**。**该条件已于 20261016 满足**（批 2 落地，见
    [adr-0012-write-idempotency-ledger.md](adr-0012-write-idempotency-ledger.md)）；
    采纳本身（三处一起动）当时仍未做，所以那两天状态仍是搁置。
- **背景文档**：[../agent-architecture.md](../agent-architecture.md) §4.1（记忆四层）、
  §4.6（`MemorySaver` 为什么不承担记忆）、§4.7（"无状态图能不能中途暂停"）；
  仓外判决文档 `/home/ubuntu/native-checkpoint-verdict-20261015.md`（批 0 的实验读数与
  本文大量数字的出处）。
- **影响范围**：**本 ADR 写在一支实验分支上**（`exp/native-checkpoint`），
  `agent/context.py`（新增 `narrator_messages` 及其四个内部件；修 `_prev_user_msg`）、
  `agent/graph.py`（新增 `_narrator_msgs`、`model_node` 改调它、探针加一格 `narr_ident`）、
  `agent/checkpoint_delta_arm.py`（**新增**：臂 C）、`eval/golden_arm.py`、
  `eval/run_golden.py`（`GOLDEN_PROD_HISTORY`）、`eval/experiments/*`、
  `tests/test_narrator_window.py`（**新增**）、`tests/test_turn_window.py`、
  `tests/test_golden_prod_history.py`（**新增**）；文档面 = 本 ADR、
  `docs/agent-architecture.md`（§4.8 追记 + §4.6/§10.5 各一句前向指针）、`CHANGELOG.md`。
  **`server.py` 一个字节都没动**——`_build_messages` 的注入循环、`thread_id` 的随机化都
  原样留着，因为"停止注入"这一刀属于**采纳**（见《决策 一》）。
- **判据**：`tests/test_narrator_window.py`（纯函数恒等 + 合成脏清单 + 反向对照）、
  `tests/test_turn_window.py` 规则 F（`model_node` 的 narrator 消息表**必须**来自闸门，
  `with_tool_call_pairs` 的实参不许是裸读）、`tests/test_golden_prod_history.py`
  （注入的历史必须**读得动**）、
  `eval/experiments/checkpoint_probe.py --narrator-window`（真 saver 的 12 轮正/反向对照）。
  **判决性读数**见《读数》一节（定点块 × 4 臂 × 2 遍交替，104 跑）。
- **触发条件**：**批 2 写操作幂等账落地**。理由见文末——恢复单位是 superstep，节点内的
  state 更新要等节点返回才落盘，所以幂等键**不能**住在 state 里。在那之前不许真开 saver。

## 背景

### 现状：跨轮记忆是**图外**拼出来的

`server._build_messages`（`server.py:627-753`）每轮**整个重建**上下文：一条
`[System: …]` 块（页面/时间/额度/摘要/台账/待办/重复提问注记）+ `req.history[-20:]`
（assistant 侧过 `strip_fact_lines`、孤儿 user 剪枝）+ 当前问题；线程每请求随机
（`server.py:870`/`:1868`）。图**不持有**任何历史——`add_messages` 的追加永远没有"上一轮"
可追加，那份手写注入就是**唯一**的跨轮记忆。

这是 §4.6 那次事故的产物（线程复用 ⇒ 长对话上下文与 worker 内存无限膨胀），当时的结论
是"线程复用是禁区"。这条结论**今天仍然成立**，但它的成因在 20261015 被量清楚了。

### 批 0 量到的：禁区不是"复用线程"，是"复用线程之后没人知道本轮到哪儿为止"

判决文档 §4.6 的实测（同一线程跑 N 轮）：

- **注入的历史被 reducer 逐轮复制**：`_build_messages` 每轮把 `history[-20:]` 注入成
  **真消息**，而图自己也在攒——于是第 r 轮的历史被注入 r 次，提示词 O(轮数²) 膨胀；
- **读端没有一处知道"本轮"到哪儿为止**：`_page_ctx` 把上一轮的页面当本轮报给模型
  （用户人在留言板、模型被告知在首页），`_has_frames` 把上一轮的帧当本轮证据
  （首轮那五条确定性快道**结构上**永不命中），`frame_chars` 同理；
- 崩溃恢复的单位是 **superstep**（整节点重跑）⇒ 写操作**会执行两次**（§2.6）。

所以"膨胀"这件事有**两个**成因，而 §4.6 当时只记了后果：① 图持有整条会话（这是想要的）；
② 图**又**被注入了一遍（这是重复的）。批 1 治了读端（20 处读端改用 `context.turn_window`，
判据 `tests/test_turn_window.py` 规则 A–E），批 3（本 ADR）治输入契约本身。

## 决策

### 一、`server.py` 不动：臂 C 是"停止注入"的**逐字等价物**，不是生产路径

生产今天**没有** saver，去掉注入＝去掉全部跨轮记忆。所以这一刀属于**采纳**，不属于本批。
本批把它做成一条**评测臂**（`GOLDEN_ARM=checkpoint_delta`，臂 C）：臂 B 那层壳 + 一步输入
变换，进图之前把 `state["messages"]` 换成「`[System 块] + [当前问题]」——也就是
`_build_messages` **停止注入 `req.history[-20:]`** 之后逐字剩下的东西。采纳时删掉那段循环
即可，**不返工**。

恒等格（`turn_msgs <= 2` ⇒ 本轮本来就没注入历史 ⇒ 原样返回**同一个 state**）让
「臂 C 与臂 B 的差集」**恰好等于**注入过 history 的那几条用例——差集本身就是"这一刀切了
什么"的读数。两条守卫（`messages[0]` 必须是以 `[System: ` 开头的 `HumanMessage`、
`messages[-1]` 必须是当前问题）不成立就**响亮失败**，不"尽力尽力而为"。

### 二、三处加固：把 `_build_messages` 一直在**悄悄**做的事补回图里

"替代"不等于"删除"。丢掉注入历史的同时，必须把那份手写注入一直在提供的三样东西补回来，
否则这一刀会变成丢掉上下文：

| `_build_messages` 在做 | 不补回来会怎样 | 这里的对应物 |
|---|---|---|
| `req.history[-20:]` 封顶 | narrator 提示词随会话线性膨胀——`model_node` 那句 `with_tool_call_pairs(state["messages"])`（`graph.py:13177`）**无截断**，批 0 §10.7 记过、当时未修 | `context.narrator_messages(turns=10)` |
| assistant 侧过 `strip_fact_lines` | 上一轮的「〔系统〕 页面已跳转：…」变成"它自己说过的话"（生产实证 `20261001T230954`） | `strip_fact_messages` |
| 孤儿 user 剪枝 | 断流轮在图 state 里留下无回复的 user 消息，下一轮被当待答问题 | `prune_orphan_users` |

三条口径与批 1 的 `turn_window` **完全同源**：`turn_start <= 0`（没记边界／第一轮）⇒
返回**同一个对象**（判据要的是 `is`，不是"内容相同"）；只作用于 `turn_start` **之前**
那一段；`msgs[turn_start:]`（本轮）原样。所以**对臂 A 是逐字节零变化**，而且这条零变化是
**构造性**的，不是"应该没变"。

两个刻意的设计点：

- **切轮不切条数**：轮边界 = 每轮那条 `[System: …]` 注入块。条数切法会把开头的注入块挤出
  窗口、页面事实静默消失（批 1 已付过一次学费，`context.py:72-77` 有注）。
- **历史段必须丢 `ToolMessage`**：臂 A 的 `_build_messages` 只注入 user/assistant **文本**、
  从不注入工具帧，narrator 在臂 A 只看到本轮的帧。只按轮数切的话提示词仍按 K×单帧预算
  膨胀（单帧上限很大）。丢掉历史帧之后形状与臂 A 对齐且**真封顶**——合成探针实测：
  第 10 轮起窗口稳定在 19 条 / 约 1080 字符，而整表同期 28→34 条 / 12203→14886 字符。

### 三、`_prev_user_msg` 的修复：这是本批**唯一的**生产相邻缺陷

`context._prev_user_msg`（`context.py:555`）取的是"倒数第二条 `HumanMessage`"，**不跳 `[System: `**。
臂 C 第 2 轮的形状是 `[Sys1, Q1, AI1, 帧1…, Sys_cur, Q_cur]` ⇒ `[-2]` 取到的是**本轮的
System 块**，不是上一轮主人那句。它只在**确认兑现轮**用（`_intent_src`，`graph.py:5987`），
而确认兑现轮正是"不许重新理解一遍、只能靠原话"的那一轮 ⇒ 取错源 = 意图清单恒空。

最小改法：先取"非系统 Human"表，长度 ≥2 时用它，否则退回全 Human 表。**对臂 A 逐字节不变**
——臂 A 里 `Sys` 恒在表头，`real[-2] == humans[-2]`；只有"历史为空 `[Sys, Q]`"那一格走
退回分支（与今天同）。这一条用**旧实现的逐字转写**在 6 种形状上对拍过
（`tests/test_narrator_window.py` ④）。

### 四、判决性读数：主判据是**探针计数**，不是通过率

本批的命题是**结构性的**（"谁在持有上下文"），效应量是**计数级**的（消息条数、提示词字符
数），不是准确率级的方差 ⇒ 5 条可跑的多轮用例 + 8 条单轮对照 × 4 臂 × 2 遍交替就够，
PASS/FAIL 差集只作护栏。判读规则**先写下来**（免得事后按结果编解释）：

- **D vs A 的差集应当为空**（反控 D 只多写磁盘）——非空先查 saver 本身；
- **B vs A 的差**＝"复用线程 + 仍注入"的代价；
- **C vs B 的差**＝"停止注入"的收益，读数应恰好落在**注入过 history 的那几条**上；
- **C vs A 的差集应当为空**（除 D 已证明的噪声地板）——非空 ⇒ 这一刀真改了行为，
  逐条定性，**不许调参拉回去**。

## 读数（20261016：定点块 13 条 × 4 臂 × 2 遍交替 = 104 跑）

```
cd $MAIN && PYTHONPATH=$WT:/home/ubuntu/exp/pydeps \
  $MAIN/.venv/bin/python $WT/eval/experiments/ckpt_ab_driver.py \
  --arms graph,checkpoint,checkpoint_delta,checkpoint_random \
  --prod-history --tag prodhist1 --reps 2
```

定点块 = 5 条可跑的**多轮**用例 + 8 条**单轮对照**；逐用例、逐遍、逐臂跑之前都复位
`conv_<id>` 与 `golden_thread` 两处 thread（不复位就是假读数，驱动头注有记）。读数落在
`/home/ubuntu/exp/ckpt_ab/prodhist1/`（探针 jsonl 逐 (臂, 用例, 遍) 一个文件 + `ab_results.json`）。
**104 跑全部 `resets=0`、`fb=0`**（fallback 与重置一次都没发生）——这一批没有一次是靠
"打回重来"收尾的。

### 主判据：探针计数表

**① 恒等格——第 1 轮四臂逐格相同。** `mt3`（3 轮）第 1 轮：planner `msgs=2`、model `msgs=3`、
`提示词=13705` 字符，**四臂一字不差**（`task_state_resume_settle` 第 1 轮同理：2、4、12675）。
这就是"对臂 A 逐字节零变化"的端到端证据，而且它是**构造性**的（`turn_start <= 0` ⇒
返回同一个对象），不是"跑出来碰巧一样"。

**② 分叉只从第 2 轮开始**（下面取第 1 遍；第 2 遍同形）：

| `mt3_detour_then_return_ledger_value`（3 轮） | planner `msgs` | model `msgs` | 窗口没动手 | model 提示词字符 |
|---|---|---|---|---|
| **A** 现状 | 2,3 / 4,5 / **6** | 3 / 5 / 6 | T/T/T | 13705 / 12722 / 12851 |
| **B** saver+会话线程 | 2,3 / 8,9 / **16** | 3 / 9 / 16 | T/**F**/F | 13705 / 13159 / 13767 |
| **C** 停止注入 | 2,3 / 6,7 / **10** | 3 / 7 / 10 | T/F/F | 13705 / 12802 / 13066 |
| **D** saver+随机线程 | 与 B **逐格相同** | 同 | 同 | ≈B（采样差） |

| `task_state_resume_settle`（2 轮） | planner `msgs` | model `msgs` | 窗口没动手 | 提示词字符 |
|---|---|---|---|---|
| A | 2,3,4 / **4,5** | 4 / 5 | T/T | 12675 / 13501 |
| B | 2,3,4 / **9,10** | 4 / 10 | T/F | 12675 / 13733 |
| C | 2,3,4 / **7,8** | 4 / 8 | T/F | 12675 / 13609 |
| D | = B | = B | = B | ≈B |

**③ B 多出来的正好是"注入的那两段"**：第 2 轮 +2 条（上一轮那对问答）、第 3 轮 +6 条
= 上一轮**留在 state 里没清掉**的那份注入（2）+ 本轮**又注入一遍**（4）⇒ 每轮增量线性
（+4）、**累起来是二次**，正是 O(轮数²) 的前身。C 的差**恰好只有"注入段"这一项**，形状与
预测一致。（只有 3 轮、两个点，这里只说到"增量线性、累加二次"，不去拟合常数。）

**④ 但 3 轮尺度上字符只差 5%**（13767 vs 13066），条数却差 **60%**（16 vs 10）。原因很实在：
**帧的体积远大于那两句历史文本**。所以"注入的代价"在短会话里**看得见条数、看不见字符**；
字符级的二次膨胀要长会话才露头（合成探针第 12 轮 13.8×）。**只报字符数会低估这一刀。**

**⑤ `narr_ident` 量的是"闸门开没开"，不是"内容变没变"。** C 第 2 轮 `turn_msgs=2`，
`narrator_messages` 算出的窗口**内容等于整表**（只有 2 轮，K=10 没到），但它是**新列表**
⇒ `win is msgs` 为 False。所以"第 2 轮起 `narr_ident` 翻 False"读作"闸门开火了"，
不能读作"提示词被削了"。

### 护栏表：PASS/FAIL（两遍逐字）

| 用例 | A | B | C | D |
|---|---|---|---|---|
| `task_state_resume_settle` / `mt2_card_then_cancel_no_write` / `mt2_short_yes_resubmits_card` / `admin_tag_attach_stale_pick_out_of_range` | PP | PP | PP | PP |
| `mt3_detour_then_return_ledger_value` | **PF** | **FF** | PP | **FP** |
| `guestboard_talk_double_source`（单轮） | **PF** | **PF** | **FP** | PP |
| 其余 6 条单轮对照 | PP | PP | PP | PP |

### 先写下来的四条判读规则，逐条对账

1. **「D vs A 的差集应当为空」——证伪，而且不是 saver 的副作用。** 五条多轮用例上 D 的探针
   格子与 B **逐格相同**（planner 2,3/8,9/16、model 3/9/16）。根因是**反控的前提在这个驱动里
   不成立**：D 的语义是"照旧用原 thread"，而 golden 传的 `thread_id` 是**常量**
   `"golden_thread"`（`eval/run_golden.py:476`）⇒"照旧用原 thread"在一个用例内**就是复用**。
   D 头注写的「线程照旧每请求随机 ⇒ 只写不读」在**生产**成立（`server.py:1868` 每请求
   `uuid4`），**在 golden 里不成立**。⇒ 多轮块上 D 不是地板，是**第二个正向臂**
   （`ab1` 那张作废的表栽在同一件事上，驱动头注自己有记）。
   它仍给了一条正面读数：**D ≡ B ⇒ 效应来自"复用线程"这件事本身，与线程键是 `conv_<id>`
   还是那个常量无关。** "挂了 saver 本身不改变行为"这句话，本批是靠**8 条单轮对照**
   撑住的（4 臂探针格子相同），不是靠 D 在多轮块上撑住的。**要把 D 变回真地板，得让它
   每轮换一个线程**——本批没做（改了反控就要重跑这 104 跑），记在《明确不做》里。
2. **「B vs A 的差 = 复用线程 + 仍注入的代价」——成立**，读数见 ②③（第 2 轮 +2、第 3 轮 +6）。
3. **「C vs B 的差 = 停止注入的收益」——成立**，且 5 条多轮用例第 2 轮起 `narr_ident` 全部
   由 True 翻 False，`msgs` 差恒等于"注入段"。
4. **「C vs A 的差集应当为空」——在 PASS/FAIL 上成立（模下一条的噪声），在探针计数上
   不成立、而且不该成立。** C 第 3 轮 planner 看 10 条、A 看 6 条：C 多出来的 4 条是
   **前两轮的帧**（图自持），A 那 4 条是**注入的历史文本**。两者形状不同不是缺陷，
   是这一刀的实质——C 的闸门只保证**不重复注入**，不保证与 A 的计数相等（A 从来没有过
   跨轮帧）。**真正该逐格相同的是第 1 轮**，实测逐格相同（见 ①）。

### 噪声地板不在 D，在"两遍"

`mt3`：A=`[P,F]`、D=`[F,P]`（**反相关**）；`guestboard_talk_double_source` 是**单轮**、
四臂结构上是同一份代码路径（B/D 都退回那个常量线程、C 的变换是恒等），却 A=`[P,F]`、
B=`[P,F]`、C=`[F,P]`、D=`[P,P]`。⇒ 这两条用例的 PASS/FAIL **在同一臂的两遍之间就会翻**，
翻的方向还不一致 ⇒ ±1/2 遍的采样噪声，与臂无关。**结论：护栏表在 2 遍上读不出任何东西**；
这正是"主判据必须是探针计数"的事后验证。（要把它当护栏，得先量出这三条用例的翻面率。）

### 报告里必须标出的一格：`exec_memory_none_honest`

单轮、注入 `history` 2 条、**没有 `conversation_id`**——这是"单轮 + 有注入 + 无会话"那一格。
C 的变换在此**真的把唯一的那份跨轮上下文剥掉了**（planner `msgs` 4→2、`prompt_chars`
11909→11877），而它的断言**照旧全绿**。生产采纳后不会这样（线程即会话 ⇒ 历史在 state 里），
这一格是**语料的形状**造成的；但它同时暴露一条覆盖缺口：**"答得对不对"的断言看不见
"喂进去的上下文变少了"。** 全表口径：注入 `history` 的 **29 个轮次里只有 4 个**带
`conversation_id`；**多轮用例（10 条）一个都不注入**——这就是为什么非造
`GOLDEN_PROD_HISTORY` 不可。

### 这一批**没有**回答的问题

- **崩溃重放**：golden 的真写默认关（`needs_real_write` 的 5 条跳过），104 跑里没有一次
  真写 ⇒ 读数是**评测臂**的，不是生产路径的。采纳前必须过批 2（见《触发条件》）。
- **K=10 的正向收缩**：3 轮以内 K 根本不到 ⇒ 端到端只验了"不过度触发"这一侧。
- **写入放大**：每跑约 8–19 行 / 13–37 KB（`checkpoints` 一张表），会话越长越大——
  §4.7 ③ 那条代价清单里的第一项，本批只**记了数**，没有定价。

## 取舍

### 语料原本**测不出**这一刀（本批最重要的一条测量事实）

`eval/golden/basic.jsonl` 实测（192 条）：**10 条多轮用例一个轮次都不注入 `history`**
——跨轮靠 saver 线程 + 显式注入的 `pending_action`/`agent_tasks`/`executions`；注入
`history` 的 **29 个轮次**（**其中只有 4 个带 `conversation_id`**）都在单轮用例上，
线程是新的 ⇒ 图里没有那段历史，此时"丢掉注入历史"＝丢上下文。

⇒ 「臂 C 与臂 B 的差别」只出现在**线程非空 × 本轮仍注入 history** 这一格，而这一格在
今天的语料里**不存在**。不做处置的话，4 臂读数会显示 **C ≡ B**，而它会被读成"停止注入没用"。

处置 = 让语料变成**生产形状**：`GOLDEN_PROD_HISTORY=1` 时，多轮用例第 i>1 轮的 `history`
= 第 1..i−1 轮**真发出去**的（`user_input`，最终回复），与 Rust `history[-20:]` 逐字同形
（`src/routes/chat.rs`）。**四臂跑同一份驱动**，对照仍同源。

**这一格第一版写错了，错法很贵，值得记下来**：`_prod_hist` 攒的是**裸 dict**（它长得就像
Rust 发过来的那份 JSON），而 `req.history = [dict, …]` 这种赋值 **pydantic 不校验**
（`ChatRequest` 没开 `validate_assignment`，`list[HistoryItem]` 的强制转换只发生在**构造时**）
⇒ `_build_messages` 拿到裸 dict，`h.role` 当场 `AttributeError`。**症状与病因完全不成比例**：
异常在**主线程**抛出、traceback 也打出来了，但进程**不退出**——挂满 15 分钟被 `subprocess`
的超时杀掉，看起来像"服务商把连接挂了"（当时真的去查了 socket 状态，那条 CLOSE-WAIT 是
无关的旧连接）。而臂 B 在批 0 跑过同一条用例 3/3、每次约 9 秒 ⇒ 差异只在"这一格开没开"。
判据现在是 `tests/test_golden_prod_history.py`：把 `run_case` 真驱动起来（`run_one` 换桩），
逐轮问"注入进去的每一条读得出 `.role`/`.content` 吗"，最后用**真的** `_build_messages`
走一遍。**锁的落点是判据不是字面**——它不检查源码里有没有 `HistoryItem(`，它检查历史读不读得动。

### `_url_trusted` 的**有意放宽**（记录，不改）

`graph.py:13298` 的 `_url_trusted(u, messages)` 把**整份**消息表里出现过的 URL 当可信。
线程复用后它的可信集合从"20 条注入历史 + 本轮"变成"整个会话"。方向是**收紧闸门**（假 URL
更难被判出来）——但被判的对象是"模型编的 URL"，而**从站内工具返回里真出现过的 URL 就不是
编的**，所以这是**误伤减少**，不是洞。同族的 `_doc_anchors`（`context.py:1110`）由近及远 +
`limit=6`/`budget=700` 封顶，多出来的历史只增加候选、不改最近 6 条 ⇒ 同样只记录。
**明写在这里，免得下一轮 review 当漏改。**

### 窗口深度 K=10 是个**可调档**，去留由读数定

选 10 的依据是"同一量级"：臂 A 注入 `req.history[-20:]` ≈ 10 轮问答，`context._RECALL_TURNS
= 10` 是 planner 侧能回看的最深轮数。⚠️ **golden 的多轮用例最多 3 轮** ⇒ K=10 下窗口在
真链路里**永不触发**，端到端只能验"不过度触发"这一侧；正向收缩必须另造合成探针
（`checkpoint_probe.py --narrator-window`，12 轮真 saver）。**没有那个合成探针，"封顶"这条
声明在本批的读数里是零证据。**

### `model_node` 的行数账：本批 +4，台账**没有**放宽

`eval/complexity_baseline.json` 记 `model_node {c:78, lines:400}`，而
`tests/test_complexity_budget.py` 的 `LINES_MAX=300`——**这条在批 0 就红了**（实际 152 行
时基线写 400，两边不同源）。本批把 `model_node` 从 160 行推到 **164 行**（+4：两行调用 +
两行探针字段）。原计划写的是"净零行"，**没做到**。选择是：把那次调用压成一行 walrus
（-1 行，但会让 AST 锁把 `NamedExpr` 当成"不是直接调用"而误判，且更难读），或者老实记下 +4。
**取了后者，且没有放宽 `LINES_MAX`、没有改基线**——把一条已经红的账再推高 4 行不好看，
但"为了让数字好看去调门槛"是本仓反复禁止的那件事。

## 复验

- `tests/test_narrator_window.py`：闸门恒等（`is`）、臂 A 形状逐元素 `is`、
  合成脏清单（事实行 + 孤儿 + 超 K 轮）**必须真变**、`_prev_user_msg` 六形态对拍旧实现、
  三条反向对照（`k < 0` / 剪枝扩到全表 / `turns=0`）、剥事实行的幂等。
- `tests/test_turn_window.py` 规则 F：`with_tool_call_pairs` 在 `model_node` 里**恰好出现一次**，
  其实参必须是 `_narrator_msgs(...)` 的直接调用或由它赋值的名字，**不许**是裸的 messages 读；
  `_narrator_msgs` 必须委派给 `narrator_messages` 且必须传 `turn_start`。反向对照：
  `with_tool_call_pairs(state['messages'])` ⇒ 红。
- `eval/experiments/checkpoint_probe.py --narrator-window`：12 轮真 saver，闸门开 ⇒ 第 10 轮起
  条数与字符数封顶；闸门关 ⇒ **逐轮线性膨胀**（末轮 14886 vs 窗口臂 1079，13.8×）。
- `tests/test_golden_prod_history.py`：注入的历史读得动；封顶 20 条；关档时一条都不注入。

## 明确不做（本批）

- **不做写幂等账**（批 2）——采纳的硬先决条件，不是读数的（见《触发条件》）。
- **不改 `server.py`**（注入循环、`thread_id`）——那是采纳。
- **不把反控 D 改成"每轮换一个线程"**——见《读数》第 1 条：D 在多轮块上其实是个正向臂。
  改它就要重跑这 104 跑，本批不做。
- **不动 `with_tool_call_pairs` / `strict_wire_issues` / `_frame_id`**——出站序列必须是
  整份 `state["messages"]`，窗口化会让帧 id 撞车 ⇒ 服务商 400（批 1 已写明）。
- **不改提示词字面、不动 `MAX_PLAN_ROUNDS`/`BUDGET`、不碰产线。**

## 触发条件（本 ADR 是"已搁置"的唯一原因）

**批 2（写操作幂等账）落地之前，不许真开 saver。** 批 0 §2.6 实测：恢复单位是
**superstep**，崩在节点中途 ⇒ 整节点重跑 ⇒ 写操作执行两次。而幂等键**不能**住在 `state`
里——节点内的 state 更新要等节点**返回**才落盘，恢复时那个字段根本没写下去。它必须落在
**节点之外**的一本账上（候选：LangGraph 自己的 `BaseStore`/`PostgresStore.setup()`，
自建表 ⇒ 不需要产线迁移）。

采纳是**三处一起动**：`server._build_messages` 删注入循环、`thread_id` 改会话坐标
（= `conversation_id`，会话删除要级联清 checkpoint）、`build_graph(checkpointer=…)` 接上
产线 saver。⚠️ **本批的读数不能当作"采纳安全了"**：golden 的真写默认关（`needs_real_write`
的 5 条被跳过）⇒ 崩溃重放那一格在本批的读数里**不存在**，臂 C 是**评测臂**不是生产路径。

### 20261016 追记：这条触发条件**已满足**

批 2（写操作的幂等账）已落地，见
[adr-0012-write-idempotency-ledger.md](adr-0012-write-idempotency-ledger.md)：键 =
`sha256([checkpoint_ns, 工具名, 参数指纹])`、at-most-once（先占位 → 调用 → 记结局）、
覆盖集从 `authz` 派生。它同时量出两条此前没写明的前提——① 节点 config 里
`configurable["checkpoint_id"]` **恒为 `None`**（langgraph 无条件写死），superstep 身份
只能去 `__pregel_task_id` 拿（而 `checkpoint_ns` 就是 `f"{节点名}:{task_id}"`）；
② 图必须用 `durability="sync"` 跑，缺省 `async` 下**硬杀**会让恢复的 task_id 变、账被
**静默绕过**（且只在崩溃那一刻失效）——这条自断言已搬进 `agent/idem.py`。

⇒ **采纳现在只差"三处一起动"与真机读数**：长会话 K=10 的正向收缩、崩溃重放对**产线等效**
saver、同会话并发两轮；外加 `pyproject.toml` / `uv.lock` 的依赖。**前置满足不等于采纳完成**
——本 ADR 的状态在那一步落地之前仍是"已搁置"。

### 20261018 追记：采纳落地（`docs/platform-roadmap.md` 批 7）

**三处一起动，一次原子提交**。落点与本文《触发条件》写的一处不同，记在这里：

| 本文原本写的 | 实际落地 | 为什么 |
|---|---|---|
| `build_graph(checkpointer=…)` | `server._build_arm()` 的 `create_agent(checkpointer=checkpoint.build_checkpointer())` | 生产调用点是 `create_agent`，`build_graph` 只**转发**（`tests/test_checkpoint_factory.py` ⑤ 锁着"只传不造"） |
| `_build_messages` **删**注入循环 | 给它**套闸门**（`if not _graph_holds_context():`） | 删掉的话，**没挂 saver 的那条路**（DSN 空时的生产、以及没挂 saver 的评测臂）会失去全部跨轮上下文——比"多注入一份"更坏，且同样不报错 |
| `thread_id` 改会话坐标 | 同上，但**两条路共用** `server._thread_id_for` | 只改一条会让流式/非流式两种线程语义并存，而线上主路径是流式那条，单跑评测看不见 |
| （未写） | 显式 `durability="sync"`，且**有 saver 才给** | `durability` 是 `stream()` 的参数、不是 `compile()` 的参数（langgraph 1.2.7 实测）；没 saver 时给了会招一条 UserWarning，而 `ReactGoldenArm.stream()` 压根没有这个形参 ⇒ `TypeError` 炸在生产者里 |

**"三处一致"是结构性的，不是约定**：三处共用**同一个判据** `server._graph_holds_context()`
——它读的是**活体对象上有没有 saver**（`agent/probe.py`，与 `/health` 的
`checkpoint_backend` 同一个取值实现），不是 `CHECKPOINT_DSN`。于是"配了 DSN 但没挂上"与
"没配 DSN"在源码形状上是同一个形态，**半接在结构上不可能**；
`tests/test_checkpoint_adoption.py` 判的就是"四处形态一致"，四条反控各拨一处全部翻红。

**建表加了一把咨询锁**：`saver.setup()` 幂等但**不并发安全**（每条迁移末尾
`INSERT INTO checkpoint_migrations (v)`，**没有 `ON CONFLICT`**），而产线是 `--workers 4`
同时启动 ⇒ 后到的那个主键冲突、启动崩。所以建表包在 `pg_advisory_lock` 里。
⚠️ **这一版是错的，同日上线当天造成了一次真停服**（阻塞式锁 + 迁移里的
`CREATE INDEX CONCURRENTLY` 成环）⇒ 已改成 `pg_try_advisory_lock` 轮询，见文末
《20261018 追记（二）》。上面那句"包在 `pg_advisory_lock` 里"**保留原文**，因为它是当时
的决策原文。

**级联清理是本 ADR 与父仓的跨仓契约**：删除动作在父仓，本仓出
`DELETE /checkpoint/{conversation_id}` 钩子。
⚠️ **父仓那一半截至 20261018 还没接上**——当时本仓的钩子没有任何调用方，是已知缺口。
**20261019 批 D 接上了**：父仓 `conversation.rs::drop_checkpoint_threads` 在
`delete_conversation`（`commit()` **之后**——HTTP 不能进事务）与 `delete_temp_user`
两个调用点各调一次，会话 id 由 `purge_conversation_data` **返回**（清单仍只有一处）。
判据 `tests/test_checkpoint_cascade.py` ③ 随之把锚点从"清单的两个 match 臂"挪到"两个调用点
的函数体"：老锚点在新形状下恒判「两臂都没接」，而**一条恒绿的判据比没有更坏**。

**还没做（不在本批）**：真机读数三条（长会话 K=10 正向收缩、崩溃重放对产线等效 saver、
同会话并发两轮）。本批只保证"接得上、接得对、半接会红"。

### 20261018 追记（二）：建表锁**造成了一次真停服**，改成 try-lock

上面《20261018 追记》里那句"建表包在 `pg_advisory_lock` 里"是**错的**，同日上线当天就
炸了。这一段记的是**机制**与**为什么当时四层判据全绿**。

**症状**：4 个 worker 无限期卡死、**一条日志都没有**（`Agent ready` 那行永远打不出来，
8010 因此根本不监听），`systemctl restart` 也卡在"等旧 worker 退出"上，要等满
`TimeoutStopSec=120` 才被 SIGKILL。

**机制**（`pg_stat_activity` 实测，成环的三方都在场）：

```
1 条 idle  持有 advisory 锁
3 条 active 卡在 Lock/advisory          ← pg_advisory_lock，一条挂着的语句 = 一条没结束的事务
1 条 active 卡在 Lock/virtualxid 跑 CREATE INDEX CONCURRENTLY
```

持锁者在跑 CIC，而 CIC 的语义要求**等所有并发事务收尾**（`WaitForOlderSnapshots`）；
那三条事务在等它放锁；而它要等 CIC 结束才放锁 ⇒ 环。**这不是"慢"，是永不等到的等**。

**修法**：`pg_try_advisory_lock` 轮询 + 总预算（60s，超了响亮抛而不是无限等）。它断环的
理由是**每次尝试都是一条已提交、已结束的语句**，两次尝试之间**不持有任何事务**——等锁者
拿到的快照比 CIC 的新，`WaitForOlderSnapshots` 不认它（同一条推理也解释了现场那条 `idle`
的持锁会话为什么没参与环：idle 的 autocommit 会话 `xmin` 已清零）。**依赖池是
`autocommit` 的**，所以 `_setup_under_lock` 里加了一条显式断言。

**顺带修掉的第二个洞**：`CREATE INDEX CONCURRENTLY IF NOT EXISTS` 会**永远跳过**一个
被打断留下的**无效索引**（`indisvalid=false`）⇒ 那张表**永远少一个索引**，而没有任何东西
会红（被打断的那次正是这副样子：迁移停在 `max(v)=5`、`checkpoints_thread_id_idx` 无效）。
现在建表前先 `drop_invalid_indexes()`。

**为什么四层判据全绿**：前三层（含那两条"连不上要响亮抛"）**全在 `127.0.0.1:1` 这种必然
失败的 DSN 上判**——"真连上一个库、真跑一次迁移、4 个进程真的同时来"这条路径**从来没有被
跑过**。补的第四层是 `tests/test_checkpoint_setup_concurrent.py`（真连库、4 进程同时建表），
它的红基线是**把建表换回阻塞版**：同一套脚手架下必须卡住、且**迁移跑不完**（实测
`max(v)=5 < 9`），真实现下 4/4 成功。**"前提不可用"与"前提不可用所以没量"是两件事**——
这一层缺库时报「未评估」，`tests/run_all.py`（已删）的汇总里每次运行都另起一行说清这次量没量。

⚠️ 同一次补网还撞出一件事，与这个死锁**同源**：**杀掉客户端进程 ≠ PG 那边停了**。红基线
把 4 个子进程 SIGKILL 掉之后，它们的 PG 后端不会跟着退出——跑着 CIC 的那个继续拿着表上的
`ShareUpdateExclusiveLock`，卡在 `pg_advisory_lock` 上的那个会在持锁者会话终于消失之后
**接着把迁移跑完**，于是两个 CIC 后端互等表锁、下一相全卡住。所以每一相之间要
`pg_terminate_backend` 掉这类僵尸会话再往下走。

### 20261019 追记（三）：`CHECKPOINT_DSN` **必须指业务库之外的独立库**

采纳那天的运维口径写的是"生产候选 = 业务库、**不建议**再另起一个专库（多一个库就多一份
运维面）"。**这条取舍作废**，理由与"少一个库"无关：

`build_checkpointer()` 会在 DSN 指的那个库里建 4 张本 agent 自持的表，而建表走的是
`PostgresSaver.setup()`——**它没有编号迁移**。业务库的结构只许由父仓的**编号迁移**改
（父仓的迁移校验脚本 `verify_parity.py` ⓪ 段判的正是"业务库里不该有 checkpoint 表"）
⇒ 指进业务库，等于在业务库上开一条**没人编号、没人 review、也不在 CI 里**的 DDL 通道；
而那 4 张表与业务表**不重名**，所以**不会报错**——这正是它难发现的地方。

实测（20261019）：`.env` 里那一行当时已填上、`/health` 回 `checkpoint_backend=postgres`，
而它指的正是业务库 ⇒ `setup()` 在每次启动时把表**幂等地建回来**，父仓 ⓪ 因此红着。
本批把口径反转、并把"指错库"做成可读与可拦的两件东西：

  · `/health` 的 `dials.checkpoint_db`（**只回库名**，不回主机/口令）——它是这一块里
    **唯一一格读配置的**，与 `checkpoint_backend`（读活体对象）合起来才读得通；
  · `CHECKPOINT_FORBID_DB`（**缺省空 = 不拦**）：填业务库的库名 ⇒ DSN 指到它时
    `build_checkpointer()` 当场抛、**进程起不来**。**生产库名不写进本仓**（公开仓，
    运维坐标不落仓），所以硬闸只能是个配置项。

**旧数据不迁**：checkpoint 是派生上下文，换库最坏退化成"老会话第一轮看不见历史"
（`docs/platform-roadmap.md` 批 7 里记着的那条已知缺口），而迁的代价是两套库坐标
从此要一起维护。
