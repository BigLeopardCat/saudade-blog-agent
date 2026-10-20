- **新增 golden 工具臂 `GOLDEN_ARM=tools`（POC，⚠️ agent 对外行为零变更）**：菜单从「技能模板」
  换成「按角色过滤的真工具表」，模型**直接点工具**，没有 `instantiate_plan` 那一层中间展开。
  它要量的是「harness 撑不撑得住」，判据是**一件都没省**：角色过滤（与执行期同一张 `authz` 表）、
  权限闸、同意闸（写操作弹卡）、参数引用 `$tool[i].field`、生产 checker 的 PASS/BLOCK 与回执行、
  无进展/预算/意图纠偏、`gate_node`、零工具 narrator —— 全部由 `react` 那张**真图**承载。
- **`agent/react_arm.py` 抬出四个可选接缝**（`make_ctx` 的 `prompt_builder` / `menu_builder` /
  `plan_builder` / `executor_factory`，经 `ReactGoldenArm.ctx_hooks` 类属性摊进去）：四格全部缺省
  `None` ⇒ 各自回落到改动前那一行。**缺省档零行为变更**由 `tests/test_arm_seams.py` 钉住——
  基类 `ctx_hooks` 恒为空字典、缺省执行器 `type(...) is WorkflowExecutor`、以及「缺省基类」与
  「四格显式 None 的子类」两条跑出来的**帧序列逐字节相同**（过一遍剔掉时间戳与 uuid 的投影，
  否则判据自己会随机红）。含三条反控：键名写错一个字母、基类自带接缝、`stream` 不摊 `ctx_hooks`
  —— 各自让对应断言变红。**接缝刻意只加这四处**：同一文件上另有一条未合并的分支在大改，冲突面越小越好。
- **新增 `agent/tool_prompt.py`**：工具臂的系统提示。块序与资产与生产那份**同源**（人设 / 受众 /
  叙述纪律 / 贴纸 / 动态槽位都取同一批 helper），只重写菜单块与输出契约——照抄生产那份等于拿一份
  写着「技能注册表」「`PARAMS.calls`」的契约去考一张工具表。**它本身就是一条 POC 结论的原料**：
  换掉手写技能模板，省掉的是「每个能力一条模板」，**省不掉一份菜单契约**。
- **新增 `agent/tools_arm.py`**：工具臂的四个接缝实现（`menu_of` / `prompt_of` / `plan_of` /
  `ToolExecutor`）。`plan_obj["workflow"]` 是**从工具名倒推的粗标签**（gate 按它分族）——这是一处
  **真实的口径差，报告里不许抹掉**。两处搬不动的生产资产在这里借回：`navigate_to` 走
  `instantiate_plan("navigate", …)`（别名 / 字面路径白名单 / IoT 两种措辞 / `confirm=False` 只在那一处），
  参数必填与枚举走 `check_call_args`（缺必填在裸工具上是 `TypeError` → 错误帧，生产是「零工具 + 问一句」）。
- **`agent/react_line.py` 拆出 `WorkflowExecutor.run_specs`**：`run(技能, 参数)` 保留「技能 → 工具序列」
  那一步，把「同意闸 → 权限闸 → 引用解析 → 真调用 → checker」整段搬进 `run_specs(规格串列表)`。
  工具臂覆写 `run` 后交同一段执行 ⇒ 两臂的闸门与回执行**逐字同源**（复制一份循环就是复制一份判据）。
- **台账**：`eval/module_baseline.json` 的 `agent/react_arm.py` 由 1249 行 / 40 定义改为
  **1305 行 / 41 定义**（四个接缝的形参与文档）。这一条是手改的、不是 `--update`：只动这一格，
  别的模块的漂移不跟着被吸收。
- **本轮射程如实收窄**（结论的边界，写在这里免得被读大）：只量**只读 + 导航 + 检索**族；写族
  （30 条技能、7 个写参数校正器）不在第一轮。菜单本身**没有按射程收窄**——把 70 个工具并成「几个
  可组合的」本身就是另一种手写分组，那是第二轮。另外三件搬不动的：`complete_when` /
  `reply_contract` / `capability` 是**每条技能各自写的散文**，工具没有「多步任务怎么算完成」这个
  概念；`CAPABILITY_DENIAL_OBJECTS` 按**技能名**索引而本臂的 `workflow` 是派生的 ⇒ gate 的能力
  否认那一族在本臂上**可能不开火**，而 `arm_capability` 不追踪它（要求型族，不是 `forbid_*`）
  ⇒ 是个只能靠人读报告发现的静默洞。
