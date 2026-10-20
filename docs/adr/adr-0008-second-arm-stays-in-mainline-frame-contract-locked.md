# ADR-0008：第二条臂留在主线当开关；帧契约收成一件有锁的产物


> ⚠️ 20261017 起 `skill` 已改名为 **`workflow`**（`agent/skills.py` → `agent/workflow_defs/__init__.py`——20261017 先改名为 `agent/workflows.py`，20261018 批 2 收尾刀又把那个中转层整块删掉（**已删**）；`tests/test_skills.py` → `tests/test_workflows.py`、`tests/test_slim_skills.py` → `tests/test_slim_workflows.py`；计划 IR `SKILL=` → `WORKFLOW=`）。本文档保留**当时的名字**，不回溯改写；读它时把 skill 读作 workflow 即可。
> ⚠️ 20261018 又一批模块已改名（**记录型文档一律保留当时的名字，不回溯改写**）：`agent/absence.py` → `agent/absence_claims.py`、`agent/frames.py` → `agent/frame_contract.py`、`agent/probe.py` → `agent/runtime_probe.py`、`agent/decisions.py` → `agent/deterministic_plan.py`、`agent/entities.py` → `agent/entity_digest.py`、`agent/refs.py` → `agent/arg_refs.py`、`agent/workflow_defs/nav.py` → `agent/workflow_defs/nav_map.py`、`agent/workflow_defs/navigation.py` → `agent/workflow_defs/nav_skills.py`、`agent/workflow_defs/reports.py` → `agent/workflow_defs/report_skills.py`、`agent/context.py` → `agent/prompt_context.py`。**现状地图（每个模块一行）**见 `docs/agent-architecture.md` §2。

- **状态**：已采纳（20261016）
- **背景文档**：[../react-line-experiment.md](../react-line-experiment.md)（这条实验线本身的
  形状与能力台账）、[../问题记录.md](../问题记录.md) §1.59（`_grant_covers` 两次收窄的现场）。
- **影响范围**：新增 `agent/frames.py` 与 `tests/test_frame_contract.py`；
  `agent/react_arm.py`（改为 import 帧构造器，删除本地那六份重复定义）、
  `agent/react_line.py`（`_consent_missing` 改调生产判据、`SkillExecutor` 多收一个
  `state`）、`tests/test_react_arm.py`（三条源码锁跟着结构改）。
  **`agent/graph.py` 与 `server.py` 一个字节都没动** —— 生产路径逐字节不变。
- **判据**：`tests/test_frame_contract.py`（新增，秒级、零网络零 LLM）。它从 `graph.py` /
  `server.py` **现取**键集与帧名，与 `agent/frames.py` 的声明算**双向**差集，并带四条
  反向对照。违反这个决定的形态有三种，各有一条会红：臂少喂一格 ⇒ ①b；声明与源码漂了
  ⇒ ①a/②a；有人在臂里手搓 `updates` 字典 ⇒ ⑥c。行为面另由
  `tests/test_react_arm.py` ⑥b/⑥b′/⑥b″ 锁住（令牌覆盖这一件才放行、换一件就弹卡、
  **同一件做过一次就作废**）。⑥b″ 是专为「补上的那七格」造的**定向用例**：把
  `popup_state` 里的 `executed` 摘掉，它当场变红（其余 100+ 条断言不受影响）——
  也就是说这一格**不是**靠语料偶然覆盖的。
- **触发条件**：不适用（已采纳）。但**本 ADR 的第一条裁决有明确的重开条件**，写在
  《决策·一》的末尾。

## 背景

`agent/react_arm.py` 是**第二份实现**：它用另一套循环（`create_agent` + 收敛判据 + 回执
台账）产出与生产（`graph.py` 手写图）**逐字相同**的帧，好让 `eval/run_golden.py` 能用同一
份语料、同一套断言给两条臂打分（`GOLDEN_ARM=graph|react`）。

**它今天在主线里，而且只是一个开关**：`server._build_arm()` 读 `AGENT_ARM`，
`config/settings.py` 默认 `"graph"`，而生产 `.env` **没设这个键** ⇒ 线上跑的还是手写图。
`exp/react-native-line`(`37e05ef`)、`feat/react-mainline`(`8c5054d`)、
`feat/react-arm-adapt`(`f334e48`) 三条分支用 `git merge-base --is-ancestor` 逐条验过：
**全是 `main` 的祖先**（落后 262/249/56，零独有提交），文档点名的那六个提交也都在 `main`
里，而 `main` 上那份最新。⇒ 别再按分支名找这份代码。

### 两个问题，两件事实

**一、"拆出去独立维护，还是留作臂？"** 留作臂。要拆的不是分支，是那 1025 行适配器。四条
理由都落在这个仓的具体事实上：① 臂的全部价值来自**同仓同提交的 A/B**，对照组一搬走就没
人保证两臂跑同一份契约——本仓 `PYTHONPATH` 那次「实验跑了、其实跑的是主仓那份代码、静默
无报错」就是现场；② 适配器要共享的正是长在 `graph.py`/`server.py` 的帧契约，拆出去等于把
千行接口变成**跨仓契约**，而本仓跨仓契约总共只有四组、全靠人工维护；③ 门禁与语料全在这一
侧（`GOLDEN_ARM`、`eval/arm_capability.py` 的 `comparable` 闸、`runs_react/` 留档 +
`retention_manifest`）；④ 它今天的生产代价是零。

**二、"那它今天到底哪儿不合规范？"** 不是"用了 `create_agent`"，也不是"代码丑"。是
**两套实现共享一份契约，而契约只活在约定里**。三处症状，每一处都在这个仓里付过代价：

1. **消费面靠 grep 数出来。** `gate_node` 从 state 读哪些键，是用 `awk` 切出函数体逐行
   grep 数出来、抄成一张七格的表贴在适配器 docstring 里的，另外三格（它调用的**辅助函数**
   读的）靠人补记。**抄漏不报错**——`.get("X", [])` 把"本线没喂"与"本轮确实没有"读成同一
   件事，闸门只是**少开火**。20261016 用 AST 走完整传递闭包实测：那张表少了一格
   （`task_frames`，`_ledger_in_scope` 的析取里那条腿）；`_popup_state` 那张四格的表少了
   **七格**，其中 `executed` 是 `_grant_covers` "令牌用在那一件上就作废"那一格
   ⇒ 臂的弹卡路径上那条判据**恒不生效**。

2. **判据分家，而且被测试钉死了。** `react_line._consent_missing` 停在
   `not self.grant`，而生产 20261012 已按 `_grant_covers(state, spec)` 收窄两档——两档各有
   一次生产实证（`logs/agent/traces/20261012/091103`：一次点头授权了两件写；
   `20261012T195727`：同一件写静默落库三份）。更坏的是 `tests/test_react_arm.py` ⑥d
   **把那一行字符串钉住了**（`"not self.grant" in _line_src2`）：锁的意图是对的（"别另立
   一套判据"），落点错了（钉了字面而非判据），于是漂移被**永久化**——那条锁越绿，臂离
   生产越远。同一条 docstring 一边自称"逐字同款"、一边把生产那处指到了 `graph.py:8717`
   （真址 12602，漂了约 3900 行）：**指路自己先漂了**。

3. **纯逻辑各写一份**：spec 串的构造、`blocked` 行、收尾正文的措辞、逐字抄的中文串。

## 决策

### 一、第二条臂留在主线，作为 `AGENT_ARM` 开关继续推进

不拆仓、不拆分支。理由见《背景·一》。

**重开条件**（三者任一发生，就该重读这一节再决定）：① 这条臂变成另一条**产品线**
（有独立的上线节奏或独立的用户面）；② 它的依赖开始与主线打架（例如需要钉死另一个
langchain 大版本，而主线要升）；③ 同仓 A/B 的价值消失（主线那套图被整体替换，两份实现
不再共享契约）。

### 二、帧契约收成 `agent/frames.py`，由 `tests/test_frame_contract.py` 守

把"已经存在但没有名字"的那一层写下来：`NODE_FRAMES`（producer 从 `updates` 里现取的四
个键）、`GATE_STATE_KEYS` / `POPUP_STATE_KEYS`（两个消费端的**读侧闭包**，逐条标出来源
行）、四个帧构造器、两路 `messages` 分片、以及 spec 串的唯一构造点。**规矩是"只做形状、
不做判断"**：任何"这一轮该不该弹卡""这句话算不算声称"的判据都留在 `graph.py` 里由两边
**直调**（`_confirm_popup` / `gate_node` / `_grant_covers` 就是这么接的）——抄第二份判据
就是抄一条漂移源，而"人工同步 94 处"正是本仓架构审计的头号结论。

**为什么不把键集做成运行时反射**（比如从 `gate_node` 的源码里现场推）：本仓的判据一律
**静态、秒级、零网络**（`test_state_keys.py` 同族），运行时反射要在图跑起来之后才有值，
而"跑起来"这件事本身依赖 LLM 与数据库 ⇒ 判据会变成偶尔才跑、失败了也说不清。

**为什么声明不能只是声明**：手抄的表漂过两次（见《背景·二·1》），所以 `frames.py` 里
每一条都带来源行号，而 `test_frame_contract.py` 从源码**现取**比对——**多一个红、少一个
红**，并且用四条反向对照（删一格 / 加一格 / 闭包取空 / 闭包被改窄到低于下限）证明它不
恒绿。这一条是"正控不红不算数"那条纪律的又一次应用。

### 三、修那条真分歧（本批唯一的**行为**改动）

`react_line.SkillExecutor` 从收一个 `grant` 布尔改成收 `state`，`_consent_missing` 直调
生产的 `_grant_covers`。`executed` 那一格**必须现算**：生产由 `execute_node` 在请求内累积
（`graph_input` 初值 `[]`），而臂不往那份 state 里回写——本轮真执行过的调用只活在
`executor.calls` 里，两者相加才是"这一轮已经执行过什么"。

## 取舍

**为什么不在这一批把 1025 行适配器换掉**（那才是"去掉手写适配器"的本体）。因为那条路要
重写整份适配器 + 动约 10 处源码文本锁，而它与本批**不是同一个风险等级**：本批能给出
"graph 臂零改动"的**结构论证**（`git diff --stat` 对 `graph.py`/`server.py` 为空 ⇒ 生产
路径在结构上不可能变），批 2 给不出。三批的顺序是串的（契约 → 适配器消失 → 同源），
本批是后两批的共同前提，也是唯一能独立验证为"生产零改动"的一刀。

**为什么 `spec_signature` 不收归。** 计划里原本要把它换成 `graph._spec_signature`，读
`react_line.py` 的既有注后**放弃**：那份 docstring 已经写清了不 import 的理由——
`_spec_signature` 还管着 `$ref` 展开与技能模板，塞进工具包装层会把它的语义面撑大。
**代码里已经记下的理由优先于计划里的推断**；这里如实记下这次偏离。

**为什么 `NOOP_TEXT` 是"抄一份 + 命名"而不是"收归一处"。** 本批不碰 `graph.py`，所以做
不到收归；退而求其次把它命名，让锁能比对（⑦：从 `graph.py` 现取那一句，与 `frames.py`
逐字相等）。收归要等批 2。

## 复验

```bash
cd /home/ubuntu/Saudade-Blog/saudade-blog-agent
.venv/bin/python tests/test_frame_contract.py     # 本 ADR 的判据本体
.venv/bin/python tests/test_react_arm.py          # 行为面（⑥b / ⑥b′ / ⑥b″）
.venv/bin/python -m pytest -q
.venv/bin/python eval/type_gate.py                # 两个数只许缩
.venv/bin/python -m ruff check --select F821 .
.venv/bin/python eval/arm_capability.py --arm react --probe   # comparable 仍须为 True
```

**⑥b″ 是不是恒绿**（这条要能自证）：把 `agent/frames.py::popup_state` 里那一行
`"executed": …` 摘掉再跑 `test_react_arm.py` ⇒ 只有 ⑥b″ 一条红、其余全绿，退出码 1。
它是**定向**造出来的，不是靠语料偶然覆盖 —— 20261016 拿 4 条 confirm 用例跑 react 臂
（`--only`，与 20261012 那份全量留档逐字比对），判定与正文全同、**没有一条**走到那一格。
