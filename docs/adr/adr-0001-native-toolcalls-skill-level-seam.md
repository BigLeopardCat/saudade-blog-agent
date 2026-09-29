# ADR-0001：native tool calls 的接缝开在技能级，不接在工具级

- **状态**：已采纳（20260927）
- **背景文档**：`docs/native-toolcalls-mainline.md`（主线交接，含三条实证与冻结范围）
- **影响范围**：`agent/native_plan.py`（新）、`agent/graph.py::planner_node`、`config/settings.py`

## 背景

planner 此前用**文本计划协议**决策：系统渲染文本菜单 → 模型写五行文本
（`SKILL=` / `PARAMS=` / `TOOLS:` / `NOTE:` / `REPLY=`）→ 正则抠回。那条路把**格式正确性**
押在模型的文本纪律上：实测约 15% 的轮次改用 JSON 对象或带引号键回答，旧正则匹配不到 ⇒
**静默落成 chat**，主人收到一句假的"我做不到"。

主线要把这一步换成 API 的 `tools` 字段 + `tool_calls` 返回。要定的第一件事是**接缝开在哪**：
模型直接点一个**工具**（我们再反查它属于哪个技能），还是直接点一个**技能**。

## 决策

**模型点的 function name 是技能名**，`parameters` 是该技能自己的参数；`instantiate_plan`
照原样展开，技能模板层**仍然是动作工具的唯一入口**。

`tools` 数组的唯一来源：`visible_skills(role)` × `skill_param_specs(skill)`
——**正是渲染 planner 菜单用的同一张表**。

## 依据（为什么"接工具"不成立）

三条都是可当场复核的反证，不是偏好：

1. **7 个参数校正器全部读技能级 `plan_obj["params"]`**：
   `_board_quote_fix`、`_announcement_text_fix`、`_name_target_fix`、`_name_arg_fix`、
   `_target_grounding_refusal`、`_write_target_refusal`、`_ledger_target_refusal`
   （都在 `agent/graph.py`）。传工具原始实参进去 ⇒ 这 7 处全瞎。
2. **技能参数与工具参数不是同一层**：`navigate` 技能的参数叫 `target`
   （`agent/decisions.py`），而它模板里的 `navigate_to` 工具参数叫 `path`
   （`tools/base.py`），且 `path` 由技能自己从 `NAV_MAP` 算出
   （`skills.py::skill_param_specs` 的注专门写了这个"死占位符"陷阱）。
3. **工具→技能反查天生歧义**：`get_moderation_status` 同时出现在两处技能模板里
   （`agent/skills.py` 的后台报表技能与后台首页技能）⇒ 反查不唯一，只能猜。

另有一条形状约束：`_plan_skill`（`agent/graph.py`）与
`confirm.sign(uid, conv_id, _plan_skill(state), picks)`（`_confirm_popup`）配合
`_confirm_grant_plan` 的 `tool ∈ skill.plan` 检查，要求 skill 与工具集自洽。

> **上面只写符号名、不写行号是刻意的**（20260929 批 H 整理）：这份 ADR 原来那组
> `graph.py:NNNN` 锚点在几个月里全部漂走，而其中一条（`_forced_review_fix`）随旧
> 确定性快道整族删掉之后**连名字都不存在了**——一个指错地方的行号比没有行号更坏
> （读的人会以为核实过了）。判据是符号与事实，位置用 grep 找。同族的行号锚点在
> `docs/agent-architecture.md` 等处还有一批，整理不在本批范围。

## 后果

**好的**：

- **爆炸半径收敛到 planner 的决策轮一处**。`state["plan"]` 仍是那段文本、仍经
  `plan_encode` 写入 ⇒ 11 个消费点与 3 处文本子串耦合**一行不用改**。
- **不扩权是结构性的**：schema 与菜单同源，"模型能选的"恒等于"今天这个身份本来就能选的"，
  没有第二份名单可漂移。管理员技能在非 admin 身份下根本不出现在 `tools` 里。
- 工具参数上的 `Literal` 闭集第一次变成**服务端强制的 `enum`**（此前只是提示词里的一句话）。
  目前吃到这个红利的是 `effect.effect/action`、`darkmode.mode`、
  `moderation_report.status` 四处，以及 `content_query` 的两个调用清单。

**代价 / 明确的局限**：

- **动作工具仍然是经技能模板间接触达的**，不是模型直调。native 在这条路上的收益是
  "参数被 schema 约束 + 一次能点多条"，而不是"模型直接操作工具集"。这是刻意换来的：
  技能模板承载了参数校正、确认卡边界、roles 过滤三件事，绕过它等于把这三件事重写一遍。
- `content_query.tools/calls` 两格**推不出形状**（该技能 `plan` 是空列表，调用清单由
  planner 经 PARAMS 注入），因此有一张只有两格的 `_SCHEMA_OVERRIDES` 手写表；
  键集合被 `tests/test_native_plan.py` 钉成字面量，加格必须同改测试。
- native **不治**"多步目标跨轮丢失"（那要会话级任务状态），也不改行为纪律
  （短应答还原 / 全选式短应答 / 授权式 / 写身份防线）——那是**行为**不是格式。

**被否掉的备选**：

- *把 `state["plan"]` 也换成结构化对象*：要同时改 11 个消费点 + 3 处子串 + 16 个测试套件
  约 200 处断言（`tests/test_skills.py` 一处就 135 处）。**不是不做，是排在后面做纯重构**，
  那时有 native 的对照数据兜底。
- *工具级 schema + langchain 的 `convert_to_openai_tool`*：实测可达（langchain_core 1.4.8）
  但形状更差——`Optional[...]` 转成 `anyOf`、无 `additionalProperties: false`、
  54 个工具里只有 4 个带 enum；而技能 schema 从 `skill_param_specs` 手工构造更准。
  同时省掉一个依赖。
