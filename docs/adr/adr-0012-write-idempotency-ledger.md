# ADR-0012：写操作的幂等账——先记账再动手，至多一次

- **状态**：已采纳（20261016）
- **背景文档**：`/home/ubuntu/native-checkpoint-verdict-20261015.md` §2.6（实测读数）、
  [../../agent/idem.py](../../agent/idem.py)（实现）、`docs/adr/adr-0011-graph-holds-context-checkpoint-memory.md`
  （本份是它的**采纳门**；那份尚未合并，所以这里只写路径不做链接——链一个不存在的文件会让
  `tests/test_docs_links.py` 红，而红的原因与内容无关）
- **影响范围**：`agent/idem.py`（新）、`agent/idem_pg.py`（新，PG 那一档；`psycopg` 只住这里）、
  `agent/graph.py` 的 `execute_node`（收口那一处）、
  `agent/block_reasons.py` + `server.py`（新原因码）、`config/settings.py`（一个路径字段）、
  `eval/retention_manifest.py`（登记新产物）。**今天零行为变更**（见《取舍》）。
- **判据**：[../../tests/test_write_idempotency.py](../../tests/test_write_idempotency.py)
  ——① §2.6 镜像（真 `StateGraph` + `MemorySaver` 崩溃+恢复）、② 反控（`NullLedger` ⇒ 写发生
  两次）、③ intent 窗口、④ 惰性（库文件不许被创建）、⑤ 不误合并、⑥ 重放要逐字还回 `meta`、
  ⑦ fail-closed、⑧ 接线锁（`tool.invoke(` 只剩一处 + 指纹先于改参）、
  ⑨ `durability` 自断言（账本在场就必须 `sync`，档不对**响亮抛**）、
  ⑩ 重放的行**必须是元组**（喂 dict 会不报错地回放字面串）、
  ⑪ 依赖面锁（`psycopg` 只在 `idem_pg.py`，且只在 `_shared` 体内懒 import）、
  ⑫ **生产形状**（20261019 批 F 补）：换了 `checkpoint_ns`（= 换了一轮）之后，上一轮**没
  跑完** ⇒ 不执行 / 上一轮**跑完了** ⇒ 照常执行（三条反控：`resuming=False`、
  `NullLedger`、`done` 那一支的两个极性）、
  ⑬ **键的稳定面**（批 F）：换轮 ⇒ 键**相同**；换会话/换指纹/换工具 ⇒ 键变；
  `attempt` 反而必须逐轮变、同一 superstep 内稳定、
  ⑭ **接线锁**（批 F）：`server.py` 里那一格**恰好两处**且走同一个推导，且它**不是**
  state 键（`graph.py` 里不许出现）。
  真机读数（不在 CI，要真 PG）：[../../eval/experiments/checkpoint_probe.py](../../eval/experiments/checkpoint_probe.py)
  的 `--crash-resume --guard` / `--concurrency N --guard` / `--backend-parity` 与两条反控。
- **触发条件**：见文末《复验》。

> **编号为什么跳到 0012**：`0011` 已被 `exp/native-checkpoint` 分支占用（图自持上下文 +
> 真 checkpointer，本份正是它的前置门）。那份尚未合并，所以索引里这里会先空一格——
> 不是漏号。

## 背景

**LangGraph 的 checkpoint 提交粒度是 superstep，不是语句。** 一个节点里连做 N 次写，
进程死在中间、从上一个 checkpoint 恢复 ⇒ **整个节点重放，那 N 次写各执行两遍**。
这不是推演：`native-checkpoint-verdict-20261015.md` §2.6 里两件写（A、B）各跑了两次。

今天这件事炸不了，因为生产**没有 saver**：`agent/agent.py` 调 `build_graph()`，而
`agent/graph.py` 的 `build_graph()` 是裸的 `return g.compile()`——没有 checkpointer 参数，
也没有 store 缝。**但这正是它危险的地方**：接上 saver 那天重放路径**第一次**通电，
而它今天**一行判据都没有**。

为什么不能拿 state 里的东西当账本：节点内的 state 更新要等节点**返回**才落盘
（同文档 §10.6）——崩溃重放时读到的是**上一个** checkpoint 的 state，"这一件我做过没有"
的答案恰好不在里面。`receipts` / `executed` 都在这一侧，用不了。

## 决策

在 `agent/graph.py` 的**唯一**工具收口（`tool.invoke(args)`，全文件只出现一次）上包一层
`agent/idem.py::guarded_invoke`：

1. **闸**：`config["configurable"]["__pregel_checkpointer"] is not None`。没挂 saver ⇒ 直通，
   一行账都不记（**这是"今天零行为变更"的全部依据**）。
2. **键** = `sha256([checkpoint_ns, 工具名, 参数指纹])`。三个成分缺一不可：`checkpoint_ns`
   把同一 thread 的不同轮分开、工具名把同一轮的不同工具分开、参数指纹把同一轮里同一工具的
   两次不同调用分开。用定长摘要而不是"三段用分隔符拼起来"——分隔符一旦长在某个值里，
   一条键会被读成两条（本仓踩过）。
   ⚠️ **`checkpoint_ns` 那一格 20261019 换成了 `thread_id`**（"哪一轮的哪一次"降级成
   `attempt_for` 存进行里）——原口径在生产那条路上恒不命中。见《修法（20261019，批 F）》。
3. **协议**：`intent` 行在**远端调用之前**提交（`isolation_level=None`，自动提交），调用返回
   **或抛异常**后改 `done`。留在 `intent` 只有一种成因：进程死在"已经发出、还没拿到返回"
   这段中间。
4. **存储：两档**（20261017 起），由 `_shared(config)` 按 saver 自己选——**没有第二个
   开关可忘**：

   | 档 | 什么时候用 | 落在哪 |
   |---|---|---|
   | **PG（主）** | 挂上的 saver 是 `PostgresSaver`（池挂在 `.conn`） | **与 checkpoint 同一个库**，表 `agent_write_idem` |
   | **SQLite（降级档）** | 其余（内存 saver / 没挂 saver / CI） | `settings.idem_db`（`logs/agent/idem.sqlite3`） |

   两档的 `key` 都是主键 ⇒ 4 个 uvicorn worker 天然互斥，不需要额外的锁。连接惰性：
   import 期不开库；`psycopg` **只住在 `agent/idem_pg.py`**，没挂 PG saver 的进程一次都
   不碰它（判据：块⑪）。

   **为什么 PG 那一档要落在 saver 那个库**：账与 checkpoint 的保留期必须**同源**——分开清
   会把本份要堵的洞重新打开（清了账、留着 checkpoint ⇒ 恢复重放时账是空的 ⇒ 写落第二遍）。
   落在同一个库里，这件事就变成一句"一起裁"，而不是两条要人工同步的运维规则。

   **失败姿态**：拿到池但建表/断言失败 ⇒ **响亮，且绝不静默退回 SQLite**。`_shared(config)`
   被放在 `guarded_invoke` 那个既有 `try` 里 ⇒ 走既有的
   `unavailable("写操作未执行：幂等账不可用…")` ⇒ `_check_spec` 判 BLOCK `unavailable`。
   "账本不可用 ⇒ 拒绝这次写"是本份的失效姿态，不是"炸掉整轮"（《迁移到 PG》有理由）。
5. **结果未知**：新原因码 `write_outcome_unknown`（`block_reasons.REASONS` +
   `server._REASON_CN` 同批改），**不可重试** ⇒ 进 `blocked`、进过程行，planner 的下一步是
   "先读一次确认到底成没成"。
6. **重放命中**：不重跑，把记下的 `(text, kind, meta)` **逐字**还回下游，并落一条
   `write_replay_hit` trace。**`meta` 必须还原**：命令类写工具的连线命令就住在
   `meta["cmd"]`，只还文本会让重放出来的命令帧被 `cmd_shape` 判 BLOCK——那是一次**凭空**的失败。

## 取舍

**为什么是"至多一次"而不是"至少一次"。** 重复写是**外部可见且不可撤销**的（多发一条站内
通知、多建一个标签、多改一次文章状态）；漏掉一次写是**可恢复且可如实告知**的。代价不对称，
所以取严格的那一侧。

**为什么指纹必须在改动参数之前取。** `execute_node` 在调用前会做两件事改 `args`：解引用
（`resolve_args`）与 `device_oled_display` 缺 `text` 时的**现创**（`_create_display_text`，
temperature **0.7** 的 LLM）。对改后的 args 取指纹，那个工具每次指纹都不同 ⇒ 幂等判据对它
**恒不命中，而且不报错**。取**计划侧**的原文还有个正面理由：它来自 checkpoint 里的 plan，
重放时逐字相同；而解引用后的值依赖上游这一轮的返回，上游若是读工具（不记账）就可能变。

**为什么走错误帧族而不是新增一个 `ToolResult` kind。** `_check_spec` 是一条**显式 if 链**。
新 kind 若忘了加分支会 fallthrough 到"文本非空 ⇒ PASS"⇒ 产回执 ⇒ 进 `execution_log` ⇒
下一轮把"结果未知"当成**已完成的事实**喂给 narrator。错误帧族"忘记接线"的后果是良性的
（落到最后一个 `or` 的 `error_frame`，照样 BLOCK、照样不进回执）。这条论证与
`adminops.py` 里 `policy_refused` 那一节逐字同源。

**失效姿态。** 账本不可用 ⇒ **拒绝这次写**（`tools.base.unavailable` ⇒ BLOCK `unavailable`），
不"降级成不记账"——那等于把保险丝拆了还假装它在。唯一的例外是写**已经发生**之后收尾记账
失败：那时照常返回结果（已经发生的事取消不了，也不能让一条成功的写看起来像失败）。

备选方案与它们为什么不行：

| 备选 | 为什么否 |
|---|---|
| ① 给 Rust 侧加 `Idempotency-Key` 头 | Rust **一个都没读**（全仓零命中）；唯一的 `idempotency_key` 是任务登记路径的 body 字段（`src/routes/chat.rs`）。而且那治的是"客户端重发"，治不了"图自己重放" |
| ② 拿 `execution_log` / `receipts` 当账本 | 它们在 state 里 ⇒ 要等节点返回才落盘（见《背景》）。**幂等键必须是节点之外的一本账** |
| ③ 给 `MemorySaver` 之外的存储 | 本机只装了 `InMemoryStore` / `MemorySaver`；Postgres 是新增常驻服务。**本份不接 saver**，那是 ADR-0011 的活 |
| ④ **本方案** | 一个模块 + 一个收口 + 一个新原因码，零新依赖（stdlib `sqlite3`），今天零行为变更 |

## 迁移到 PG（20261017）

SQLite 那一档是**临时做法**（主人 20261017 明确），本节的活是把它迁到 PG——但迁法不是
"换掉"，而是**加一档**（见《决策 4》）：挂了 PG saver 就落 PG，其余维持原样**逐字节不变**。
这样采纳那天账**自动**变 PG，而 CI（没有 PG）照旧跑得动。

**表**（`agent/idem_pg.py::_DDL`，`setup(pool)` 幂等建表 ⇒ **不需要迁移脚本**，与
`saver.setup()` 同办）：

```sql
CREATE TABLE IF NOT EXISTS agent_write_idem (
  key text PRIMARY KEY, tool text NOT NULL, fp text NOT NULL, status text NOT NULL,
  ts timestamptz NOT NULL DEFAULT now(), text text, kind text, meta text)
```

三处**不报错**的坑，各写一句：

- **`meta` 必须是 `text` 不是 `jsonb`**。SQLite 那格也是 TEXT，`_rehydrate` 拿到的是一段
  JSON 串；`jsonb` 列会让 psycopg 直接返回 dict ⇒ `json.loads(dict)` 抛 TypeError ⇒ 被
  `except (TypeError, ValueError)` **静默吞掉** ⇒ meta 变 `{}` ⇒ 重放出来的命令帧被
  `cmd_shape` 判 BLOCK（一次**凭空**的失败）。列类型是判据的一部分，不是口味。
- **行必须构造成 4 元组**。池的 `row_factory` 是 `dict_row`（langgraph 要的），直接把 dict
  交给 `_rehydrate` 的 `_status, text, kind, meta_json = row` 会**按键名解包** ⇒ 重放回
  下游的是字面串 `"text"`、`kind == "kind"`，**而且不报错**。所以本模块每一次查询都显式
  `cursor(row_factory=tuple_row)`。CI 的块⑩ 只能喂一个**手搓的** dict，证明不了"PG 那条路
  真的会交元组"——那一条由 `--backend-parity` 在真机上钉。
- **`autocommit=True` 是硬要求**，而真实理由**不是**"不自动提交就不提交"：实测
  `psycopg_pool` 里 `with conn:` 成功路径会走 `psycopg.Connection.__exit__` 的 `commit()`，
  非自动提交**也会**提交。差别是**提交点**——自动提交让占位在**语句返回时**就落库，非自动
  提交的提交点在**块退出**，而"先记账再动手"要求占位在**远端调用之前**真的落盘。
  `ensure_autocommit(pool)` 读 `pool.kwargs` 断言这一条，假就响亮抛。

**清场口径变了**：键是 sha256、表里**没有 `thread_id` 列**，所以没有 `DELETE` 可做。探针
改成**每次用唯一 thread 名**（`uuid` 后缀）⇒ 新的 `task_id` ⇒ 新的键 ⇒ 一片干净的行；行数
判据也改成**按那一个键**查（这张表在 saver 那个库里是共用的）。

**保留期**：`eval/retention_manifest.py` 那条 `agent-idem-ledger` **留着**（降级档仍会长在
`logs/` 下），但 PG 表**记不进**那份清单——它只审计 `logs/` 树。⇒ 口径写在这里：
**PG 表必须与 checkpoint store 的保留策略一起裁**，分开清会把本份要堵的洞重新打开。
这是一条**欠账**（采纳那批要落一个能审计 PG 表的保留清单，或把 checkpoint 与账的清理写成
同一条脚本）。

## 复验

`tests/test_write_idempotency.py` 全绿（十一块），其中几条**反控**必须能红（本仓纪律：
正控不红不算数）：把闸摘掉（退回今天的形状）⇒ ①③⑥⑦ 一起红、① 的读数从 `[0, 1]` 变成
`[0, 0, 1]`；把收口搬回 `graph.py` ⇒ ⑧ 红；把 `durability` 档换成 `async` ⇒ ⑨ 红；
把 PG 那边的行改成 dict ⇒ ⑩ 红；把 `psycopg` 提到 `idem.py` 的模块级 import ⇒ ⑪ 红。
都在落地前实测过。

真机读数（要真 PG，不在 CI）——**每一条都配了会红的反控**：

| 探针 | 读数 | 反控 |
|---|---|---|
| `--crash-resume --guard`（真子进程 `os._exit`） | 同一件写 **1 次** | `--guard-unsafe`（占位留在一笔没提交的事务里）⇒ **2 次** |
| `--crash-resume --guard --prod-shape`（恢复那一次传 dict = **生产每轮的形状**） | 同一件写 **1 次**（20261019 批 F 起；改前是 2） | 同一条命令里那一格**干净轮之后重复同一件写** ⇒ **2 次**（合法的重复照常执行——没有它，"读 1"可能只是账变成了从不执行） |
| `--concurrency 4 --guard`（4 进程抢同一个未完成的 superstep） | **1 次**写、恰好 1 个真调工具、账里恰好 1 行、`不可用 0/4` | `--guard-unsafe`（先 SELECT 再 INSERT、不看有没有插进去）⇒ **4 次** |
| `--concurrency 4 --guard-slow`（把 in-doubt 窗口撑到 2s） | 后到的**全部**读 in-doubt ⇒ 1 次 | 同上 |
| `--backend-parity`（两档跑同一份 3 件写 × 6 步，含批 F 的换轮两支） | 18 步读数**逐字相等**，两档都交元组 | —— |

`--guard-unsafe` 那一格（并发）**必须在 SELECT 与 INSERT 之间停 0.5s**，否则读数是**抖的**：
PG 的自动提交立刻可见，某个 worker 的 SELECT 只要晚于别人的 INSERT 就**真的看到那一行** ⇒
读数是 3 而不是 4（20261017 实测）。那不是"反控不成立"，是**仪器没量准**——被量的是非原子性，
"谁先谁后"是采样运气。撑开窗口之后连续三次都是 4。

`tests/test_block_reasons.py` 的"不可重试那一族就是这七个"随本份变成**八个**——那条断言的
存在就是为了逼出这次复核：每多一个 `False`，就等于对 planner 说"重试没用"。本份的第八个
是**重试正好是它要防的那件事**，理由写在表里。

## 口径更正（20261019）：上面那张真机读数表**只在续跑路径上成立**

上表每一行都是真的，但**每一行量到的都是 `stream(None, config)` 那条路径**——也就是
"接着跑没跑完的任务"。**而生产不走那条路**：`server.py` 每轮传的是 **dict 输入**
（`graph_input(...)`，`server.py` 的两处 `_agent.stream`），langgraph 于是**丢弃**上一份
checkpoint 的未完成任务、**从 START 重启**（`langgraph/pregel/_loop.py:977-979`，原话
`# discard any unfinished tasks from previous checkpoint`；`_first` 的 `is_resuming`
判据在同文件 861-867）。重启 ⇒ 全新的 checkpoint 血缘 ⇒ `checkpoint_ns`（= 本份的键的
身份成分）**逐字变了** ⇒ 键变 ⇒ **账认不出同一件写**。

真机读数（20261019，`eval/experiments/checkpoint_probe.py --crash-resume --guard`
加不加 `--prod-shape`，**同一份夹具、只差"恢复那一次传什么"**）：

| 恢复那一次传什么 | 两遍的 `checkpoint_ns` | 同一件写执行了几次 |
|---|---|---|
| `None`（续跑 = 上表那一档） | 逐字相同 | 1（账压住了） |
| **dict**（= 生产每轮的形状） | **逐字变了** | **2（账没压住）** |

⇒ **本份的保险丝在生产那条路上恒不命中。** 三处连带的口径要跟着读：

1. `agent/idem.py` 头注《幂等键是什么》那张三条实测表，同样只在续跑路径上成立；
2. `tests/test_write_idempotency.py` 块① 的 `invoke(None)` 量的是**续跑那一格**——
   它仍然有效，只是**不是生产那一格**（那一格的读数在探针的 `--prod-shape` 档）；
3. 本节《触发条件》里那张 `durability` 三行对照表（`sync + 硬杀 ⇒ 逐字相同`）**也是
   续跑路径上的**。

**这是已知缺口，不是本份被推翻**：账的两阶段协议、`intent` ⇒「结果未知」那一支、
`require_sync_durability` 那条自断言，在**续跑**这条路径上都成立且仍然必要。缺口是
"生产没有走那条路径"，于是**没有任何东西读到那本账**（产线 `agent_write_idem` 表
至今一行都没有）。影响面被同意闸收窄了一半——最危险的那批写（`admin.console` /
`write.console`）在 `_HARD_SCOPES` + `CONSENT_SCOPES` 里，重做要重新弹卡、用户看得见；
**但"已经发出去了、没拿到返回"那一支因此无声**：用户看到一张新的确认卡、点确定，
那件写就真做了第二遍，而他永远不会被告知"上一次结果未知"。

**修法未定**（两条候选，都要先有读数再拍）：① 把键换成**跨重启稳定**的身份
（代价：合法的重复「再显示一次」会被吞，要另开显式通道）；② 让生产走**续跑**路径
（下一轮若 `next` 非空就先续跑完，代价：崩溃与取消留下的形状一样、得先把取消改成终局态
才能区分）。触发条件：**出现一次"写真的做了两遍"**，或**要给某件不可逆的写开直通**。

## 修法（20261019，批 F）：键降成「会话」，另存「哪一轮的哪一次」

**选了候选①，但把它的代价从根上绕开了**——代价本来出在"键少了轮次这个成分，于是主人
明确再要一次也会被吞"，而"主人又要了一次"这件事**是可判的**：上一轮**跑完了**就是又要
一次，**没跑完**就是那一轮的续。判据是图状态的 `next` 非空（上一轮正常收尾为空，崩溃/
被取消留下的非空——批 E 第 ⑤ 节实测记过）。

### 两格拆开

| 角色 | 取值 | 为什么 |
|---|---|---|
| **键** `idem.key_for` | `sha256([thread_id, 工具名, 参数指纹])` | `thread_id` 是 `conv_<会话 id>`，**跨重启逐字不变** ⇒ 账在生产那条路上第一次认得出同一件写 |
| **这一次尝试** `idem.attempt_for` | `sha256([checkpoint_ns, 参数指纹])` | 同一 superstep 的重放逐字相同、换一轮（或同一轮的下一个 superstep）就变 ⇒ 分得出"重放"与"主人又要了一次" |

`attempt` 存在**原来的 `fp` 列**里 ⇒ **表结构、列名、主键、`ts`、保留期全不动**，两档后端
（SQLite / PG）只是换了个值的算法。

### 分流（`claim` 的四支）

| 情形 | 行为 |
|---|---|
| 插进去了 | `new` ⇒ 执行 |
| 行在，`行.fp == attempt`（**同一 superstep 的重放**） | 今天的行为**逐字不变** |
| 行在，换了 attempt，**`resuming`** | 那一行就是上一轮的尝试 ⇒ `done` 重放 / `intent` 判结果未知 |
| 行在，换了 attempt，**非 `resuming`** | 主人明确再要一次 ⇒ **一次原子的 UPDATE** 把那行改回 `intent` 并换 attempt ⇒ 当新的一件执行 |

`resuming` 由**服务端**算：`server._idem_resuming(thread_id)` ⇒
`agent/checkpoint.py::thread_is_resuming`（读 `get_state().next`）。**缺省 `True`**——
读不到那一格就按"上一轮没跑完"办，失效姿态落在**多拦一次**（主人看得见那条帧）而不是
**静默放过一次重做**（看不见）。两条路（`/chat` 与 `/chat/stream`）都要给这一格，判据
`tests/test_write_idempotency.py` 块⑭ 按源码锁钉。

### 为什么**不**选候选②（让生产走续跑）

它要先把"崩溃"与"被主人取消"分开，而**这两者在 checkpoint 里留下的形状一模一样**
（`AgentCancelled` 不写 state，`end_reason` 只进日志与 trace）——也就是要先造一个终局态
标记。本修法**不需要**这个区分：取消是**协作式**的（`_stopped(config)` 在调用**之前**
检查），取消掉的那一轮写的行是 `done` 不是 `intent`，重放它**恰恰是对的**（那件写已经
发生了）。另外候选②会改掉"每一轮"的语义（新消息要等上一轮续完），而本修法一个字都不改
轮次语义。

### 这一版**不**做的事（别把它读成"重放这条路已经全干净了"）

1. **同一会话并发两轮**（前端有 busy 锁，理论上到不了）时，后到那轮会把先到那轮的
   in-flight `intent` 读成"结果未知"。窄，且回答诚实，如实记着。
2. `intent` 那一行的"释放"靠**下一轮跑完之后**（`resuming` 翻回 `False`）——也就是说主人
   被告知"结果未知"之后，**再要一次就会真做**。这是刻意的：一个永久拦着、没有出口的
   死锁比"已经告知过的风险"更坏。
3. 键里**没有** `thread_id` 之外的轮次坐标 ⇒ 表里**仍然没有** `thread_id` 列，`DELETE`
   仍然做不了（清场口径与《迁移到 PG》那一节写的**一样**，没有变）。

## 触发条件

满足**任一条**就该回来重读本份：

- **ADR-0011 真接上 saver**（图自持上下文 + `thread_id` 挪到会话坐标）——那时本份的账本
  **第一次真正通电**，必须同时把**保留策略**与 checkpoint store **一起**设计：PG 那一档
  （采纳后就是它）表在 checkpoint 那个库里，口径见《迁移到 PG》；`logs/agent/idem.sqlite3`
  是降级档，`eval/retention_manifest.py` 里登记的是 `open`/未接管，理由就在那儿；
- **接线的那一刻先确认 `durability` 档**（✅ 20261017 **已验**，见下）。`langgraph/pregel/main.py:2618`
  的缺省是 **`"async"`**（`config.get(CONF, {}).get(CONFIG_KEY_DURABILITY, "async")`，docstring
  也这么写）：checkpoint 写交给后台线程、主循环**不等它**。进程被**硬杀**时上一步可能没落盘
  ⇒ 恢复点前移 ⇒ 重放那个 superstep 拿到的 `task_id` 可能与崩前那次不同 ⇒ **键变了、账被
  静默绕过**（读数回到 2×）。实测三行对照：

  | 档位 / 收场方式 | 恢复后 `__pregel_task_id` |
  |---|---|
  | `sync` + **硬杀** | 与崩前**逐字相同** ⇒ 账认得出来 |
  | `async`（缺省）+ **硬杀** | **变了** ⇒ 键变 ⇒ 账**被静默绕过** |
  | 任意档 + 进程内抛异常 | 不变（异常路径本来就会刷盘） |

  ⚠️ 这张表**也是在续跑路径上量的**（见《口径更正（20261019）》）：生产形状下
  （每轮 dict 输入 ⇒ 从 START 重启）**任意档位都会变**，因为血缘本身就是新的。

  这条断言**搬进了代码**：`agent/idem.py::require_sync_durability`（账本在场而档不是 `sync`
  ⇒ **响亮抛**，且抛在 `try` 之外、`is_write` 之前——配置错不该被降级成一条回执）。
  判据：CI 的块⑨ + 真机的 `--crash-resume --guard`（那一格两处 `stream` 都是 `sync`；
  把任一处改回 `async` 它就会红——**这正是当初"要么显式跑 sync、要么补一条硬杀+真 saver 的
  判据"那条要求的兑现**）。
  ⚠️ 这条前提最初是 `exp/native-checkpoint` 的 `5afd414` 记下的，那一份的实现（PG 自建表、
  键取 `__pregel_task_id`）在 20261017 那批里**已删除**（两份实现收敛成一份），但这条前提
  与本份的键同源，随本批一起验过了；
- 出现**新的写工具**且它的参数由模型现创（像 `_create_display_text` 那样）——要复核指纹
  仍在改动之前取；
- 账本行里出现**结果超上限未留档**（trace 事件 `idem_result_capped`）——那时重放拿到的是
  降级帧，要重新设计"多大的返回值得留"；
- 有写操作**真的执行了两遍**——那说明键的三个成分里有一个不稳定，先查 `checkpoint_ns`
  在那一版 langgraph 上还是不是纯函数。
