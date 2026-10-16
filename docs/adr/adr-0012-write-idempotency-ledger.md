# ADR-0012：写操作的幂等账——先记账再动手，至多一次

- **状态**：已采纳（20261016）
- **背景文档**：`/home/ubuntu/native-checkpoint-verdict-20261015.md` §2.6（实测读数）、
  [../../agent/idem.py](../../agent/idem.py)（实现）、`docs/adr/adr-0011-graph-holds-context-checkpoint-memory.md`
  （本份是它的**采纳门**；那份尚未合并，所以这里只写路径不做链接——链一个不存在的文件会让
  `tests/test_docs_links.py` 红，而红的原因与内容无关）
- **影响范围**：`agent/idem.py`（新）、`agent/graph.py` 的 `execute_node`（收口那一处）、
  `agent/block_reasons.py` + `server.py`（新原因码）、`config/settings.py`（一个路径字段）、
  `eval/retention_manifest.py`（登记新产物）。**今天零行为变更**（见《取舍》）。
- **判据**：[../../tests/test_write_idempotency.py](../../tests/test_write_idempotency.py)
  ——① §2.6 镜像（真 `StateGraph` + `MemorySaver` 崩溃+恢复）、② 反控（`NullLedger` ⇒ 写发生
  两次）、③ intent 窗口、④ 惰性（库文件不许被创建）、⑤ 不误合并、⑥ 重放要逐字还回 `meta`、
  ⑦ fail-closed、⑧ 接线锁（`tool.invoke(` 只剩一处 + 指纹先于改参）。
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
3. **协议**：`intent` 行在**远端调用之前**提交（`isolation_level=None`，自动提交），调用返回
   **或抛异常**后改 `done`。留在 `intent` 只有一种成因：进程死在"已经发出、还没拿到返回"
   这段中间。
4. **存储**：agent 自持的 SQLite（`settings.idem_db`，默认
   `/home/ubuntu/Saudade-Blog/logs/agent/idem.sqlite3`）。`key` 是主键 ⇒ 4 个 uvicorn worker
   天然互斥，不需要额外的锁。连接惰性：import 期不开库。
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

## 复验

`tests/test_write_idempotency.py` 全绿，其中两条**反控**必须能红（本仓纪律：正控不红不算数）：
把闸摘掉（退回今天的形状）⇒ ①③⑥⑦ 一起红、① 的读数从 `[0, 1]` 变成 `[0, 0, 1]`；
把收口搬回 `graph.py` ⇒ ⑧ 红。两条都在落地前实测过。

`tests/test_block_reasons.py` 的"不可重试那一族就是这七个"随本份变成**八个**——那条断言的
存在就是为了逼出这次复核：每多一个 `False`，就等于对 planner 说"重试没用"。本份的第八个
是**重试正好是它要防的那件事**，理由写在表里。

## 触发条件

满足**任一条**就该回来重读本份：

- **ADR-0011 真接上 saver**（图自持上下文 + `thread_id` 挪到会话坐标）——那时本份的账本
  **第一次真正通电**，必须同时把 `logs/agent/idem.sqlite3` 的保留策略与 checkpoint store
  **一起**设计（`eval/retention_manifest.py` 里登记的是 `open`/未接管，理由就在那儿）；
- **接线的那一刻先确认 `durability` 档**。`langgraph/pregel/main.py:2618` 的缺省是
  **`"async"`**（`config.get(CONF, {}).get(CONFIG_KEY_DURABILITY, "async")`，docstring 也这么写）：
  checkpoint 写交给后台线程、主循环**不等它**。进程被**硬杀**时上一步可能没落盘 ⇒ 恢复点
  前移 ⇒ 重放那个 superstep 拿到的 `task_id` 可能与崩前那次不同 ⇒ **键变了、账被静默绕过**。
  ⚠️ **本份没有测过这一档**：§2.6 镜像与 `tests/test_write_idempotency.py` 用的是 `MemorySaver`
  （同步、进程内），它在结构上就看不见这件事。⇒ 接线时要么显式跑 `durability="sync"`，要么
  先补一条"硬杀 + 真 saver"的判据。这条前提是 `exp/native-checkpoint` 的 `5afd414` 记下的
  （那一份用 PG 自建表、键取 `__pregel_task_id` 而不是 `checkpoint_ns`），**本份未采纳那份实现**，
  但这条前提与本份的键同源、必须一起验；
- 出现**新的写工具**且它的参数由模型现创（像 `_create_display_text` 那样）——要复核指纹
  仍在改动之前取；
- 账本行里出现**结果超上限未留档**（trace 事件 `idem_result_capped`）——那时重放拿到的是
  降级帧，要重新设计"多大的返回值得留"；
- 有写操作**真的执行了两遍**——那说明键的三个成分里有一个不稳定，先查 `checkpoint_ns`
  在那一版 langgraph 上还是不是纯函数。
