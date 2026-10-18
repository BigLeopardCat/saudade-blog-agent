# ADR-0010：state 累加器的合并语义——先立台账，暂不加 reducer
> ⚠️ 20261018 又一批模块已改名（**记录型文档一律保留当时的名字，不回溯改写**）：`agent/absence.py` → `agent/absence_claims.py`、`agent/frames.py` → `agent/frame_contract.py`、`agent/probe.py` → `agent/runtime_probe.py`、`agent/decisions.py` → `agent/deterministic_plan.py`、`agent/entities.py` → `agent/entity_digest.py`、`agent/refs.py` → `agent/arg_refs.py`、`agent/workflow_defs/nav.py` → `agent/workflow_defs/nav_map.py`、`agent/workflow_defs/navigation.py` → `agent/workflow_defs/nav_skills.py`、`agent/workflow_defs/reports.py` → `agent/workflow_defs/report_skills.py`、`agent/context.py` → `agent/prompt_context.py`。**现状地图（每个模块一行）**见 `docs/agent-architecture.md` §2。

- **状态**：已搁置（20261016）
- **背景文档**：[../../tests/test_state_reducers.py](../../tests/test_state_reducers.py)、
  [../../tests/test_state_keys.py](../../tests/test_state_keys.py)、
  `agent/graph.py` 的 `AgentState`
- **影响范围**：本份**无**（只登记台账，一个字节的合并语义都没改）。重开后的第 2 步才动
  `agent/graph.py` 的**所有**写入者。
- **判据**：[../../tests/test_state_reducers.py](../../tests/test_state_reducers.py)
  ——② 未登记的裸 list 键、③b 过期条目、① 扫面自检（判据瞎了 ⇒ 后面全是恒真的假绿）。
- **触发条件**：见文末《复验》下的那一节。

## 背景

`AgentState` 是这张图的"工作台"（`agent/graph.py` 第 1 节标题原话）。28 个键，其中
**9 个是 list**。LangGraph 合并两份 state 更新时**只有带 reducer 的键**才做合并，其余键
一律 **last-write-wins**。今天**只有 `messages` 带** `Annotated[list, add_messages]`，
其余 **8 个是裸 list**。

裸 list 不是错——错的是**没人分得清哪几个是累加器**。这 8 个里混着三种语义：

| 语义 | 键 | 写点 |
|---|---|---|
| **请求内累加器** | `executed` / `receipts` / `noop_specs` / `blocked_seen` / `tool_data` | `execute_node` 取 `list(state.get(...) or [])` → 逐条 append → **整体再返回** |
| **覆盖（last-write-wins 正是要的）** | `blocked`（只含本轮受阻项）、`task_satisfied`（每轮重算） | `execute_node` 现造 `blocked: list = []`；`_confirm_popup` 返回 `_satisfied_specs(already)` |
| **未裁决** | `task_frames` | `planner_node` 的壳**累加**（`list(upd.get("task_frames") or ()) + sink`），`_plan_update` 是**整体赋值** |

累加器那一行是这一族的缺陷形状：累加**完全靠每个写入者记得把整个累加器原样再返回一遍**
（`"executed": executed + [s for s in specs if s not in executed]`）。**漏掉那一步 = 静默丢
一半，没有任何东西会红。**

「初值」那一半早已锁死：`graph_input()` 给 28 个键**全部**配了初值，键集的"恰好性"由
`tests/test_state_keys.py` ⑤ 锁住。**「reducer」这一半此前没有任何判据**——分不清哪几个是
累加器，也就分不清"忘了再返回一遍"与"本来就该覆盖"，两者在源码上长得一模一样。

## 决策

分两步。**本份只落第一步，第二步搁置。**

1. **已落**：立 `tests/test_state_reducers.py`。每个裸 list 键**要么带 `Annotated` reducer、
   要么在台账里写明这个合并语义为什么是对的**；台账**双向**（不许漏登、也不许留过期条目）。
   **零行为变更。**
2. **搁置**：逐个把累加器改成真 reducer（`operator.add` / 去重 add / …）。

## 取舍

为什么搁置第 2 步——**不是"不值得做"，是"现在做会把风险叠在一起"**：

- **改 reducer = 改 LangGraph 的合并语义 = 所有写入者的写法都要跟着改。** 今天写入者是
  手工 `acc + [...]`；加 reducer 之后同一行会变成**双重累加**（reducer 再 `+` 一次）。
  这是整族里风险最高的一刀，而它**没有任何判据能预先告诉你改漏了谁**——正是本份第一步
  要补的那张表。
- **六个累加器的合并语义各不相同**：`executed` 要去重、`blocked_seen` 是集合语义
  （`sorted(prev_seen | {...})`）、`tool_data` 的**顺序即下标**（`$tool[N].field`，
  见 `agent/refs.py`）。**不是无脑 `operator.add`。**
- **`task_frames` 两种语义并存**，还没裁决；它是台账里唯一被标成"待裁决"的一格。
- 这一步会与 `agent/graph.py` 上**正在进行的另一批改动**抢同一块地。

备选方案与它们为什么不行：

| 备选 | 为什么否 |
|---|---|
| ① 当场给 8 个全加 `operator.add` | 会把去重（`executed`）与顺序（`tool_data`）语义改坏，`task_frames` 直接冲突 |
| ② 什么都不做 | 第 10 个裸 list 键还会以同样的方式出现——本仓「有责无声」那族的标准形态 |
| ③ **本方案** | 先立台账（零行为变更），把"哪几个是累加器"从**口头约定**变成**可枚举的表**，再逐族动 |

台账为什么落在**判据这一侧**（`tests/test_state_reducers.py`）而不是 `AgentState` 旁边：
它是判据的**数据**，与判据同生共死；`AgentState` 的 docstring 已经把每个键的语义写在那儿了，
不必抄第二份（抄两份就会漂）。

## 复验

`tests/test_state_reducers.py` 全绿，含三条合成反控（④ 摘掉 `messages` 的 reducer /
⑤ 理由为空 / ⑥ 条目过期）+ 一条负控（⑦ 带 reducer 的键不许被报成未登记），外加一条
**端到端反控**：往 `AgentState` 真加一个裸 list 键 ⇒ ② 当场红并点名。

## 触发条件

满足**任一条**就该重开本份、按台账逐族定 reducer（**一次只动一族**）：

- 任一累加器**真出过一次"静默丢一半"**——判据是同一请求内 trace 里 `receipts` / `tool_data`
  的条数**少于**实际执行数；
- 需要往 `AgentState` 加**第 10 个 list 键**；
- `task_frames` 的两处语义（壳累加 / `_plan_update` 整体赋值）必须收成一种；
- `execute_node` 的复杂度/行数台账（`tests/test_complexity_budget.py`）腾出了空间——
  第 2 步要往写入者里塞东西，而那张台账**只许缩小**。
