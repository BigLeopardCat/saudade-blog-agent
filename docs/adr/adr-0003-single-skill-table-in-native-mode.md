# ADR-0003：native 档一轮只发一份技能表（技能块去重）

- **状态**：已采纳（20260927）
- **背景文档**：`docs/native-toolcalls-mainline.md`、`docs/adr/adr-0001-*.md`（接缝在技能级）
- **影响范围**：`agent/skills.py`（`build_planner_context(slim=)`、`_skill_plan_seq`、
  `Skill.planner_contract`）、`agent/native_plan.py`（描述渲染走 `render_tool_marks`）、
  `agent/graph.py`（`_render_planner_prompt(slim_skills=)` 与两个调用点）
- **判据**：`tests/test_slim_skills.py`（纯渲染 + 信息等价 + 契约单一来源 + 驱动真
  `planner_node` 的接线）
- **实测**：`eval/golden_full_run.py` 全量 + 4 条用例的 A/B 五遍对照（见下方「落地实测」）

## 背景

native 档一轮 planner 调用里，**同一张技能表被下发了两次**，形态不同：

| | 来源 | admin 档字数 |
|---|---|---|
| 提示词里的散文菜单 | `skills.build_planner_context`（触发条件 / 参数 / 固定工具序列 / 完成判定） | 17,588 |
| `tools` 数组 | `native_plan.build_tool_schema`（`description` = 同一段描述 + 完成判定，`properties` = 同一份 `skill_param_specs`） | 20,480 |

两者合计 38,068 字 = 单次 planner 调用的绝大部分输入。重复本身不致命，**两份各自漂移**
才致命——本批落地时就抓到一处实证：`build_tool_schema` 里的 `content_query` 描述**原样
漏着**未展开的 `__无参只读工具清单__` 标记，因为角色相关的展开（`render_tool_marks`）
此前只在文本菜单那一路被调用过。

## 决策

**技能块按档位渲染两份形态，native 档只留 schema 里没有的那部分。**

`build_planner_context(role, slim=True)` 删掉**逐字重复的三行**（每条技能的描述行、`参数：`
行、`完成判定：`行），保留：

- **固定工具序列**（`- ops_report：get_server_status({}) → get_service_health({})`）——
  schema 里没有这份信息，而 `content_query` 的 calls 通道要求 planner 写出工具名；
- **导航映射表**（`navigate` 的 target 取值来源，native 通道没有第二份；删了那两条规则变假话）；
- **能力边界的兜底段**（"站内根本没有对应能力时选 chat 并如实说做不到"——native 侧同样需要）；
- **口语变体说明**。

配套一条**唯一渲染器**纪律：技能描述的角色相关展开（`render_tool_marks`）在
`native_plan.build_tool_schema` 里也必须调用一次——两份形态共用同一个渲染器，不手抄第二份。
`slim` 只作为渲染参数。**20261004 追记**：`slim` 的默认值已翻成 `True`，因为
`PLANNER_ENGINE` 拨盘与文本档**已整体删除**——生产只剩 native 一条路，而 native 恒用 slim
（`slim=False` 现在只在 `tests/test_slim_skills.py` 里当对照臂，见 `skills.build_planner_context`
的注）。因此原来那两条"钉住默认档不变"的断言（`tests/test_planner_engine.py`（**已删**）的
"默认档不变"、`test_prompt_prefix.py` 按全量菜单取的 30,000 门槛）**都已随这次改动消失/改口径**：
前者整套删掉，后者门槛按 slim 形态重测后下降（改的是"拿一个已不存在的档位当基线"，不是放宽判据）。

## 依据（"能删"不是文风偏好，是可验证的信息等价）

删之前先回答"这三行删掉之后，模型还看不看得见"。判据在 `tests/test_slim_skills.py` 里逐条
断言、三个档（admin / user / None）各跑一遍：

1. 每个可见技能的**描述**（标记按本轮角色展开后）、**`complete_when`**、**每一个参数名**
   都能在 `build_tool_schema(role)` 里找到——红了就是把那一行加回去，不是改测试；
2. schema 里**不出现**未展开的占位标记（修掉上面那处漂移）；
3. 展开结果**按角色分档**（后台读面只出现在 admin 的 schema 里）——去掉散文菜单不能把
   角色可见性弄丢，那正是 `ADR-0001` 里"不扩权是结构性的"赖以成立的那张表。

**钱不是理由**（刻意写下来，免得下次有人拿它当动机）：被删的这一段落在**稳定前缀**里，
按命中价计费。落地后按 `eval/token_cost_report.py` 实测（planner 单次调用）：

| 指标 | 完整菜单 | slim | 差 |
|---|---|---|---|
| 输入 tok/次 | 17,193 | 14,351 | **−2,842（−16.5%）** |
| 其中命中 tok/次 | 14,683 | 11,744 | −2,939 |
| 其中未命中 tok/次 | 2,510 | 2,607 | +97（噪声带，持平） |
| 命中率 | 85.4% | 81.8% | −3.6pt（**分母变小**，比率自然下降） |

⇒ 省下的正是"被删那一段的 token × 命中价"这一份，未命中侧持平。**此前 ADR 里
≈6,275 tok/次 的估算是拍脑袋的高估（约 2×），以实测 2,939 为准**。真正的收益是
**同一事实只有一个来源**：两份形态的漂移已经在本次落地时真实发生过一次，而漂移的
表现是"模型读到一个不存在的工具名"这种不报错的坏行为。

## 落地实测：两条契约被搬出正文（已修）

**先给结论**：slim 的第一版**真弄坏了两条能力**，A/B 对照实锤，修法是本文档之外新增的
一条纪律（`Skill.planner_contract`）。两条的机制是同一条：**"必须怎么做"的话被从提示词
正文搬进了 `tools[].description`**。

**先说读法**：全量 golden（native 档、134 条、本机）在这台机器上的**单跑结果不可判读**。
同一配置连着跑五次完整菜单（`eval/report/runs/` 的 `20260927_202359` … `20260928_0*`）：

| run | 完整菜单 | 备注 |
|---|---|---|
| 20260927_202359 | 107/134 | 那次 API 事故（403）污染，**作废** |
| 20260927_205535 | 120/134 | |
| 20260927_212527 | 125/134 | |
| 20260927_221315 | 128/134 | 稳定带的上沿 |
| 20260927_232422 | 124/134 | |

稳定带 **120–128**；而且相邻两次**同配置**的红集只重叠 5 条（221315 的 8 条 vs 232422
的 13 条）。slim 第一版 `20260928_002523` 跑 **121/134**，红集与相邻完整菜单 run 之间
9 条新红、10 条消失——**这个换血幅度就是噪声本身的幅度**，单跑差 3–7 条判不出回归。

于是对可疑用例做 A/B：同一批用例在 slim / 完整两臂各跑 5 遍（控制臂靠临时翻转
`graph.py` 的 `slim_skills` 参数实现，`trap ... EXIT INT TERM` 保证退出即还原）：

| 用例 | slim | 完整菜单 | 判读 |
|---|---|---|---|
| `rag_talk_rag` | **0/5** | **5/5** | **实锤回归**（非方差） |
| `followup_named_doc_reread` | 4/5 | 3/5 | 方差带（两臂都时红时绿） |
| `followup_named_doc_title_only_id` | 4/5 | 5/5 | 方差带 |
| `followup_entity_slot_category` | 2/5 | 1/5 | 方差带 |

**机制（一）：成对点名契约**。`content_query` 里有一条"必须怎么做"的契约——问"留言板/说说里有没有人聊过、
写过 X"必须**成对**点名 `list_guestbook` 与 `list_talks`。它原本住在技能 `description`
里，而 slim 把 description 整行从散文菜单删掉、只留 schema ⇒ 这句话落进了
`tools[].description` 的一长段描述中间。模型的失败形态很具体：**技能选对了、第一个数据源
也调了，唯独漏掉这条契约**（只查了留言板）——即"长描述里的必做句"遵守率显著低于它在
提示词正文里的时候。（旁证：`graph.py` 的规划规则 3 里也写着同一句话，但那句是按文本
协议说的——"PARAMS.tools 点名…"，native 档下根本没有 PARAMS 行，接不上。）

**机制（二）：路由契约**。同一段描述里还有一句"规划方式：数据/列表型 → 点名 tools；
知识型/验证型 → 给 calls 定位"，也是"必须怎么做"那一半。它被搬走之后的失败形态更严重：
**模型改用通用知识直接作答、一个检索工具都不调**（`rag_git_svn` 的回复是"Git 和 SVN 最
核心的区别…"，零 `search_notes`/`rag_search`）。A/B 对照（先 3 遍、再单独 6 遍）：

| 用例 | slim（修前） | 完整菜单 | 判读 |
|---|---|---|---|
| `rag_git_svn` | **5/9** | **9/9** | 实锤回归（零工具作答） |
| `multi_step_referent_nav_effect` | 6/9 | 7/9 | 方差带（两臂都时红时绿） |
| 另外 10 条（同一批全量红的用例） | 31/36 | 35/36 | 差集全部落在上面两条上 |

⚠️ **这段契约在提示词正文里本来就有第二份**（规划规则 3 逐字写着"每轮都必须给调用清单，
只允许两种情况留空"+ 通道选型）——**它不够**。技能旁边那一句与七条规则里的一段，模型
遵守率不是一个量级；这就是"有规则 ≠ 有遵守"，也是本条纪律必须写在技能旁边的原因。

**修法**（用户拍板）：新增 `Skill.planner_contract` 字段，"必须怎么做"与"技能是干什么的"
分开写；**两档都渲染**（同一个 `contract` 变量算一次、两个分支共用，避免两档各写一份），
`content_query` 里原本重复两遍的成对点名句因此收敛到这一处（`description` 一句 +
`inputs["tools"]` 一句 → 一句）；那条"规划方式"句同时从 `description` 搬进
`planner_contract`（同一纪律的第二次应用：它同样是"必须怎么做"）。修后复测：
`rag_git_svn`、`rag_arch_check`、`rag_talk_rag`、`guestboard_talk_double_source`
四条在两臂各 5 遍**全绿（slim 20/20 = 完整 20/20）**。

**新纪律（本条比这次修法更重要）**：

> **描述（是什么）归 schema，契约（必须怎么做）归提示词正文。**

- 不要因为"slim 把描述交给了 schema"就把契约也一起交过去——实测遵守率会掉；
- 也不要因为这条纪律就把契约**再抄回** description：抄回去就是两份，两份就会漂，
  而且会重新落回"夹在长描述里"那个形态；
- 判据锁在 `tests/test_slim_skills.py` 的 ②b 两条：契约**两档都渲染**（各一次）、
  且**不出现在** schema / `description` / `inputs` 里。

**修后全量收尾对账**（`20260928_013054`）：**125/134**（0.933），两条实锤回归
（`rag_talk_rag` / `rag_git_svn`）双双转绿。剩下 12 条红里，有 5 条不在**任何**一次完整菜单
run 的红集里出现过——按上面的读法，这种单发红必须先证明它不是回归，于是对这 5 条
（`todo_multi_step_serial` / `dep_search_read_ota` / `own_favorite_add_not_logged_in` /
`task_state_resume_settle` / `account_freeze_grounding_refusal`）又做了一轮 A/B 五遍：

| | slim | 完整菜单 |
|---|---|---|
| 合计 | **21/25** | **20/25** |
| `todo_multi_step_serial` | 4/5 | 5/5 |
| `dep_search_read_ota` | 5/5 | 5/5 |
| `own_favorite_add_not_logged_in` | 3/5 | 3/5 |
| `task_state_resume_settle` | 5/5 | 5/5 |
| `account_freeze_grounding_refusal` | 2/5 | 3/5 |

⇒ **5 条全部判为噪声**（两臂互有胜负、每条差 ±1，样本量 5）。其中
`account_freeze_grounding_refusal` 两臂都时红时绿，是**既有缺陷**（弹卡目标名取到模板占位符
`$name`）而不是本批引入——按纪律**改代码待点名**，此处只记录。

## 后果

**好的**：

- native 档 planner 输入从 38,068 字降到 24,264 字（−36%），其中提示词那一半从 17,588 降到 3,784；
- 技能语义（触发条件 / 参数 / 完成判定）在 native 档**只有一个来源**（schema），不会再各自漂移；
- text 档不受影响（默认参数），回滚 = 这一处调用点不传 `slim_skills`。
- **质量**：修后全量 125/134 落在完整菜单的稳定带（120–128）之内，两轮 A/B（4 条 +
  5 条，各五遍）**没有留下任何一条 slim 特有的红**。两条实锤回归的成因都是"契约被搬进
  schema"，已修 ⇒ 可以推上线。**但记住判据是多遍 A/B，不是单跑全量**：这次若只看全量，
  第一版会被读成"掉了 3 条"（噪声），真回归那两条反而混在里面看不出来。

**代价 / 明确的局限**：

- native 档下模型读技能语义必须走 `tools` 的 `description` 与 `properties`。这是 tool
  calling 的规范读法，但它把一份**隐含依赖**放进了 schema 渲染器：将来谁在
  `build_tool_schema` 里少渲染一段描述，`tests/test_slim_skills.py` 的等价断言会红
  （这就是那条测试存在的意义，别把它当成"重复"删掉）。
- slim 是**文本形状**的改动，不改任何判据（`visible_skills` / `skill_param_specs` /
  `instantiate_plan` 的白名单都不看这段文本）。**质量影响只能靠 golden 判**：native 档
  全量与既有基线对照，重点看技能选择正确率与多步族。

**被否掉的备选**：

- *反过来只留散文菜单、不发 schema*：schema 是 `ADR-0001` 的核心红利所在（闭集参数第一次
  由服务端约束解码），删它等于把接缝退回文本协议。
- *只删描述、保留参数行*：参数行与 schema 的 `properties` 同源，而 schema 那侧更准
  （类型 / 必填 / **enum 闭集**都在），保留一份更弱的副本没有意义。
- *两档都 slim（含 text 档）*：text 档没有 tools 数组，描述就是它唯一的技能语义来源——
  slim 在那条路上是**真的丢信息**。两档的渲染差异是结构性的，不是配置项。
