# ReAct 试验线：原生 tool calls 当骨架，补上「什么时候停」与「谁来验收」

> **状态（20261012 复核）**：不再是"分支 `exp/react-native-line`"那种形态——那条分支是
> `main` 的**祖先**（落后 196 个提交、无一条独有提交，是**陈旧的指针**），早已作废；
> 两条实现（`agent/react_line.py` 的循环与 `agent/react_arm.py` 的生产帧契约适配器）
> 都**在 `main` 上**，本轮的适配在 `feat/react-arm-adapt`。
> **出厂仍不在生产路径上**：`AGENT_ARM` 的真值源是 `.env`、默认 `graph` ⇒ 线上跑的还是
> 原来那条手写图、**逐字节不变**；`react` 那一档 20261012 才接进 `server.py`（拨它要重启
> 服务，见 §四 的现状表）。入口三处：`GOLDEN_ARM=react`（评测侧选臂，`eval/golden_arm.py`）、
> `AGENT_ARM=react`（生产侧选臂，`server._build_arm`）、两个套件。
> 它是一次**可复现的对照实验**，外加三个真写下来的机制（收敛判据 / 工具级回执 /
> 独立 narrator）。读完这篇要知道：它证了什么、没证什么、真要做成产品还差哪几件，
> 以及——**这一族读数什么时候才配被引用**（§六，本轮最值钱的一条）。
> §六 末节是 **20261012 那次全量双臂对比的读数**（两臂各 180 条、同分母）。
>
> **20261016 追加（帧契约收口，`docs/adr/adr-0008-*.md`）**：三条 react 分支又核了一遍
> ——`exp/react-native-line` / `feat/react-mainline` / `feat/react-arm-adapt` **全是 `main`
> 的祖先**（落后 262/249/56，零独有提交），`main` 上那份最新。裁决两件：**臂留在主线当
> 开关**（不拆仓），以及**帧契约收成 `agent/frames.py` + 机械锁
> `tests/test_frame_contract.py`**。同时修掉一条真分歧：`react_line._consent_missing` 停在
> 生产 20261012 **收窄之前**的 `not self.grant`，而 `tests/test_react_arm.py` ⑥d 把那行
> 字符串钉住了 ⇒ 漂移被永久化；现在直调生产的 `_grant_covers`，并把 `executed` 那一格
> 补上（此前**根本没喂**，那条判据在臂上是恒不生效的）。详见 ADR-0008 与下表末行。

## 一、它要回答的问题

20261004 那次定点 A/B（三臂、同模型、同解码参数、写工具全是桩 ⇒ 零真实写）量出来
（**同样是「P6 之前」的读数**；而且这个探针本身在当时还带着三处测量偏差，
20261011 才修掉——见 §四 现状表最后一条）：

| 臂 | 命中族（48 句） | 撞 `recursion_limit` | LLM 轮数 | 耗时 |
|---|---|---|---|---|
| A 现网手写图（planner 决策 + execute 执行 + checker 验收） | **31/48** | 0/48 | 54 | 83s |
| B′ 香草自由 ReAct（65 个裸工具 ＋ 12 行提示词） | 31/48 | 4/48 (8%) | 131 | 277s |
| C′ 装资产的自由 ReAct（45 个技能级 schema ＋ planner 当轮真提示词） | 34/48 | **12/48 (25%)** | 164 | 258s |

三条读出来的东西：

1. **命中率不是结构的差值，是资产的差值**——B′ 与现网打平，装满资产才 +3/48（仍在
   噪声量级）；而"ReAct 贵 17×、33% 收不住"那个旧结论，大半是**解码参数没对齐**造成的
   （ReAct 臂裸用 `get_llm()` 时 thinking 是开的）。同解码后只贵 ~3×。
2. **结构给出的差值是两件，而且都不在"会不会调工具"上**：自由循环**没有确定性收尾**
   （用户拿到 `GraphRecursionError` 而不是一句回答），以及**没有验收与台账**（工具结果
   只是消息流里的一串字符，"办成了吗"只能靠回复措辞猜）。
3. 三句指代不明（「把它号封吧」「给他驳回请求」「两个都通过吧」）**三臂全 0/3**——
   失败点在指代解析，不在工具调用机制。**这一条这条线治不了**，也不该指望它治。

所以本分支只做两件：**收敛判据**与**工具级回执**。命中率不是它的靶子。

## 二、两件补齐

### 1. 收敛判据（`ConvergenceMiddleware`）

生产侧的对应物是 `graph.py` 的 `MAX_PLAN_ROUNDS` + `_wrap_up_plan`：轮次用完**不是抛错**，
是产一条诚实的收尾。这里把同一件事做进 `create_agent` 的中间件钩子，两个判据**挂在不同
钩子上**，因为它们的对象不同：

| 判据 | 钩子 | 为什么挂这儿 |
|---|---|---|
| 轮次预算（`budget`） | `before_model` | 判在"下一次模型调用之前" ⇒ **撞预算的那一轮照样执行**，停的是"再来一轮"。挂在 `after_model` 会把模型已经点名的调用整个吞掉——用户等了半天、动作没发生，那是另一个语义。 |
| 无进展（同一 `(技能, 参数)` 签名重复） | `after_model` | 判据的对象是"模型**刚提出**的这一轮"；等下一轮开始时最后一条已经是 ToolMessage，看不出来它想点什么。跳 `end` 时那次重复**不执行**：同样的调用已进台账，再跑一遍拿的是同样的结果（生产 `_already_done_writes` / `_trim_done_reads` 提前拦的也是这一格）。 |

两条细节是**试跑量出来才定下的**，别当成随手写的：

- **纯应答技能（chat）不算"进展"**：第一版把每次技能调用都记进签名表，于是"模型先说一句、
  下一步就去调工具"的正常节奏被误判成卡住，整轮停在闲聊上（试跑里那句复合句
  「牛牛冻结账号给日程4标记为未完成」就是这么停的）。现在只有**真执行了工具**的调用才进
  无进展判据；次数限制交给轮次预算管。
- **图级保险丝要给得宽**：每个中间件钩子都是**独立超步**（`before_model` / `after_model`
  各一个节点），一轮 ≈ 4 个超步而不是 2。按 `2×budget+2` 设保险丝会让中间件在被判停之前
  先被图掐死——看板上显示成"中间件没生效"，其实是被保险丝动了手（20261004 试跑实录）。

### 2. 工具级回执（`SkillExecutor`）

生产侧 `execute` 的验收链是：技能 → `instantiate_plan` 展开成计划 → 逐 spec 执行 →
`_check_spec` 判 PASS/BLOCK → PASS 的进 `receipts`（系统确认的事实）、BLOCK 的进 `blocked`
（带原因码）。本线照同一条链走，**判据仍是那一份 `_check_spec`**（不抄第二份）：

```
模型的技能调用 → instantiate_plan(技能, 参数) → 逐 spec: resolve_args → 执行 → _check_spec
                                                        ↓
                                       receipts / blocked（本轮的台账）
                                                        ↓
                        一行回执行附在工具结果末尾还给模型（"已执行" / "未生效（原因码），别原样重试"）
```

三个刻意的选择：

- **技能名不能直接喂给 checker**：`_TOOL_MAP` 里没有 `content_query` 这种东西，直接判会
  一律 `unknown_tool`——那不是判据，是口径错配。所以必须展开成真工具名。
- **台账分三本**：`receipts`（PASS，系统确认的事实）、`blocked`（BLOCK，带原因码）、
  `replies`（纯应答技能，**没有执行任何工具**）。第一版把纯应答也塞进 `receipts`，收尾正文
  里就印出了「确实办成 1 件（）」——"没执行"混进"事实"里，是本仓栽过的
  「剔空与收尾轮长得一样」的同一类形状。
- **缺工具要响亮**：计划里的工具不在可执行清单里 ⇒ 产错误帧判 BLOCK，**不静默跳过**。
  跳过会让"计划 3 步只成了 2 步"看起来像全成，而那正是回执要防的事。

## 三、结果

> ⚠️ **本节两张表都是「P6 之前」的读数**（P6 = 20261012 收尾改由独立 narrator 写，见 §四
> 现状表第 5 条）。它们记录的是那一版实现量出来的东西，**一个字都不改**——但要标明：那一版
> 的收尾是决策模型自己写的，而闸门判的就是那一段（§四 第 4 条当时也还没接），所以它与本轮
> 之后的**任何**读数都不可比，**不许混进同一张表**。表里的 48/48 是当场口径，不是"今天的
> 基线"。

20261004 跑的一遍（qwen3.8-flash，三臂同解码 thinking 关 / temp 0.2 / 1200 tokens，
16 句 × 3 轮 = 48 次，写工具全是桩 ⇒ **零真实写**；三臂的图级保险丝都是
`recursion_limit=24`，`react_line` 另有自己的 `budget=6`）：

| 臂 | 命中族 | 撞上限 | 模型轮数 | 48 次耗时 |
|---|---|---|---|---|
| A 现网手写图 | 31/48 | 0/48 | 53 | 127s |
| C′ 装资产的自由 ReAct | **35/48** | 5/48 (10%) | 225 | 350s |
| **D 本线（+ 收敛 + 回执）** | 33/48 | **0/48** | 149 | 259s |

读出来的：

1. **收敛判据有效，而且不是靠"硬砍"换的**：C′ 在同一个保险丝下仍有 5/48 撞死
   （用户拿到的是异常而不是回答），D 是 **0/48**——48 次里 43 次自然收尾、5 次由
   `no_progress` 收尾，收尾语是确定性中文且带台账。轮数也从 225 降到 149（早停省下的）。
2. **准确率仍是三点打平**：31 / 35 / 33，量级都在 ±4/48 的噪声里（逐句看，D 相对 A
   只赢一句、输一句）。**没有任何一条证据支持"换成 ReAct 骨架更准"**——与上一轮
   A/B 的结论一致：命中率由**资产**决定，不由循环形状决定。
3. **代价仍在**：D 的模型轮数是现网的 ~2.8×、耗时 ~2×。它换来的只有"零异常收尾"
   与一份 74 条的工具台账（48 次里 0 条 BLOCK——桩工具几乎全 PASS，所以这次跑**没有
   真正考验**回执的 BLOCK 分支；那条路只有 `tests/test_react_line.py` 的 ⑩b/⑩d 覆盖）。
4. 三句指代不明**仍然 0**（「把它号封吧」「给他驳回请求」三臂全 0；「两个都通过吧」
   C′ 3/3、D 2/3、A 0/3——那句的差别在采样，不在结构）。**指代解析不在这条线的射程内。**

## 四、已知缺口（真上线必须先补的）

这条线**不是"把 graph.py 换掉"就完事**的替代品。它今天缺的，按重要性排：

1. **台账是"一次运行一个实例"**（`RunLedger` 活在 Python 对象里，不是 graph state）。
   常驻服务里必须换成按 langgraph `run_id` 分桶，否则并发会话会串账。
2. **没有权限判据**：生产 `execute` 在调用之前有 `authz.check(principal, name)`（含
   `_HARD_SCOPES` 硬拦与 `AGENT_AUTHZ_ENFORCE` 两个档），本线的展开链**一步都没接**。
3. **没有确认卡（consent）与写目标有据判据**：生产那两道闸（`consent_missing` /
   `target_missing`）就挂在 execute 的同一层，本线没有对应物 ⇒ 写操作会裸奔。
4. **没有 gate**：回复里"我办成了"这类声称的判据全家都长在 `graph.py` 的 gate 节点上，
   本线跑完直接把模型最后一条消息当回复（收尾语是自己产的，但**自然收尾那一路没有 gate**）。
5. **没有跨轮执行记忆 / task / 实体摘要 / narrator / 事实块 / 流式帧**：都是 `graph.py` 与
   `server.py` 那一侧的既有资产。本线只证明了"决策与执行这两层可以换成 ReAct 骨架"，
   别的层没搬。
6. **指代解析（三句 0/3）与它无关**：那是注入的上下文够不够的问题，不是循环形状的问题。

一句话：**同分下换骨架不划算**——ReAct 骨架省下的是"手写图的代码量"，付出的是上面这六件，
而它们恰好是这几个月一线事故换来的东西。

### 现状（20261012 复核，逐条对上面那份清单；原叙述保留，只加状态）

上面那份清单是 20261004 写的，此后**每一件都被逐条处理过**——有闭合的、有实测发现
"本来就不是缺口"的、也有仍然欠着的。逐条：

| # | 现状 | 依据 |
|---|---|---|
| 1 | **闭合**。台账与内层 agent 都是 `stream()` 的局部量 ⇒ 每请求隔离，同一个臂对象可同时服务两轮 | 适配器提交 `37e05ef`（第二条臂按生产帧契约产帧）；判据 `tests/test_react_arm.py` ⑪ 两项流水交替推进、两本账互不可见。⚠️ 那条"必须换 `run_id` 分桶"的纪律**仍然成立**，但它管的是**将来把 `build_agent` 提成常驻单例**那一版，不是今天这一版（`agent/react_line.py` 的 `RunLedger` 注已改写） |
| 2 | **闭合**（含 `_HARD_SCOPES` 硬拦，判据字面与 `graph.execute_node` 逐字相同） | `37e05ef`「试验线接权限闸」；判据 `tests/test_react_arm.py` ③ |
| 3 | **闭合**（同意闸 + 写确认卡） | `da7af0f`（同意闸与写确认卡） |
| 4 | **闭合**。20261011 直调 `graph.gate_node`（判据一条都没抄第二份）；20261012 把**判的对象**也对齐了 | `86125dd`（接闸门谓词）、`c5376b0`（P6） |
| 5 | **大半闭合，`task_frames` 一件仍欠**。跨轮执行记忆与 `open_tasks` 已经读到（`[System: …]` 块 ⇒ `_page_ctx` ⇒ 提示词那一格，与生产 planner/model 同一通道）；narrator 20261012 拆成独立节点；事实块这一格的差**已经不是差**（`agent/factblock.py` 的 `BLOCK_FAMILIES` 是空集 ⇒ 生产两臂都停止代印，两边口径自动一致）；流式帧（`__EXEC__` / `__CMD__` / gate 帧）在 `37e05ef` 的适配器里就位。**只剩 `task_frames`（task_state 族）不发**——能力台账里这一族只有 1 条断言、且它的兄弟判据由生产那半的确定性结算满足 ⇒ 它**没有**变成 `comparable = False` 的来源，故排后 | `c5376b0`（P6 独立 narrator + 逐条核验 P5 两件不是缺口） |
| 6 | **不变**（仍不在射程内） | — |
| ＋ | **清单外补的一条**：收尾丢意图纠偏环（零工具收尾但主人那句话里还有一次都没规划过的动作 ⇒ 纠偏重决策一次） | `19b1601`。这是原清单没列、但生产那一侧早就有的判据 |
| ＋ | **清单外补的第二条**：离线定点探针的三处**测量偏差**（工具名折算扫子串、族级口径把方向相反的工具算成命中、两条 ReAct 臂逐字共用 planner 的提示词） | `d9d45ad`（20261011）。探针不进 CI，所以它那三处偏差由门禁里的 `tests/test_react_arm.py` ⑨ 看着 |
| ＋ | **清单外补的第三条（20261012 全量双臂实测抓出）**：本线的 **trace 词汇表**与生产不同源——`graph` 那个 run 目录每轮记 `planner.llm_done`/`native_decision`/`decision`/`execute.call`/`execute.check`，本线的 run 目录里**只有 `producer.*` 与 `gate.*`**（平均 7.4 帧/轮 vs 51.4）⇒ L3 跨源对账对本线近乎无料可读。**判据后果只兑现了一处、已修**：`planner.ledger_frame` 缺席让三条 `require_ledger_*` 断言**恒假**（假红 1 条，见 §六）⇒ `_system_prompt` 照生产记同款事件；其余节点事件的补记仍未做（今天不改变任何读数） | `tests/test_react_arm.py` ⑬（含负控） |
| ＋ | **清单外补的第四条（20261016）**：**帧契约此前只活在约定里**。`gate_node` 读哪些 state 键是 grep 数出来手抄的 ⇒ 实测少一格（`task_frames`，`_ledger_in_scope` 析取里那条腿）；`_popup_state` 那张四格的表少**七格**，其中 `executed` 是 `_grant_covers` "令牌用在那一件上就作废"那一格 ⇒ 臂的弹卡路径上**恒不生效**。`_popup_state` 的 docstring 自称"只读四个键"，实测**十一格**。⇒ 契约收进 `agent/frames.py`，由 `tests/test_frame_contract.py` 从 `graph.py`/`server.py` **现取**双向比对（多一个红、少一个红 + 四条反向对照） | `agent/frames.py`、`tests/test_frame_contract.py`、ADR-0008 |
| ＋ | **清单外补的第五条（20261016）**：第 3 条那条同意闸**与生产分家了**——`react_line._consent_missing` 停在 `not self.grant`（生产 20261012 收窄之前那版），而 `tests/test_react_arm.py` ⑥d 把那行**字符串**钉住了 ⇒ 锁越绿、臂离生产越远。现改为直调 `graph._grant_covers`，`SkillExecutor` 多收一个 `state`（`executed` 现算自 `executor.calls`）；⑥d 的落点改成"用的是生产那一份判据"，行为面由 ⑥b/⑥b′/⑥b″ 与 `test_frame_contract.py` ⑨e–⑨j 锁住。**语料没走到那条路**：20261016 拿 4 条 confirm 用例跑 react 臂（`--only`），逐字与 20261012 那份全量留档比对，**判定与正文全同**（1 红仍是既有那条 `mix2_two_writes_one_breath_card_only`，红的是 `__TASK__` 缺口，理由逐字相同），而**没有一条**走到"`executed` 非空时判弹卡"那一格（唯一贴近的 `board_audit_from_ledger_exec` 因真写默认关而 SKIP）⇒ 那条分歧靠 ⑥b″ 这个**定向用例**守着：摘掉 `popup_state` 的 `executed`，它当场红，其余 100+ 条断言不动 | `agent/react_line.py`、`tests/test_react_arm.py` ⑥b/⑥b′/⑥b″/⑥d |

**这六件之外还有一件事必须单列**：即便清单全绿，这份读数也只在 §六 那道闸（`arm_capability`
的 `comparable`）为真的时候才配被引用——20261011 之前它一直是 `False`，而那次全量读数
（0.9216）因此**作废**。

## 五、复现

```bash
cd /home/ubuntu/Saudade-Blog/saudade-blog-agent        # 就是仓根，不是另一个 worktree
.venv/bin/python tests/test_react_line.py              # 接线单测：秒级、零网络
.venv/bin/python tests/test_react_arm.py               # 生产帧契约 + 闸门/narrator/选臂/并发
.venv/bin/python tests/run_all.py                      # 全套（本文件所在仓的常规入口）
.venv/bin/python eval/experiments/react_line_ab.py \   # 定点 A/B：要网络、烧 token
    --rounds 3 --arms planner,react_assets,react_line --out /tmp/react_line_ab.json
.venv/bin/python eval/arm_capability.py --arm react --probe   # 读数许可闸（§六），离线
```

**陷阱（都在实测里撞到过）**：

- **`PYTHONPATH` 那条陷阱已经随之作废**：它针对的是"另开一个 worktree 跑实验、而
  `.venv` 的 editable `.pth` 把主仓钉在 `sys.path` 上"那种跑法——那会**静默**用到主仓
  那份代码，实验看起来"跑了"其实没跑分支。现在实现在仓根本体上，直接跑即可；
  **但那条性质本身没变**：哪天又用 worktree 跑对照，`PYTHONPATH` 还得指过去。
- 抓 planner 真提示词的办法**曾经**是 monkeypatch `agent.graph.bind_native`（包一层记录
  `.invoke(prompt)`）——20261011 起**两条 ReAct 臂不再用 planner 那份提示词**（那是拿一份
  它们结构上满足不了的契约考它们），各拿各的惯用提示词（`react_arm._system_prompt`）。
  要"一字不改地拿到当轮那份"这个纪律本身仍然有效（`eval/native_tools_probe.py` 首版
  栽过），只是对象换了。
- ReAct 臂必须 `agent.stream(..., stream_mode="values")` 收消息：`invoke` 撞上限抛错会把
  **整轮已发生的调用一起丢掉**，把"绕太多圈被掐死"误读成"没调工具"。
- 桩载荷**不许太贫**（只回一句"已执行"）：模型看不到内容会反复重调，撞上限率从 15%
  虚增到 33%。另外桩的返回值要带命令形状（`meta.cmd`），否则 `navigate_to` 那一批整批
  BLOCK（`cmd_shape`）——回执行还在、`receipts` 却是空的，"办成几件"会读成 0。
- 判工具名**不能读** `native_decision.calls`（那里只有技能名，读族的工具全在 PARAMS 里）——
  现网臂要读计划文本的 TOOLS 行；ReAct 臂的技能要按 `SKILL→plan 模板` **结构化**折算
  （`Skill.plan`），**不许扫子串**：`account_unfreeze` 的模板里只出现 `unfreeze_account`，
  扫子串会把 `freeze_account` 一起白送（方向还正好相反）。三种口径统一折算，否则比的是
  三种不同的问题。
- 三臂**同解码**（thinking 关 / temp 0.2 / 1200 tokens）。ReAct 臂裸用 `get_llm()` 会让
  thinking 默认开着，比出来的是解码参数不是结构。

## 六、这一族读数什么时候才可读：`arm_capability` 的 `comparable`

**结论先行**：这条臂的 `pass_rate` / `--ab` **只在 `arm_capability --arm react` 给出
`comparable = True` 时才配被引用**；`False` 时它是一条"看着正常"的假读数，
**不许引用、不许拿去和 graph 臂比**。这一节是那条闸的说明书——它比本篇其它任何一节都值钱，
因为**它是唯一一条会否掉其它所有数字的判据**。

**病在哪**：语料里有一族断言是**纯否定**的——"这一轮**没有**发命令""**没有**弹确认卡"
"**没有**调这个工具"（`forbid_cmd_prefixes` / `forbid_cmd_contains` / `forbid_frame_prefix` /
`forbid_exec_tools` / `forbid_task_state` / `forbid_fallback`）。一条**压根不具备那种能力**
的臂，会让这一族**整族恒真**：不是"它做对了"，是"它做不到，所以判据问不出来"。
20261005 那次全量读数就是现场：372 个可证伪键实例里 **138 条落在 `forbid_fallback`**，
而那一版的臂**结构上不会发 fallback**（收尾那一路上没有 gate）⇒ 138 条白送，
`pass_rate = 0.9216` 因此作废。

**闸怎么判**（`eval/arm_capability.py`）：逐族问"**这族被武装过吗**"——语料里有**正向兄弟**
的（`require_cmd_prefixes` ↔ `forbid_cmd_prefixes`），看兄弟那侧在归档里见没见到；
没有兄弟的（`forbid_fallback` 是唯一一族，语料里不存在 `require_fallback`）**只能靠实证或
合成正控**：

```bash
.venv/bin/python eval/arm_capability.py --arm react --probe
```

`--probe` 会**强制这条臂发一次兜底**，确认 `forbid_fallback` **真的会变红**——正控变红
才是"判据接到底了"的证书；正控**也变绿**说明判据根本没接上（那才是坏事）。判定结果
落在报告的 `comparable` 与 `comparable_ids` 两格。

**消费方式**：`comparable = False` 时**首选**是拿它给出的那份 `comparable_ids` 做**同一个
`--skip-ids`**，把两臂的分母收窄到"两边都真的可证伪"的那些用例上再对比
`pass_rate` 与 `pass_rate_ci95`。**绝不许"子集跑 vs 全量跑"**——那是拿两种分母做差。
（`--print-skip-ids` 取那份名单。）

**本轮的结果**（20261012，`--arm react --probe`）：七族全部 `✅ armed`，
`可证伪键实例 372/372 = 1.0`、**`comparable = True`**、不可证伪 0 条 ⇒ 这一次读数配被引用。
两条把状态推到这里的提交：`86125dd`（把闸门谓词接上，`forbid_fallback` 从此会开火）、
`c5376b0`（把**判的对象**也对齐——闸门判的必须是主人真会读到的那一段，即零工具 narrator
写的正文，而不是手上有工具的决策模型的回忆）。

**这道闸只判了一半（20261012 补）**：上面七族全是**纯否定**族——病是"做不到 ⇒ 恒真"（假绿）。
镜像方向同样活在语料里，而这道闸**不看**：要求型断言（`require_ledger_frame` /
`require_ledger_rows` / `require_card_targets_from_ledger`）读的是 `planner.ledger_frame`
这个 **trace 事件**，一条不产那条事件的臂会让它们**恒假**（假红）。现场就是本轮
`admin_board_audit_reviewed_refusal`：react 臂报红，理由逐字是「本轮没有
planner.ledger_frame 事件 —— 待办台账**没摆上桌**」，而两臂那一轮的行为**逐字相同**
（回复正文一字不差、确认卡载荷的 `skill`/`tool`/`args` 三项全同）——红的是证据通道，
不是行为。**修法是把通道补上**（`_system_prompt` 照 `graph.py` 的 planner 那一处记同款
事件；判据 `tests/test_react_arm.py` ⑬，含"空帧不记"的负控），**不是**再加一层闸——
闸管的是"读数配不配被读"，而这条的根因是臂没说话，不是语料设了个它够不着的门槛。
留一条纪律给下一次：**这道闸给出的 `comparable = True` 不等于"语料里的每条断言都判得动"**，
它只覆盖"纯否定族会不会白送"。

### 那一步已经跑了：20261012 全量双臂（本节的读数）

跑法：`GOLDEN_ARM=graph` → `eval/report/runs/20261012_011722_469.json`；
`GOLDEN_ARM=react` → `eval/report/runs_react/20261012_014919_934.json`。串行、00:52–01:49、
避开 04:00 夜间回归；`last_run.json` 只有 graph 覆写。两臂**各自先过了本节那道闸**
（都是 372/372 = 1.0、`comparable = True`、不可证伪 0 条 ⇒ `--skip-ids` 两边都空）
⇒ **同 180 条、同 8 条 skip**，不存在"子集跑 vs 全量跑"。

**① 准确率**

| | graph | react |
|---|---|---|
| 通过 | **173/180 = 0.9611**（Wilson 0.922–0.981） | **164/180 = 0.9111**（Wilson 0.861–0.945） |
| 采样层（能力题 161 条） | 155，点值 0.9627，下界 0.9211 ⇒ **达档（≥0.9）** | 145，点值 0.9006，**下界 0.8447 ⇒ 未达档** |
| 硬层（回归组 "0 红"） | **18/19 ❌**（`note_traffic_denied_visitor`） | **19/19 ✅** |

逐条：**19 条不同，graph 独赢 14、react 独赢 5**，另 3 条两边都红
（`concurrent_orphan_history`／`challenge_claim_phantom_nav`／`followup_short_all_two_picks`）。
react 独赢的那 5 条：`rag_arch_components`、`nav_article_target`、
`note_traffic_denied_visitor`、`mix2_conditional_write_reads_first`、
`zako_admin_write_request_refused`。按 tag 读，差在**三族**：
`honest` **13/13 vs 7/13**、`admin` 54/58 vs 46/58、`write` 47/50 vs 39/50
（恰是"如实说做不到 / 后台写要不要弹卡"那一整片）；反方向 react 赢 `regression`
（19/19 vs 18/19）、`tool_call`（19/19 vs 18/19）、`zako`（4/4 vs 3/4）。

**② 终止**

| | graph | react |
|---|---|---|
| `GraphRecursionError` | **0** | **0** |
| 轮次总数 / 每例轮数分布 | 186 / {1:175, 2:4, 3:1} | 186 / **同一分布** |
| `tool_rounds` 合计 | 195 | 194 |
| 墙钟（逐例求和） | 1461.9 s | **1907.2 s（1.30×）** |
| 时延 p50 / p95 / max | 6.6 / 19.3 / 27.0 s | 8.1 / 27.6 / **39.9 s** |

`rag_project_files` 在 react 上是**保险丝真的开火**（用户可见正文里有"内部出了点问题"），
但那是**诚实收尾**而不是抛错。两臂的 LLM 轮数**不报**：本线的 trace 里没有逐节点事件
（见 §四 现状表 ＋ 第三条），两臂的轮数不在同一个可读面上。

**③ 可审计性**

| | graph | react |
|---|---|---|
| 工具调用总数 / 首轮即调 | 207 / 100.0%（54-54） | 211 / 100.0%（54-54） |
| 命令帧 `__CMD__` / 确认卡 `__CONFIRM__` | 33 / 28 | 34 / 29 |
| 零工具轮 | 74 / 186 | 75 / 186 |
| `resets` | 1（全 `text`） | **3（`all`:2 + `text`:1）**——多出的两条是 `gate_replan` |
| `__TASK__` 帧 | 10 | **1** |

**口径边界（这份读数不许被读过头）**：① `__TASK__` 与 trace 事件两格**两臂不可比**
——那是本线的欠账（§四 现状表 ＋ 第三条），不是行为差异；② 上面那张"假红"就出在本轮，
已修但**修后未复跑**（修完当场 provider 403，见下），所以 react 的 16 条红里按行为算
是 **15** 条；③ 结论只对那一笔提交成立，**两臂各有一半的道理**——点值 graph 领先，
硬层 react 领先，**没有任何一条证据支持"换骨架更准"**（仍是资产决定命中率）。
④ 跑完（01:49）之后 provider 开始回 403 `AccessDenied.Unpurchased`（02:23 实测，
连最小调用都不通）⇒ **这份读数不受影响，但修完那条假红的那次单例复跑被它挡住了**。
