# ADR-0014：planner 降成路由器，schema 索引化按需取

- **状态**：提议中（20261020）——阶段 0 落地且判据在跑之后转 `已采纳`
- **背景文档**：[../react-architecture-plan-20261020.md](../react-architecture-plan-20261020.md)（方案全文，含分阶段判据）、
  [../tool-menu-poc-20261020.md](../tool-menu-poc-20261020.md)（三臂读数）、
  [../prefix-cache-audit-20261020.md](../prefix-cache-audit-20261020.md)（成本审计）、
  [adr-0001-native-toolcalls-skill-level-seam.md](adr-0001-native-toolcalls-skill-level-seam.md)（**本 ADR 部分取代它**）、
  [adr-0009-react-arm-becomes-a-real-graph.md](adr-0009-react-arm-becomes-a-real-graph.md)（第二条臂已是真图）
- **影响范围**：
  · **阶段 0**：`agent/planner_llm.py::build_tool_schema`（description 截成一句索引）、
    新增 `describe_skill`、`agent/prompts.py` 的规划规则块。**不碰技能层、`instantiate_plan`、gate、写族。**
  · **阶段 1**：`agent/react_arm.py` 的 `menu_builder` 接缝（已在，缺省档逐字节不变）。
  · **阶段 2**：`agent/tools_arm.py`（POC 已建）、`agent/tool_menu.py`、`workflow_defs/nav_map.py`
    （`NAV_MAP` 下沉到工具层，见《取舍》第 2 条）。**射程只有只读 + 导航 + 检索。**
  · **阶段 3**：`agent/write_grounding.py`（9 个写参数校正器）、同意闸次序。
  · **`agent/graph.py` 与 `server.py` 在阶段 0/1 一个字节都不动** ⇒ 那两阶段的 A/B
    **不需要复跑 graph 臂**（依据是 `git diff --stat`，不是"应该没变"）。
- **判据**：阶段 0/1 `tests/test_prompt_prefix.py`（渲染前缀锁）+
  `tests/test_react_arm.py`（四类帧 / 三条铁律 / 缺省档逐字节）；
  阶段 2 另加 `eval/arm_capability.py --arm <新臂> --probe` 必须 `comparable=True`
  （否则**退出码 3、`pass_rate` 不许引用**）+ 19 条 regression 硬层不许空过。
  **本 ADR 被违反时红的是**：`tests/test_react_arm.py` 里"缺省档逐字节等于改动之前"那几条
  （接缝被改成非可选覆盖），以及 `tests/test_prompt_prefix.py`（稳定前缀被动过）。

## 背景

### 当年的接缝为什么开在技能级

ADR-0001（20260927）把 native tool calls 的接缝开在**技能级**——模型点的 function name
是技能名，`instantiate_plan` 照原样展开。它给了三条可当场复核的反证，**不是偏好**：
7 个参数校正器全读技能级 `plan_obj["params"]`；`navigate.target` 与
`navigate_to.path` 不是同一层；工具→技能反查天生歧义。

那三条今天**逐条复核过**（20261020，见《取舍》），结论写在下面——**其中两条仍然成立**。

### 现在为什么可以重开

**诊断：当年的脚手架在替模型补信息，不是在补能力。**
`docs/问题记录.md` 与 `gate 声称洞①–⑭` 那一族事故逐条读下来，**没有一条是"模型推不出来"**：
契约里出现可抄的否认句、站内「没有」误伤能力否定、`task_intents` 的"本领名"描述与
工具臂提示词互相打脸——**全是 harness 把信息喂错/喂漏**。而今天 schema、`authz` 角色过滤、
checker、同意闸、`reply_contract` 都已经把这些**显式写下来了** ⇒ 补信息有了别的落点。

**成本（实测，生产 trace + 离线渲染）**：

| 量 | 值 |
|---|---|
| planner 单次输入 | **52,145 tok** |
| └ 其中 tools schema | **38,738 tok（74%）** |
| schema 解剖 | 散文 41,499 字符（**50.2%**）/ 结构 41,113 字符 |
| **首轮决策落在 `chat`** | **40%**（21/57 轮） |
| planner 调用次数 | 1.61 次/轮 |

**最刺眼的一格**：40% 的轮次要付 52,145 tok 才能得出"这就是句闲聊"。
而 schema 那 50.2% 的散文**不是签名，是一份内联的目录**——它描述"这个站能干什么"。

### 但不能一次改完

`docs/tool-menu-poc-20261020.md` 的三臂：`graph` 0.9825 / `react` **0.9766** / `tools` **0.6725**。

⇒ **loop 本身只值 −0.6pt**（`react` 臂菜单不变），**所以"上 ReAct"不是风险源**。
风险全在 `tools` 臂，而它**一次改了两件事**（平铺菜单 + 丢掉 `instantiate_plan`），
**两件事的贡献分不开**——这正是本 ADR 分四阶段的原因。

## 决策

**分四阶段，每阶段独立、可回退、判据跑前定死**（全文见方案件 §6）：

| 阶段 | 动什么 | **不动什么** | 只证什么 |
|---|---|---|---|
| 0 | 散文外置（索引 + `describe_skill`） | 技能层、`instantiate_plan`、gate、写族 | 无回归 + **取回率** |
| 1 | 路由收窄（第一轮粗菜单） | **`instantiate_plan` 固定序列保留** | POC 的 A 族（13 条选错）转绿 |
| 2 | 执行层改 ReAct（**只对只读 + 导航 + 检索**） | 写族继续走模板；narrator 零工具 | POC 的 B 族（8 条定序漏步）**不许掉** |
| 3 | 写族 | — | 同 2 |

**红线：narrator 必须仍是零工具节点。** `agent/react_arm.py` 的 `_narrate` 写死了理由——
闸门判的是"叙述的**声称**与执行事实对不对得上"，而那个判据成立的前提是 narrator
**结构上不可能动手**（"我调用了 X"从它嘴里出来必然是声称）。**给它装工具 = 拆判据的地基。**
本 ADR 全程不动这一条，四个阶段都不动。

## 取舍

### 1. 为什么不"直接把描述删掉"

`eval/contract_position_ab.py`（7 用例 × 12 遍）实测：住正文 84/84、住 schema 84/84、
**整句删掉 73/84**。⇒ 那句话**本身承重**，但**位置无关**。
**索引化属于"换位置"，不是"删掉"**——这是本方案与"把描述砍了"的分界线。

### 2. ADR-0001 的三条反证，今天逐条复核（20261020）

**① 7 个参数校正器读技能级 `plan_obj["params"]` —— 仍然成立，而且现在是 9 个。**
它们已随模块拆分搬到 `agent/write_grounding.py`（ADR-0001 写的 `graph.py` 锚点已漂）：
`_board_quote_fix` / `_announcement_text_fix` / `_todo_text_fix` / `_name_target_fix` /
`_name_arg_fix` / `_target_grounding_refusal` / `_write_target_refusal` /
`_ledger_target_refusal` / `_freeze_policy_refusal`。全仓 **28 处**读 `plan_obj["params"]`。
**⇒ 但它们全部属于写族** ⇒ **阶段 2 的射程（只读 + 导航 + 检索）碰不到**；阶段 3 才碰。

**② `navigate.target` vs `navigate_to.path` —— 仍然成立，而且落在阶段 2 的射程里。**
实测：`navigate` 技能参数是 `target`，`navigate_to` 工具参数是 `path`（+`confirm`），
且 `path` 由技能层从 `NAV_MAP` 算出。**⇒ 这是阶段 2 必须正面回答的一条**，
做法是把 `NAV_MAP` 的别名解析**下沉到工具层**（`navigate_to.path` 收 alias、
由确定性 resolver 解析，字面路径仍过 `tools/base._NAV_EXACT_PATHS` 白名单），
并留一条锁：两种措辞（下线 / 未部署）逐字不变。

**③ 工具→技能反查天生歧义 —— 仍然成立**（实测 `get_moderation_status` 同时出现在
`dashboard_todo_list` 与 `moderation_report`；`list_admin_comments` 同时出现在
`comment_roster` 与 `dashboard_todo_list`）。**⇒ 但它只在"需要反查回技能"时才成问题**——
那个需求来自 `_confirm_grant_plan` 的 `tool ∈ skill.plan` 检查，**那是写路径的闸**。
⇒ **只读族不需要反查**（工具本身就是动作），阶段 2 不受影响。

### 3. 为什么不是"一次改完"

见《背景》末段：`tools` 臂一次改了两件事，**贡献分不开**。
分阶段是为了**让每一笔钱花在一个可判的问题上**——阶段 2 的判据只有一条：
**B 族不许掉**。掉了就说明定序那一半脱不开 `instantiate_plan`，**停在阶段 1**。

### 4. 为什么不是"把 70 个工具并成几个可组合的"

那**本身就是另一种手写分组**，量它等于量"手写分组 v2 vs 手写模板 v1"。
本 ADR 量的是**组织方式**（目录内联 vs 外置），不是重新分组。合并版留作后续独立议题。

### 5. 三处**未验证**（写在这里，不许当结论引用）

1. **取回率低 ⇒ 退化成"描述全删"**，而那个代价已量过（`contract_position_ab` 臂③ = 73/84）。
   ⇒ **必须有 `names-only` 对照臂**，index 与它的差**才是取回的价值**；打平 ⇒ 阶段 0 不成立。
2. **探针语料是 user 角色设计的**（`eval/experiments/menu_shape_ab.py` 的 25 条，
   无 admin 专属句）⇒ 跑 `--role admin` 前要先补，否则量的是"schema 本来就不大"那一档。
3. **"缓存命中率会很高"只在路由那一次成立**——今天 schema 是**恒定**的（跨会话可共享）；
   索引化后**执行层按路由变** ⇒ 跨会话不再共享。**总量大幅下降是真的，别把"命中率高"当结论。**

## 复验

| 观测到的事实 | 该做什么 |
|---|---|
| 阶段 0 的取回率与 `names-only` 臂打平 | **停**。索引没在起作用，`describe_skill` 是多余的 |
| 阶段 0 golden A/B 出现回归 | 退回：散文留在 schema。**先查是不是"索引切得太狠"**（截断规则），再怀疑设计 |
| 阶段 2 的 B 族（8 条定序漏步）掉了 | **停在阶段 1**。定序脱不开 `instantiate_plan`，这是本方案的真实边界 |
| 阶段 2 `arm_capability --probe` 不是 `comparable=True` | **退出码 3，`pass_rate` 不许引用**——纯否定族在"什么都不产出"的臂上恒真 |
| 有人提议给 narrator 装工具 | 回本文《决策》末段。**那是拆判据的地基，不是优化** |
