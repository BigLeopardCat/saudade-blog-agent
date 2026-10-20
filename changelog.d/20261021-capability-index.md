- **新增能力索引 `agent/capability_index.py`（ADR-0015 阶段 2 的第一块）**：把"一条技能"
  从**注册表形状**（`plan` 固定序列、`inputs`、`optional_params`）翻成**决策侧形状**
  ——一行索引 + **这条能力涉及哪些工具** + 完成判定 + 回复契约。planner 从此选的是**能力**，
  执行层拿到的是一条能力那一批工具（多数 1–3 件）。
  ⚠️ **本笔只是索引本身，还没有任何生产路径 import 它** ⇒ agent 对外行为**零变更**
  （缺省档逐字节不变）。接线（新臂 + 执行层 ReAct）是同一 feature 的下一笔。
  ① **为什么是它**：工具级菜单 POC（`docs/tool-menu-poc-20261020.md`）三族的红，**同一件
  东西**同时解释——A 族（13 条，路由）红在菜单是**全部** 70 件工具；B 族（8 条，定序）红在
  `checker 验收回执缺少工具 X`，而裸工具表里**没有"这条能力该调哪些工具"这个要求**；
  C 族（4 条，检索）红在回复正文缺关键词，而 `complete_when` / `reply_contract` 是每条能力
  各自写的散文，在工具臂上**没有家**。索引项把这三样一起带上。
  ② **单一来源，零新增手写**：工具面 = `[n for n, _ in wf.plan] + list(wf.extra_plan_tools)`
  ——与 `workflow_defs._derive_write_workflow_names` **逐字同一条算法**；索引行走
  `tool_index.index_line`（机械切、不改写）。**不另立一张分组表**：重新分组本身就是另一种
  手写分组（ADR-0015 取舍 #4）。
  ③ **射程（如实记边界）**：`react_scoped` 为真的走"执行层 ReAct"——只读 + 导航 + 特效 +
  夜间 + 设备 + 检索（`content_query` 的工具面按角色现算，`dynamic=True`）；为假的
  **继续走模板**——写族 30 条（9 个参数校正器、28 处读技能级 `plan_obj["params"]` 的落点
  全在那儿）+ `chat`（零工具，靠 `WorkflowExecutor.run` 的纯应答分支留痕）。
  ④ **判据**：新增 `tests/test_capability_index.py` 五组——索引名字集合 == `visible_workflows`
  （逐字且同序）/ 工具面 == `plan ∪ extra_plan_tools`（判据侧**独立算一遍**，不调被测函数）/
  索引行是原文**前缀**且 ≤80 列 / 承重格在场（工具名进菜单项、`complete_when` 与
  `reply_contract` 进执行层简报）/ 射程与写名单**互补** / 够不着的名字一律 `None`。
  **四条反控实测会红**：工具面写成常量、`react_scoped` 恒真、索引行不切、取回口不判可见性
  —— 各红 4 / 66 / 130 / 84 条。
  ⑤ **不扩权**：可见性判据用 `visible_workflows(role)`——与 `build_tool_schema`、planner
  菜单、`task_intents` 归一化、`describe_skill_text` 是**同一张表**。系统专用技能
  （`read_article`）不在任何角色的索引里。
  ⑥ `docs/agent-architecture.md` §2 加了这一行（`test_module_index.py` ② 判"磁盘 ⊆ 树"）。
