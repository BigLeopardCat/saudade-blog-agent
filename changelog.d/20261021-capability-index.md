- **能力索引 + 能力索引臂（ADR-0015 阶段 2 落地）**：**planner 选能力，索引把那条能力的
  工具面交给执行层**。`AGENT_ARM=capability` 可选，**默认仍是 `graph`**（见 ⑥）。
  ① **新增 `agent/capability_index.py`**：每条技能 → 一行索引 + **这条能力涉及哪些工具**
  + 完成判定 + 回复契约。工具面 = `[n for n, _ in wf.plan] + list(wf.extra_plan_tools)`
  ——与 `workflow_defs._derive_write_workflow_names` **逐字同一条算法**；索引行走
  `tool_index.index_line`（机械切、不改写）。**不另立一张分组表**（重新分组本身就是另一种
  手写分组，ADR-0015 取舍 #4）。
  ② **新增 `agent/capability_arm.py`**：一条回合分**两态**——第 0 轮菜单 = 能力索引，
  模型点一条能力（**只选定、零调用**）；第 1..n 轮菜单 = 那条能力的工具面，模型点真工具、
  执行层逐条真调。两态的判据是同一格 `ctx["caps"]`，菜单变了就把 `ctx["menu"]` 清掉。
  **一个字节的 harness 都不重写**：只换 `react_arm.make_ctx` 的四个接缝，权限闸 / 同意闸 /
  `$tool[i].field` 引用解析 / 生产 checker 的 PASS/BLOCK / 回执 / gate / 零工具 narrator
  全部由那张真图承载（`tests/test_capability_arm.py` ⑤ 有源码锁钉这条）。
  ③ **为什么是这个形状**：工具级菜单 POC（`docs/tool-menu-poc-20261020.md`，0.6725 vs
  0.9825）的三族红由**同一件东西**解释——A 族（13 条，路由）红在菜单是**全部** 70 件工具；
  B 族（8 条，定序）红在 `checker 验收回执缺少工具 X`，而裸工具表里**没有"这条能力该调哪些
  工具"这个要求**；C 族（4 条，检索）红在回复正文缺关键词，而 `complete_when` /
  `reply_contract` 在工具臂上**没有家**。索引项把这三样一起带上，执行层的提示词里有
  「该调的工具都调上」与那两格。
  ④ **射程（如实记边界）**：走 ReAct 的是 `react_scoped` 为真的那些——只读 + 导航 + 特效 +
  夜间 + 设备 + 检索（`content_query` 的工具面按角色现算）；**写族 30 条与 `chat` 继续走
  模板**（`CapabilityExecutor.run` 按名字分派：是能力名走 `WorkflowExecutor.run` 的
  `instantiate_plan` 展开，是工具名直接调），两条路的下游是**同一个 `run_specs`**。
  写族有 9 个参数校正器、28 处读技能级 `plan_obj["params"]` 的落点，硬并进来是另一个量级
  的风险。⚠️ **写族实际不会走到工具态**：`react_scoped(favorite_add) is False` ⇒ 索引态那
  一轮就直接执行完了，菜单不切——这是有意的。
  ⑤ **一处行为差异，如实记**：本臂的 `plan["workflow"]` 是**模型自己选的能力名**（不是
  `tools_arm` 那种从工具名倒推的粗标签）⇒ gate 的能力否认族（`CAPABILITY_DENIAL_OBJECTS`
  按技能名索引）在本臂上**照常开火**，而那正是 `tools_arm` 头注里点名的那个静默洞。
  另一处是代价：**每回合多一轮 LLM 决策**（索引态那一轮只选定、不执行）。
  ⑥ **判据**：新增 `tests/test_capability_index.py`（五组，四条反控实测各红 4/66/130/84 条）
  与 `tests/test_capability_arm.py`（五组，四条反控实测会红）。端到端那条用脚本化假模型
  真跑一遍 `planner → execute → model → gate`，判"第 0 轮零调用 / 第 1 轮绑的是那条能力的
  工具面 / 工具真被执行 / 两轮提示词不同且含能力简报三格"。**离线全绿 = 接线与不变量成立，
  ≠ 读数更好** ⇒ 本臂**没有跑过 golden A/B**，`config/settings.py` 的默认**仍是 `graph`**，
  切它之前必须先按 `docs/agent-ab-method.md` 跑交错 A/B。
  ⑦ **接缝签名扩了一格**：`menu_builder(role, ctx)` / `plan_builder(tool_calls, role, ctx)`
  ——"菜单按轮变"需要一份**每请求私有**的记忆，`ctx` 正好是它（放 `state` 会进 checkpoint、
  放模块级会串请求）。缺省档一格都不读 ⇒ 对已有两臂是纯形参；`_bound_llm` 拆成
  `llm_client` / `menu` / `llm` 三格（**作废由建菜单的那条臂自己负责**，不做"每轮重建再比
  一比"的兜底），`_prompt_of` 的缓存键改跟菜单身份走。缺省臂三格各自只写一次，
  `tests/test_arm_seams.py` 与 `tests/test_react_arm.py` 的缺省档判据钉着这条。
  ⑧ **一处台账动作，如实记**：`eval/module_baseline.json` 里 `agent/react_arm.py`
  （1305→1369 行 / 41→42 定义）与 `server.py`（2829→2835）**手动 `--update`**——长的全是
  接缝那几段说明（"为什么 ctx 那一格必须存在""三格为什么这么分"），而这一刀是**刻意做小**的
  （`react_arm.py` 上另有一条未合并的资产对齐分支，冲突面越小越好）。diff 里只有这两条，
  没有静默放宽别的格子。另外 `agent/planner_llm.py` **没有**进台账：`strip_slots` 一度加在
  那里把它顶到 813（上限 800），改放 `agent/tool_menu.py`（**槽位的挂载点与摘除点住同一个
  模块**）后回到 789。
  ⑨ `eval/golden_arm.py` 注册了 `GOLDEN_ARM=capability`（`KNOWN_ARMS` 多一格），
  `docs/agent-architecture.md` §2 加了两行（`test_module_index.py` ② 判"磁盘 ⊆ 树"）。
