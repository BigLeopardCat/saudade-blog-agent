# ADR-0012：写操作的重放闸——把「崩溃重放会再写一遍」堵在节点外

- **状态**：**已采纳**（20261016）——落在这支实验分支 `exp/native-checkpoint` 上。
  ⚠️ 它是**采纳 saver 的前提**，不是采纳本身：生产今天照旧没有 saver（见文末《触发条件》）。
- **背景文档**：[../agent-architecture.md](../agent-architecture.md) §4.6（`MemorySaver`
  为什么不承担记忆）、§4.8（那句「线程复用是禁区」的射程被收窄）；
  [adr-0011-graph-holds-context-checkpoint-memory.md](adr-0011-graph-holds-context-checkpoint-memory.md)
  的《触发条件》——本 ADR 就是那条触发条件的兑现。
- **影响范围**：`agent/replay_guard.py`（**新增**，纯件：`ReplayGuard` 协议 / `guard_of` /
  `replay_key` / `WRITE_TOOLS` / `invoke_guarded`）、`agent/replay_guard_pg.py`（**新增**：
  `PostgresReplayGuard` + 自建表 `setup`）、`agent/graph.py`（`execute_node` 里那 4 行
  `try: tool.invoke / except` → 1 行 `invoke_guarded`；`_check_spec` 加 `kind == "in_doubt"`
  一格；顶层 import）、`agent/block_reasons.py` 与 `server.py::_REASON_CN`（新原因码
  `write_in_doubt`，两侧**同话术**）、`agent/prompts.py`（planner 规则：见到 in-doubt 帧
  **先去读一次现状**，不重发）、`agent/checkpoint_arm.py`（`make_guard()`；臂把账放进
  `configurable`，`stream()` 传 `durability="sync"`）、`tests/test_replay_guard.py`（**新增**）、
  `eval/experiments/checkpoint_probe.py`（`--crash-resume --guard` / `--guard-unsafe`）、
  `eval/judge_registry.json`（穷举量）。
  **`server.py` 的链路一个字节没动**——`_build_messages` 的注入循环、`thread_id` 的随机化
  都原样留着（那是**采纳**），这里只多一行原因码中文。
- **判据**：`tests/test_replay_guard.py`（真图四条 + 纯函数那一节；CI 里跑，零 PG 零网络零 LLM）、
  `eval/experiments/checkpoint_probe.py --crash-resume --guard`（真子进程硬杀 + 真 PG saver +
  真账，2× → 1×；**内置反控** = 不带 `--guard` 的那条，另一条反控 `--guard-unsafe` = 占位不提交）。
- **触发条件**：本 ADR **已采纳**，没有搁置条件。它的**下游**才是搁置的那一件——
  **采纳 saver**（ADR-0011 的《触发条件》：三处一起动）。本 ADR 落地后那条条件**已满足**，
  但采纳仍需单独立项与真机读数。

## 背景

### 这一格今天不存在，采纳之后立刻存在

生产今天**没有 saver**：`graph.build_graph()` 不接 checkpointer，`server.py` 每请求生成一个
随机 `thread_id`（§4.6）。所以 `execute_node` 里那次 `tool.invoke` 只会被执行一次，崩溃就是
崩溃，没有"重放"这回事。

采纳（ADR-0011）要接上产线 saver 并把 `thread_id` 改成会话坐标——那时**下一轮请求会从上一轮
那个未完成的 checkpoint 恢复**。而 LangGraph 的恢复单位是 **superstep**：节点内的 state
更新要等节点**返回**才落盘，所以崩在 `execute_node` 中间时**整个节点从头重跑**，而它里面
那次 `tool.invoke` 是**真的写库** ⇒ 同一件写第二次落库。

ADR-0011 把这件事写成硬前置：**批 2 写操作幂等账落地之前不许真开 saver。** 本 ADR 就是这一批。

### 一条必须先纠正的前提：键不在 `checkpoint_id`（我此前记反了）

第一版把幂等键写成 `config["configurable"]["checkpoint_id"]`。**错。** 实测源码
（`.venv/.../langgraph/pregel/_algo.py:745`、`:921`、`:1091`、`:1232`）：节点 config 里
`CONFIG_KEY_CHECKPOINT_ID` 被**无条件写死成 `None`**。我引的 `_loop.py:473/522` 是给
**checkpointer** 拼的 config（`patch_configurable(self.checkpoint_config, …)`），
**不是节点 config**，看错了一层。

superstep 的身份要去**同一处**写进去的 `CONFIG_KEY_TASK_ID`（常量值 `__pregel_task_id`）拿：

```python
task_id = task_id_func(checkpoint_id_bytes, checkpoint_ns, str(step), name, PULL, *triggers)
# _algo.py:616；task_id_func = _xxhash_str（:550）
```

它**完全由输入 checkpoint + 节点名 + step + triggers 决定**。

这个错误为什么危险：拿 `checkpoint_id` 当键会塌成 `(thread, None, idx)`，同一个会话里**每个**
superstep 的第 idx 件撞同一个键 ⇒ 静默压掉整条会话的写；而**只写一件时，"键对了"与"键塌了"
表现完全一样**，正向与反控两条判据**照样全绿**。所以判据里必须有一条**正控**：恢复之后
**再走一个新 superstep、排同一件写，必须仍然执行**。这条才分得开（见《复验》第 ③ 条）。

### 第二条前提：`durability="sync"`，而且它只在崩溃那一刻失效

`task_id` 是从**上一步那个 checkpoint** 算出来的，所以"重放拿到同一个 task_id"的前提是
**那个 checkpoint 真的落了库**。langgraph 缺省是 `durability="async"`：checkpoint 写交给
后台线程、**主循环不等待**（`_loop.py:1202` 原注 "save it, without blocking"）⇒ 进程被
**硬杀**（SIGKILL / OOM / `os._exit`）时，那一步的 checkpoint 可能根本没落盘。恢复时
`__start__` 会**重跑**、写出一个**新** checkpoint，`execute` 的 task_id 因此**变了**，
键也变了——**账被绕过，而表面一切正常**。

实测对照（同一张图、同一个 thread、真 PG saver、子进程 `os._exit`）：

| 崩法 | 子进程 task_id vs 恢复后 | 原因 |
|---|---|---|
| 节点**抛异常** | **逐字相同** | 循环正常退出 ⇒ 后台写被 flush |
| 硬杀（`os._exit`） | **不同** | 那一步的 checkpoint 丢了 ⇒ 恢复时重跑 `__start__` |
| 硬杀 + `durability="sync"` | **逐字相同** | 每步落盘都等（`main.py:3002` 的 `_put_checkpoint_fut.result()`）|

**这条失效判据判不出来**：判据里那个"崩"是进程内 `BaseException`（`except Exception` 拦不住
它 ⇒ 节点不返回 ⇒ superstep 不提交），循环会正常退出、后台写会 flush ⇒ **全绿**。所以闸
必须**自己断言**这一档（节点 config 里的 `__pregel_durability` 如实出现，async/sync/exit
三档都是），档不对就**响亮抛**（`ReplayMisconfigured`）。

## 决策

### 一、名字叫「重放闸」，不叫「幂等账」——因为射程只有一格

**名字就是射程**：它只治「同一个 superstep 重放」。**不治**「planner 在下一轮重排同一件写」
——那是新 superstep ⇒ 新 task_id ⇒ 新键，仍归既有的 `graph._grant_covers`（"令牌只授权
签名体里那一件"）与 `graph._already_done_writes`。两件事正交，谁也别想替谁。
**别把本模块读成"写操作幂等"**：它保证的是 at-most-once，不是 exactly-once。

### 二、键 = `(thread_id, task_id, spec_index)`

`thread_id` 来自顶层注入（采纳后是会话坐标），`task_id` 来自节点 config，`spec_index` 是
`execute_node` 里这一件的下标。性质：

- 重放同一个 superstep ⇒ 输入 checkpoint 没变 ⇒ **同一个 task_id**（这正是要的）；
- 换一轮 / 换节点 ⇒ 新 task_id ⇒ 新键（**不会误压一次合法的重复写**）；
- 顺手带上了节点身份 ⇒ 将来同一 superstep 里多一个写节点也不会撞键。

`merge_configs` 对 `configurable` 是**并集**（`_internal/_config.py:175-177`），所以顶层注入的
`thread_id` / `principal` / `stop_event` 与 langgraph 补的那几个键**同时在场**。

拿不到键源（缺 `thread_id` 或缺 `task_id`）⇒ **响亮抛**（`ReplayUnavailable`），不静默降级：
"分不清这是第几次"就必须**不做**，而"当作没账继续做"会静默双写。

### 三、at-most-once：先占位、后调用、再记结果

```
claim(key, signature)   →  INSERT … ON CONFLICT DO NOTHING   （原子；赢的人才能往下走）
invoke                  →  tool.invoke(args)
record(key, outcome)    →  UPDATE … SET outcome = {text, kind, meta}
```

**显式选"宁可漏写、绝不双写"**，理由是后果不对称：这本账要护的是
`authz._ALWAYS_CONFIRM_TOOLS` 那一族——公告 / 给第三方的站内通知 / 冻结账号 / 禁言。
**双写落在不可撤回、且落在别人身上**（"收不回"正是那张表的 why）；漏写主人看得见、
再说一句就补上。

三种重放结局，**一种都不许糊**：

| 占位行的状态 | 重放时做什么 | 为什么 |
|---|---|---|
| 有 `outcome` | **逐字重建 `ToolResult(text, kind, meta)` 回放**，不调工具 | 那一轮的行为必须与没崩过一模一样 |
| 只有占位（`outcome` 为 NULL） | **不调工具**，返回 `ToolResult(如实帧, kind="in_doubt")` ⇒ `_check_spec` 判 BLOCK | 写**可能发生、也可能没发生**（占位在 invoke **之前**，窗口 = 整个调用时长）。`PASS` 在本仓的定义是"系统确认的事实"，把"未知"记成确认事实就是编造 |
| 占位的 `signature` 与当前 spec 不符 | **响亮抛**，不回放 | 静默回放"另一件的结果"是本设计最坏的失效形态 |

- `outcome` **成功与失败都记**：`tool.invoke` 抛异常时 `out` 是 `__ERROR__` 帧，那一帧就是
  这次的结局；不记它，下次重放会**假报成功**。
- `kind` / `meta` 必须一起存、一起还原：`execute_node` 从 meta 读审计的
  `before/after/change`（`_RCPT_META_KEYS`）与 `is_noop(out_meta)`——只回放字符串会让
  "一次真 no-op"重放成"真改动"，也会让 `write.console` 的回执**丢掉 before/after**（那是
  落库的审计）。
- 新 kind 必须接进 `_check_spec`（否则**静默产回执**）；`block_reasons` 里
  `write_in_doubt` 的 `retry_param=False` ⇒ 摘菜单一轮：**盲目重发一件可能已经写进去的写**
  正是要挡的事。

### 四、覆盖范围从 `authz` **派生**，不手抄

`WRITE_TOOLS` = scope ∈ `authz.CONSENT_SCOPES`（`write.console` + `write.own`）的那批，
**现取**，不另抄一份表（手抄的那份会在新增写工具时**静默漏掉一格**）。

页面写（`navigate_to` / `toggle_effect` / `toggle_dark_mode`）与设备写
（`device_oled_display`）**刻意不进账**，但理由要写对：不是"用户眼前能改回"——那条判据对
flip 型工具是错的——而是这些命令带**显式 `action`/`mode`**（set 不是 flip，
`tools/base.py` 的 docstring 明写"不因重复命令翻转状态"）⇒ **重放幂等**，记账是多余的。
屏显同办，**代价明写**：重放会再刷一次屏，而文案由小 LLM 生成（`_create_display_text`，
temperature 0.7）**非确定** ⇒ 重放后的屏文与首次不同。这一格**明知而不治**。

### 五、自建表 + 主键，不用 `BaseStore`；池必须 autocommit

ADR-0011 当初列的候选里有 LangGraph 自己的 `BaseStore`。**否掉**：`BaseStore.put` 是 upsert，
**没有 compare-and-set** ⇒ 占位不原子（探针 `--concurrency` 已证明同 thread 真并发）。
自建表一条 `INSERT … ON CONFLICT DO NOTHING` 就原子，且自建表同样**不需要产线迁移**
（与 `saver.setup()` 同一先例）：

```sql
CREATE TABLE IF NOT EXISTS agent_write_replay (
  thread_id  text        NOT NULL,
  task_id    text        NOT NULL,
  spec_index integer     NOT NULL,
  signature  text        NOT NULL,
  claimed_at timestamptz NOT NULL DEFAULT now(),
  outcome    jsonb,                  -- NULL = 已占位、结果未知（in-doubt）
  PRIMARY KEY (thread_id, task_id, spec_index)
)
```

**池必须 autocommit**（`checkpoint_arm` 那条池本来就是）：占位若和 saver 共用一个未提交的
事务，崩溃回滚会把占位一起丢掉 ⇒ **账白做**。这一条进契约并**配反控**（探针 `--guard-unsafe`
就是把 autocommit 关掉的那一档，读数应回到 2×）。

### 六、故障方向：两处刻意不对称

- `claim` 故障 ⇒ **响亮抛**（在 helper 的 `try` 之外）：占位**没提交**意味着"我不知道这一件
  是不是已经做过" ⇒ **不能做**。被 `execute_node` 的 `except Exception` 吞掉的话，"账连不上"
  会被误标成"工具报错"（`_check_spec` 判 `error_frame` ⇒ planner 按规则 5 改参重试），
  而真正的风险被藏起来。
- `record` 故障 ⇒ **响亮记日志 + 继续**：写**已经发生**、结果也是真的，不许把成功的一轮丢掉；
  代价只是下一次重放会报 in-doubt（那是诚实的）。

### 七、默认关：不带账时逐字节等于今天

`configurable` 里没有 `replay_guard` ⇒ `guard_of` 返回 `None` ⇒ `invoke_guarded` 退化成
`try: tool.invoke(args) except …`，与不带本模块**逐字节相同**（而且**一个字节都不多查**——
连 durability 都不看）。生产、golden、四条臂**全都不带**这个键 ⇒ 本批对既有读数**零影响**。
带它的是：`checkpoint_arm`（真 saver 那几条臂）与探针。

## 取舍

- **代价 1：每个 superstep 多一次同步提交。** `durability="sync"` 是前提不是选项（见《背景》），
  延迟换正确性。这一条是**明写的**，不是"顺带发现"。
- **代价 2：漏写是可能的。** at-most-once 的必然面（见《决策 三》），换来的是"绝不双写"。
- **代价 3：in-doubt 帧给用户看的是"未知"，不是"办好了"。** 那一轮不进回执，planner 被要求
  先去读一次现状——**比"看起来成功"差，但比"编造一个确认事实"好**。
- **代价 4：并发精度不够。** `claim` 失败就报 in-doubt，不等待、不超时退避。若真有另一个
  worker 正在做同一件，帧文说的是"可能已经生效，也可能没有"——方向安全（**永远不会双写**），
  但精度不够。留给下一批。
- **代价 5：它不治"下一轮重排同一件"。** 射程所限（见《决策 一》），那一格仍归
  `_grant_covers` / `_already_done_writes`。
- **代价 6：`execute_node` 里"唯一持久副作用"这条前提是**人守的**。** 将来谁在
  `execute_node` 里加一处 HTTP 写 / 文件写 / 发帧，必须走 `invoke_guarded`，否则它会重放两遍
  而账看不见它。这条写进了模块头注，**没有机器判据**——记在这里当欠账。

## 复验

- `tests/test_replay_guard.py` —— **主判据是真图**：`StateGraph(AgentState)` + **真**
  `graph.execute_node` + `InMemorySaver`（生产 venv 有）+ 桩写工具（替换
  `graph._TOOL_MAP` 里一件 `write.own` 工具），崩法是桩工具抛 **`BaseException`**。
  四条断言缺一条这个闸就可能是空的：
  ① **正向**：崩 → `invoke(None)` 恢复 ⇒ 桩工具**恰好被调 1 次**；
  ② **反控**：账关掉 ⇒ **2 次**；
  ③ **正控（抓键塌缩）**：恢复后**再走一个新 superstep**、排同一件写 ⇒ **仍然执行**
  （没有这一条，①② 在键塌成 `(thread, None, idx)` 时**照样全绿**）；
  ④ **响亮**：`signature` 不符 ⇒ 抛，且抛之前**一个工具都没调**。
  另有 ⑩ 节：账在场 + `durability` ∈ {`async`, `exit`, 缺键} ⇒ `ReplayMisconfigured`；
  账不在场 ⇒ 逐字节等于今天；真图确实往节点 config 里注入 `__pregel_durability`。
- `eval/experiments/checkpoint_probe.py --crash-resume --guard`（**不在 CI**，要真 PG）：
  真子进程 `os._exit` 崩在工具调用中途 + 真 saver + 真账 ⇒ 恢复那一轮的第 1 件走 in-doubt、
  **同一件写执行 1 次**（期望 1）。**内置反控** = 上面那条不带 `--guard` 的 `--crash-resume`
  （2×）；**另一条反控** `--guard-unsafe`（占位不提交）⇒ 读数回到 2×。
  这一格**必须真起子进程**：进程内 `BaseException` 证明不了"占位已经提交"，而"占位已提交"
  正是这本账的全部价值。
- 既有套件：`tests/run_all.py`、`eval/type_gate.py`（只许缩）、`ruff check --select F821`、
  `eval/wordlist_gate.py`。

## 明确不做（本批）

- **不做采纳**：`server._build_messages` 删注入循环、`thread_id` 改会话坐标、产线
  `build_graph(checkpointer=…)`——**三处一起动**，是下一批（ADR-0011）。
- **不加依赖**：`langgraph-checkpoint-postgres` / `psycopg` **不进** `pyproject.toml` /
  `uv.lock`（那是采纳那一步的事；`agent/checkpoint_arm.py` 已先例性地这么着）。
- **不给 `write.page` / `write.device` 记账**（理由与代价见《决策 四》）。
- **不做并发等待 / `claimed_at` 超时退避**（见《取舍》代价 4）。
- **不改** `_grant_covers` / `_spec_signature` / `_already_done_writes` 的语义。

## 触发条件（下游那一件）

本 ADR 落地 ⇒ **ADR-0011 的《触发条件》已满足**（"批 2 写操作幂等账落地之前不许真开 saver"
这句里的前提现在成立了）。但**采纳本身仍要单独立项**：三处一起动 + 真机读数
（长会话 K=10 的正向收缩、崩溃重放对产线等效 saver、同会话并发两轮），
且生产 `pull`/`restart` 归主人。**本 ADR 的读数不能当作"采纳安全了"**——golden 的真写默认关
（`needs_real_write` 那几条被跳过）⇒ 崩溃重放那一格在 golden 读数里**不存在**。
