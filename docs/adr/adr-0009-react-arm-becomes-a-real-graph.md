# ADR-0009：第二条臂的循环拆成真节点（`create_agent` 退场）


> ⚠️ 20261017 起 `skill` 已改名为 **`workflow`**（`agent/skills.py` → `agent/workflow_defs/__init__.py`——20261017 先改名为 `agent/workflows.py`，20261018 批 2 收尾刀又把那个中转层整块删掉（**已删**）；`tests/test_skills.py` → `tests/test_workflows.py`、`tests/test_slim_skills.py` → `tests/test_slim_workflows.py`；计划 IR `SKILL=` → `WORKFLOW=`）。本文档保留**当时的名字**，不回溯改写；读它时把 skill 读作 workflow 即可。
> ⚠️ 20261018 又一批模块已改名（**记录型文档一律保留当时的名字，不回溯改写**）：`agent/absence.py` → `agent/absence_claims.py`、`agent/frames.py` → `agent/frame_contract.py`、`agent/probe.py` → `agent/runtime_probe.py`、`agent/decisions.py` → `agent/deterministic_plan.py`、`agent/entities.py` → `agent/entity_digest.py`、`agent/refs.py` → `agent/arg_refs.py`、`agent/workflow_defs/nav.py` → `agent/workflow_defs/nav_map.py`、`agent/workflow_defs/navigation.py` → `agent/workflow_defs/nav_skills.py`、`agent/workflow_defs/reports.py` → `agent/workflow_defs/report_skills.py`、`agent/context.py` → `agent/prompt_context.py`。**现状地图（每个模块一行）**见 `docs/agent-architecture.md` §2。

- **状态**：已采纳（20261016）
- **背景文档**：[../react-line-experiment.md](../react-line-experiment.md)（这条实验线本身的
  形状与能力台账）、[adr-0008-second-arm-stays-in-mainline-frame-contract-locked.md](adr-0008-second-arm-stays-in-mainline-frame-contract-locked.md)
  （上一批：帧契约收成有锁的产物——本 ADR 是它的直接续篇）、
  [../问题记录.md](../问题记录.md) §1.59（`_grant_covers` 两次收窄的现场）。
- **影响范围**：[agent/react_arm.py](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/agent/react_arm.py)（900 行 `stream()` 生成器 → 四个节点函数 + 三条路由
  + 三行委托）、[agent/react_line.py](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/agent/react_line.py)（删 `ConvergenceMiddleware` / `build_agent` /
  `wrap_tools_with_receipts` / `ReactLineState`，三条收敛判据抽成纯函数
  `convergence_verdict`；`SkillExecutor` / `RunLedger` / `wrap_up_text` 原样留）、
  `agent/frames.py`（删三份塑形器与五个帧构造器）、`tests/test_frame_contract.py`
  （③④ 从"喂没喂"改写成**结构锁**）、[tests/test_react_arm.py](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/tests/test_react_arm.py) 与
  [tests/test_react_line.py](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/tests/test_react_line.py)（约 20 处源码锁跟着改落点）、
  `eval/complexity_baseline.json` 与 `eval/mypy_baseline.json`（各销两条账）。
  **`agent/graph.py` 与 `server.py` 一个字节都没动** —— 生产路径逐字节不变，
  所以本批的 A/B **不需要复跑 graph 臂**（这是本批唯一能把验证成本砍半的那一刀，
  依据是 `git diff --stat` 为空，不是"应该没变"）。
- **判据**：`tests/test_frame_contract.py`（秒级、零网络零 LLM）。批 1 的 ③④ 是**真调一次
  塑形器**看它喂没喂全；那两件东西已经不存在了，判据换成三条**结构**判据，它们比原判据
  **强**，因为不依赖任何人的记性：③a–③c 编译出来的图里**真有** `NODE_FRAMES` 那四个节点名
  （生产者按名取键，少一个 = 那一格静默丢掉）、③d–③f 两张键集表**全是 `AgentState` 的
  声明键**且都在 `graph_input` 初值里、④a–④c 臂源码 AST 里**一个手搓的 `("updates", …)`
  二元组、一个 `langgraph_node` 字面量、一个 `*_frame` 构造器都不许有**（这三样是适配器的
  本体，回来一个就等于适配器回来一半）。反向对照 ⑥a–⑥h 照旧：改坏任何一种都必须当场红、
  且只红那几条。行为面另由 [tests/test_react_arm.py](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/tests/test_react_arm.py) ①（四类帧）②（三条铁律）
  ⑤⑥⑦⑧⑩⑪⑬ 锁住——**这些行为锁本批一条都没改**（改的只有"钉字面"的那几处落点）。
- **触发条件**：不适用（已采纳）。

## 背景

批 1（ADR-0008）把**帧契约**从口头约定收成了一件有锁的产物（`agent/frames.py` +
`tests/test_frame_contract.py`），并修掉一条真分歧（`_consent_missing` 改调生产
`_grant_covers`）。但 [agent/react_arm.py](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/agent/react_arm.py) 那 900 行的**外层适配器**原样留着。它是一个
`stream()` 生成器，做四件事：

1. **手搓 `("updates", {...})` 二元组**——图语义里 `updates` 是节点跑完由 langgraph 产的，
   适配器自己拼一份"形状对的"；
2. **给分片冒充 `meta["langgraph_node"] = "model"`**——生产者只把
   `langgraph_node == "model"` 的分片当用户可见正文，适配器就自己刻一个；
3. **给 `gate_node` / `_confirm_popup` 喂合成 state**（`frames.round_state` /
   `popup_state` / `gate_state`）——两个生产函数拿到的不是 `AgentState`，是适配器拼出来的
   最小 dict；
4. **`create_agent` + `ConvergenceMiddleware` + `wrap_tools_with_receipts`** 驱动内层循环。

批 1 已经用事实证明了第 3 样有多贵：`_popup_state` 那张**自称四格**的表，AST 走完读侧
传递闭包实测**十一格**，漏掉的 `executed` 正是 `_grant_covers` 「令牌用在那一件上就作废」
那一格 ⇒ 臂的弹卡路径上那条判据**恒不生效**；`gate_state` 那张表少 `task_frames` ⇒
闸门在"这一轮 planner 刚登记过任务"的现场**静默地少开一次火**。两处都是
`.get("X", [])` 把"本线没喂"与"本轮确实没有"读成同一件事，**抄漏不报错**。

### 四条调查结论（它们决定了形状，不是可选项）

1. **真图节点只在节点结束时产一条 `updates`**（LangGraph 语义），而 `execute` 的工具帧
   必须**边跑边发**（主人要实时看到命令/回执）。⇒ 内层 ReAct 循环**必须拆成真节点**，
   `create_agent` 挂不成一个节点。**这是计划里没写到、实现时才发现的约束。**
2. **拆分是逐字等价的**：`langchain.agents.factory._execute_model_sync` 的模型调用就是
   `model.bind_tools(final_tools, tool_choice=request.tool_choice, **model_settings)`
   `.invoke([system_message, *messages])`（无结构化输出时 `tool_choice=None`、
   `model_settings` 空）⇒ 搬迁可逐字对齐，不需要"重新实现一遍内层循环"。
3. **真节点返回 `AIMessage` 会出现在 `messages` 流里**，且 `meta["langgraph_node"]` = 该节点
   自己的名字（`langgraph/_messages.py` 的 `_find_and_emit_messages` **不按消息类型过滤**）
   ⇒ 第 2 样冒充**结构上不再需要**。嵌套图（`subgraphs=False`）的内层消息不外泄。
4. **分片的真来源是 LLM 流**，不是节点返回值：`StreamMessagesHandler` 在
   `on_llm_new_token` 上把 `ChatGenerationChunk.message`（= `AIMessageChunk`）推给
   `messages` 通道。而 `BaseChatModel._should_stream` 只要有 `_StreamingCallbackHandler`
   就返回 `True`，langgraph 每个节点都挂了一个 ⇒ **在真图节点里 `llm.invoke()` 就会流式**。
   生产 `model_node` 用 `llm.invoke()` 却能流式，靠的正是这一条（配
   `settings.llm_streaming=True`）。**本线因此自动获得真流式**——见《决策·四》，那是本批
   唯一一处**刻意引入的行为变化**。

### 做完之后消失的东西（这就是判据）

| 消失物 | 为什么它必须死 |
|---|---|
| `create_agent` / `ConvergenceMiddleware` / `build_agent` / `wrap_tools_with_receipts` | 循环不再由内层图驱动；收敛判据搬进 `planner` 节点，成为可单测的纯函数 |
| `round_state` / `popup_state` / `gate_state` | 两个消费端拿**真 state**，不需要塑形——而塑形就是"手抄键集表"这个缺陷本体的产地 |
| `planner_frame`/`model_frame`/`gate_frame`/`execute_frame`/`visible_chunk` | 帧由**节点名 + 节点返回值**结构性地产生 |
| `meta["langgraph_node"]="model"` 冒充 | `model` 是**真节点**，名字由 langgraph 盖 |
| 手搓的 `("updates", …)` 二元组 | 图里没有地方可以手搓 |
| `_gate` 里手动丢 `RemoveMessage` 那一段 | 图里 `gate → planner` 这条边**就该**让 `RemoveMessage` 生效（生产同形） |

## 决策

### 一、臂的图 = `graph.AgentState` + 四个真节点 + 四条条件边

```
START → planner ─┬─(有技能调用)──→ execute ─┬─(弹卡/零改动)──→ END
                 │                          └─(还有得做)─────→ planner
                 ├─(零工具 = 收尾)─→ model ──→ gate ─┬─(gate_replan，只此一次)→ planner
                 └─(预算/无进展)───→ model            └────────────────────→ END
```

- **节点名恒为** `planner` / `execute` / `model` / `gate`（= `server.py` 从 `updates` 里
  现取的四个键）。名字不再是"声称"，是 `add_node` 的结果；`test_frame_contract.py` ③a
  把它与 `frames.NODE_FRAMES` 对起来，⑤b 再与 `server.py` 现取的键集判**相等**。
- **state 就是 `graph.AgentState`**（复用生产那一份声明，不是另立一份）。`graph_input()`
  那 25 个键一个不丢——未声明键被 langgraph **静默丢掉**是本仓付过 13 次代价的一族。
- **入口是 `START → planner`，没有入口条件边。** 计划里原本写的是"快道命中 ⇒ 从 START
  直接进 `execute`"，实现时否掉了：`add_conditional_edges(START, …)` **写不了 state**，
  绕过 `planner` 就没人写 `plan_obj`/`plan_rounds`，生产者那一格 `updates["planner"]`
  会**缺席**（`🧭 规划中…` 不发），而且路由顺序会把 `updates` 帧的次序打乱。快道判据因此
  住在 `planner` 节点里，与生产 `planner_node` 的 `rounds == 0 and not has_frames`
  **同一条守卫**（那是生产注里写明的"进循环后再判 = 死循环陷阱"）。
- **`gate → planner` 只走一次**，两处保证：`gate_node` 的 `fail()` 闭包自己就有那条
  `not state.get("gate_replan")`；`route_after_gate` 再确认一次 `not done`。两条都与生产
  `graph.route_after_gate` 逐字同款。

### 二、四个节点各是什么

- **`planner`** —— 一次决策，等价于 `create_agent` 的一轮。五步：
  ① 预算判据（原 `ConvergenceMiddleware.before_model`）——**撞预算的那一轮照样执行**，
  停的是"再来一轮"（判在拿到模型输出之后会把模型已经点名的调用整个吞掉：用户等了半天、
  动作没发生）；② 快道判据；③ `bind_tools(技能菜单).invoke([SystemMessage, *窗口])`；
  ④ 无进展判据（原 `after_model`）；⑤ 意图纠偏（原 `_intent_correction`，打回自己
  重决策**一次**）。
  **本节点不往 `messages` 里写任何东西**（生产 planner 同款）：`with_tool_call_pairs`
  会给"没人认领的工具帧串"补一条合成 assistant，planner 一旦也写消息，那些帧就会被认领
  进 planner 的 assistant 里，而 planner 的 `tool_calls` 与 execute 真发出去的
  `tool_call_id` 未必对得上 ⇒ **出站序列非法**（严格服务商一律 400）。**决策由
  `plan_obj["calls"]` 承载，不靠消息表。**
- **`execute`** —— 跑清单：`SkillExecutor.run(skill, params)` 逐条（技能级展开 → 权限 →
  同意闸 → checker 验收 → `receipts`/`blocked`）。**逐条产 `ToolMessage`，`name` 取真工具名**
  ——本线的模型菜单是技能级的，而 `require_tool_calls`/`require_zero_exec` 认的是
  `_TOOL_REGISTRY` 里的真名，用技能名会让那两族**静默失真**（`require_zero_exec` 更坏：
  写工具永远不在 `tool_calls` 里 ⇒ 恒绿）。收尾判一次弹卡：`_confirm_popup(state, …)`
  拿到的是**真 state**。
  **快道那一圈也走这个节点**：清单来源两处（系统快道 / planner 决策），生产
  `execute_node` 执行的是 `state["plan"]`，快道与决策写的都是同一格，同形。
- **`model`** —— narrator，三条出口：① **确定性正文路**（收敛判据的收尾语 / 崩溃语）：
  不调 narrator，直接发一片 `AIMessageChunk`；② **正常路**：调 narrator（真流式）⇒
  返回**原始响应对象**；③ **narrator 失败退回草稿**：同 ①。
  为什么 ②③ 必须自己造一片：正文**只有分片这一条通道**（生产者只把
  `isinstance(chunk, AIMessageChunk) and meta["langgraph_node"]=="model"` 的分片
  `queue.put` 给主人；`updates` 里的 `final_reply` **只进 trace**）。而 `AIMessageChunk`
  是 `AIMessage` 的**子类** ⇒ 生产者两边、`updates` 那侧的 `isinstance(_m, AIMessage)`
  都过。**两边都认的只有这一个类型**——所以确定性正文的唯一出口是 `_chunk(text)`，
  返回一条裸 `AIMessage` 会让这段正文只进 trace、不进主人屏幕。
  narrator 的输入里**仍然没有决策模型那条零工具草稿**（P6 的刻意设计）。
- **`gate`** —— 直调 `graph.gate_node(state, config)`（**真 state**，**同一个资产**）。
  判据（`_REPLAN_ISSUES` 约 20 族 / `_claim_issue` 的声称族 / 复读族 / 编造 URL /
  零帧如实核验）**一条都没抄**——抄第二份就是抄一条漂移源。异常照旧**吞掉按 pass 处理**
  并留 WARNING（见《取舍》）。

### 三、`ReactGoldenArm` 保留，但只剩三行

`build()` 编译一次图，返回 `ReactGoldenArm(graph)`；它的 `.stream()` 只做两件：把
`RunLedger` + `SkillExecutor` 塞进 `config["configurable"]`，然后
`yield from self._graph.stream(state, config, stream_mode=...)`。

**为什么账走 `config["configurable"]` 而不是图 state**：图是 `build()` 时编一次、跨请求
**共用**的单例，而账是**一次运行一个实例**的非序列化物件。放进 state 才是错的（并发两个
请求会往同一本账上写）；放进 configurable 与生产把 `stop_event` / `principal` /
`open_tasks` 放那儿**同源**。`make_ctx()` 是唯一构造点（`ReactGoldenArm.stream` 与离线
探针共用），离线探针自带 `react_ctx` 时就整份用它——否则调用方手上那本账永远是空的。

**保留这个类名是硬要求**：[tests/test_react_arm.py](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/tests/test_react_arm.py) ⑫ 断言装在 `server._agent` 上的对象
类型名，`eval/arm_capability.py` 的 `_ForcedGate` 也按 `.stream(state, config,
stream_mode)` 包装。

### 四、真流式是**刻意**引入的行为变化

改前臂手搓**一整片**（全文一次性）；改后 narrator 的 token 分片由 LLM 流给。理由：
① 这是生产**今天**的形状（生产 `model_node` 同一句 `get_llm`，`llm_streaming=True`）；
② 手搓分片本来就是适配器的产物。**代价写在《取舍》里，不假装同源。**

### 五、崩溃兜底

`planner` / `execute` 节点各自 `try/except`：异常 ⇒ 记 WARNING（原样吞掉才是真查不出来）
⇒ 返回"崩溃收尾"状态（零工具 + 确定性正文）⇒ 走 `model` 的 ① 出口发给主人。
**不做**在图外面接异常再手搓帧的那一套——那等于把刚删掉的东西请回来。

## 取舍

**两条刻意引入、明写不假装同源的口径差：**

1. **真流式**（《决策·四》）。代价两条：① 分片在闸门**之前**就出去了（改前在闸门之后）
   ——生产同形，`__RESET__` 负责把被打回的正文从主人屏幕上撤掉；② `_narrator_call` 的
   "回了 tool_calls 再纠正一次"那一支，第一次的正文已经流出去了 ⇒ 主人会看到两段。
   **生产有同一个洞**（同一段代码、同一个流式开关），本批**不发明第三套处置**。
2. **`gate` 节点的异常吞掉按 pass 处理**。闸门是**加装**的一层，它自己炸了不该把主人这
   一轮的答复整条吃掉——生产那边会被 langgraph 变成整轮错误，本线没有那层兜底，所以自己
   兜。`_popup_update` 与 `_narrate` 的异常同一条理由。

**一批继承来的、本批不动的口径差**（模块头注《已知口径偏差》有完整清单，此处只记
"为什么不顺手改"）：`planner` 的提示词**每请求只渲染一次**（生产是每轮重渲染，而
`rounds`/`frames_txt`/`blocked_rows`/`ref_hints` 都会变）——改它会让 A/B 读出的差异分不清
是结构还是提示词；`planner` 读的是**真消息窗口**而生产读 `_render_planner_prompt` 渲染的
一个字符串（信息面同源、形态不同）；**快道与意图纠偏都不吃决策预算**（`advance=False`，
按旧臂口径——改它会让读数里混进一条与结构无关的预算差）；`bind_tools` **不传
`parallel_tool_calls`**（生产 planner 是 `False`）⇒ 本线容忍一轮多条调用，偏差表现为
"步子更碎"，不会丢调用；`_narrator_call` 在原文上**就地改** `resp.content`（贴纸记号修复）
而**不是**另建一条 `AIMessage`——重建会换掉 id，而那条 id 已经被 `messages` 通道认领过
（`on_llm_end` 的 dedupe 靠它），重建 = 同一段正文发两遍。

**本批仍然不做的**（如实登记，别当成已经做完）：`task_frames`（`__TASK__`）**不发**——
能力台账里这一族只有 1 条断言，且它的兄弟判据由生产那半的确定性结算满足 ⇒ 它是 `armed`
而**没有**变成 `comparable = False` 的来源；**逐节点 trace 词汇表**（生产每轮的
`planner.llm_done` / `native_decision` / `decision` / `execute.call` / `execute.check`）
**不补**——今天一个读数都不改变（没有断言读它们），而**拆成真节点之后补它变得便宜**
（每个节点函数自己 `record(...)`），那是批 3。

**两条工程棘轮上的处置，都是"缩小"而不是"放宽"：**

- **`SkillExecutor.run` 的复杂度**：抽出模块级纯函数 `literal_path_override`（`navigate`
  的字面路径防推断兜底，正则**比 `graph.py` 那处紧一格**——松的那版会把整条 URL 吞成一个
  路径、也会把 `12/25 号` 里的 `/25` 当成主人点名的路径）并把 `ledger.record` 的两条硬
  要求搬出函数体 ⇒ **C 33 → 27、124 → 112 行**，两项都回到预算内 ⇒
  `eval/complexity_baseline.json` 里那条**销账**。这是棘轮按设计工作，不是放松。
- **mypy**：图改造让 [agent/react_arm.py](https://github.com/BigLeopardCat/saudade-blog-agent/blob/563c8a5/agent/react_arm.py) 的错误从基线 10 涨到 **23**（全是类型注解口径，
  无运行时缺陷）。处置是把注解改**准**而不是记账：langgraph 交给节点的一切都是
  `RunnableConfig | None`、节点体是 `AgentState`、路由表必须是 `dict[Hashable, str]`
  （`add_conditional_edges` 的形参就是它，而 dict 的键**不变** ⇒ `dict[str, str]` 传不
  进去）。⇒ **23 → 0**，`total` 219 → 204，基线手改销账。

## 落地时抓出的一条真回归：`planner` 看不到自己上一轮的真参数

**全量 A/B 抓出来的**（183 条、同语料 `c56c27efc0`、同 skip 集、同日两次跑）：

| | 改前（`f13dde6` + 旧臂） | 改后（拆成真节点，未修） | 改后（＋本节这条修复） |
|---|---|---|---|
| 通过 | **168/183 = 0.918** | **156/183 = 0.852** | **172/183 = 0.9399** |
| rag 族 | 26/26 | **13/26** | **25/26**（30 条定点块上 23/24） |
| 30 条定点块（24 条 rag ＋ 6 条绕圈例） | 30/30 | **15/30** | **29/30** |

**症状不是"答错"，是"绕圈"**：`after_offer_then_look_up` 从 2 次工具调用变成 **29 次**
（同一个 `search_notes` 反复重发）；`rag_git_branch` 做成了同样那两件
（`rag_search` + `get_article_detail`，回执逐字相同），却把 6 轮预算烧穿，主人拿到的是
确定性收尾语「这一轮的步骤数用完了」而不是答案；`rag_ota_partition` 更早就撞
`no_progress`。**执行没变，停不下来了。**

**根因**（一条命令就能复现，零 LLM）：

```
HumanMessage | 'Git 的分支为什么很轻量？'
AIMessage    | ''  | [('content_query', {}), ('content_query', {})]   ← 参数是空的
ToolMessage  | '<article text>\n[回执] 已执行'
```

`_window` 用 `with_tool_call_pairs` 给工具帧补"配对的 assistant"，而那个函数**只补形状**：
`content=""`、`args={}`（`agent/context.py` 的 docstring 自己写着"只补形状、不动内容"）。
于是决策模型**看不到自己上一轮问的是什么**，只看到一串「`content_query`(空参数)」加几条
结果正文 ⇒ 认不出"这件事我刚做过" ⇒ 反复重问。**旧臂没有这个病**：内层 `create_agent`
累积的是模型自己那条真 `AIMessage`（带参数、带正文）——那两样是**内层循环白送的**，
拆成真节点之后要自己补回来，本批第一版漏了这一步。

**修法**（`_pair_frames` / `_pair_run`）：`execute` 节点登记"这一批清单是谁发的"
（`ctx["calls"]`）与"每条工具帧是哪一条调用发的"（`ctx["origin"]`），`_window` 按它把连续
同出处的帧合成一条**带真参数**的 assistant；没有出处的（系统快道之前发的、异常路径的）
原样交给 `with_tool_call_pairs`——**不猜**。

**只补参数、不补正文**：模型自己那条叙述（`content`）既不是事实也不进闸门，补它只是多烧
token；要的是"我问过什么"这一个**可核对的坐标**。这条修复**不是调参**——它把旧臂结构上
白送的一样东西补回来，判据是那 30 条定点块从 15/30 回到 29/30，全量从 156/183 回到
**172/183**（比改前的 168 还多 4 条，多出来的那几条是改前被负正则措辞判红的方差）。
逐条定性见 `../react-line-experiment.md` 的《批 2 的前后对照》：8 条判定变化里 6 条是方差、
2 条是"同码异跑"（`nav_friends_down` 是 narrator 采样，闸门按设计把它抓了；
`rag_project_files` 是预算边缘——同码定点单跑 PASS、全量里烧穿 6 轮）。
**残留一处**：`_pair_frames` 补的是**参数**，补不回模型自己那条叙述（`content`）
⇒ `rag_project_files` 这种"读过了还想再读"的情形仍会在预算边缘翻车，批 3 再看。

**下一次的纪律**：拆一个"循环驱动器"（`create_agent` 一类）的时候，**先列出它白送了什么**
——消息表怎么攒、谁的参数看得见、哪条消息带 id——再逐条确认新形状有没有对应物。
本批列了三条（工具帧、注记帧、分片通道），漏了第四条（**模型自己的调用参数**），
代价就是上面这张表。

**这张表不是"只差一个变量"的对照，如实记下另外两处**（改前那一遍 02:36–03:02 跑完之后、
改后那一遍之前，工作区里落进了**别人的未提交改动**）：① `eval/run_golden.py` 的判据窄修
（把「别以为/不要以为/别觉得」这类**劝阻式否定**收进 `_NEG_PROPOSITION_PREFIX_RE`）——它
只会把"本该放行的句子"放行，方向是**单侧**的（只可能 FAIL→PASS，不可能 PASS→FAIL）；
② `agent/graph.py` 的 `_confirm_popup` / `execute_node` 多了一条 `action_conflict` 分支
（反极性改选不弹卡）——它在臂上是活的（臂直调这两个函数），但只在"planner 排的那一件恰好
是主人这句话的反义词"时才开火。**射程是量出来的，不是猜的**：拿全量 192 条用例的 239 条
用户消息 × 67 个工具跑 `authz.action_conflict`，命中 3 个 (消息, 工具) 对，全在
`own_favorite_*` / `favorite_remove_zero_write` 三条用例里，且都是**工具取反义**的那一侧
（用例本身写的是正确极性）⇒ 那三条的判定变化归**绕圈回归**，不归这条分支。
`agent/prompts.py` 那一处改的是 `_PLANNER_PROMPT`，臂不用它（臂的 `_system_prompt` 自己
长在 `react_arm.py` 里，只从 `prompts` 取 `BLOG_ASSISTANT_PROMPT`/`STICKER_GUIDE`/
`audience_block`）⇒ 与臂无关。

## 复验

```bash
cd /home/ubuntu/Saudade-Blog/saudade-blog-agent

# 结构锁（秒级、零网络零 LLM）
.venv/bin/python tests/test_frame_contract.py

# 行为锁 + 收敛判据纯函数
.venv/bin/python tests/test_react_arm.py
.venv/bin/python tests/test_react_line.py

# 推前三件
.venv/bin/python -m pytest -q
.venv/bin/python eval/type_gate.py
.venv/bin/python -m ruff check --select F821 .

# 读数许可闸：forbid_fallback 的正控要能真的开火
.venv/bin/python eval/arm_capability.py --arm react --probe
```

**负控（graph 臂零改动）**：本批**自己没有**动 `agent/graph.py` 与 `server.py`——判据是
`git diff f13dde6 -- agent/graph.py server.py` 里**没有一行属于本批**。⚠️ 但**别用
`git diff --stat` 为空当这条判据**：跑本批 A/B 的那两天，工作区里同时躺着**别人的未提交
改动**（`graph.py` 的 `action_conflict` 分支、`server.py` 的 `_REASON_CN` 一格、`prompts.py`
的一条 planner 规则），那三处**确实**在生产路径上。⇒ 正确的说法是：**本批的改动面不含
生产路径**（本批点名的 13 个文件里没有它们），所以"结构上不可能因本批而变"成立；
而"这一遍读数与改前那一遍只差本批"**不成立**，那两处差异见上一节末尾的登记。

**前后对照**：见 `docs/react-line-experiment.md` 的状态节（本批读数与逐条定性）。
