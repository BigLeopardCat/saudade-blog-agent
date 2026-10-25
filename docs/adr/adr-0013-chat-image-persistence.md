# ADR-0013：聊天的图片落库，但**只做展示**——注入给模型的历史仍只有 `[图片]` 标记

> ⚠️ 20261018 又一批模块已改名（**记录型文档一律保留当时的名字，不回溯改写**）：`agent/absence.py` → `agent/absence_claims.py`、`agent/frames.py` → `agent/frame_contract.py`、`agent/probe.py` → `agent/runtime_probe.py`、`agent/decisions.py` → `agent/deterministic_plan.py`、`agent/entities.py` → `agent/entity_digest.py`、`agent/refs.py` → `agent/arg_refs.py`、`agent/workflow_defs/nav.py` → `agent/workflow_defs/nav_map.py`、`agent/workflow_defs/navigation.py` → `agent/workflow_defs/nav_skills.py`、`agent/workflow_defs/reports.py` → `agent/workflow_defs/report_skills.py`、`agent/context.py` → `agent/prompt_context.py`。**现状地图（每个模块一行）**见 `docs/agent-architecture.md` §2。

- **状态**：被 ADR-0022 取代（20261025）；图片落库与展示契约保留，历史入模改为引用与按需回看
- **背景文档**：父仓 `src/routes/chat.rs`（写 `prepare_chat` / 读 `chat_history_handler`）、
  `scripts/migration/pg/0003_chat_image.sql`、agent 仓
  `frontend/public/live2d-widgets/chat-core.js::dbRowToItem`（本仓是看板娘前端的源码仓）。
- **影响范围**：父仓新增一张 PG 表与一个响应字段；本仓只改看板娘前端那一个纯函数
  （`chat-core.js`）与 `boot.js` 的 `VER`。**agent 的 Python 侧一行都不改** ——
  `server.py::_build_messages` 注入的历史仍是 `role`/`content` 字符串。
- **判据**：
  - 父仓 `tests/pg_integration.rs`：dataURL 往返逐字相等 / 删 `chat_history` 行 ⇒
    它的 `chat_image` 行也没了（外键级联）/ `(message_id, seq)` 唯一键挡住重复插入；
  - 父仓 `frontend/tests/chat-core.test.mjs`：DB 行带 `images` ⇒ 走到
    `replaceWithIncoming` 之后**仍在**，且占位回填**没有**触发；
  - **行为判据**：本仓 `tests/` 与 golden 里**没有任何一条**依赖 `[图片]` 标记
    （已 grep 核实），所以这一刀在 agent 侧"零变化"是可验证的，不是声称的。
- **触发条件**（"让模型看历史图"那一半什么时候重开）：**两条同时满足**才重开——
  1. 出现**只有看图才能回答**的历史指代（用户问"我上面那张图里写了什么"而
     助手答不出，且这种问法在真实 trace 里**反复出现**，不是偶尔一次）；
  2. 视觉 token 的成本口径已经量过（20 条窗口 × 每轮最多 6 张 × 每张 1280px
     ⇒ 一轮重发的视觉 token 上限），并且能接受它。

## 背景

用户能在对话里发图（最多 6 张、每张 ≤1MB 的 dataURL，前端压到最长边 1280 + JPEG 0.85）。
**图片本体从来没有落过库**：Rust 把它透传给 Python agent 之后丢掉，只在
`chat_history.content` 末尾拼 `[图片]` / `[图片×N]` 标记。

后果是**聊天记录不完整**：前端在**会话内**能用内存里的 `item.images` 渲染真图，
但刷新/换设备之后只剩一个占位块「ⓘ图片已过期」。库里 1188 条消息中有 7 条带 `[图片]`
标记，那 7 张图**已经永久丢了**（只存在于当时那个浏览器里）。

这件事为什么值得单独写一份 ADR：它是一个**跨两仓、跨语言**的决定，而且"不做另一半"
（不喂模型）比做的那一半更需要写下来 —— 代码里看不见一个"没被写出来的功能"，
于是每年都会有人重新提议一次。

## 决策

**图片落 PG 的新表 `chat_image`，只做展示。**

1. **另开一张表，不给 `chat_history` 加列**。热路径（`GET /api/chat/history` 的 50 条、
   发给 agent 的 21 条）**一个字节都不多读** —— "排除大字段"是结构性的，
   不靠"记得写 `select_only`"这条纪律。
2. **`data` 列存完整 dataURL**（含 `data:image/…;base64,` 前缀），不存 `mime` / `bytes`
   —— 两者都能从前缀与长度推出来，手抄一份就多一处会漂的手工同步。
3. **外键 `ON DELETE CASCADE`**（本仓 chat 家族的第一条 DB 外键）。理由：删
   `chat_history` 的地方有 5 处（`discard_handler` 的全删、每会话 500 条删头、
   断连清理、删会话的事务内、删临时账号），漏一处 = 图片永久孤儿、**没有任何东西会红**。
   外键把"五处人工同步"变成结构性事实。
4. **只做展示**：注入给模型的历史**仍是 `[图片]` 文本标记**。agent 的行为、token、
   成本一个字节都不变。
5. **响应体有字节预算**（`CHAT_HISTORY_IMAGE_BUDGET`，8 MiB，与请求体上限同一个数）：
   一次回 50 条、单条最多 6 张，理论最坏几十 MB；而 `saveHistory` 故意把 `images`
   剥掉不落 localStorage ⇒ **每次刷新都重下**，前端 8 秒超时就静默降级成本地缓存
   （只有一行 console）。预算是安全阀，不是常规路径。

## 取舍

**为什么不是"也喂给模型"（行业默认形态）。** 主流对话产品确实把窗口内的历史图当
vision 块一起重发（ChatGPT / Claude / Gemini 都是），撑得住它的是三件事：客户端先降采样、
前缀缓存把重发从全价降到缓存价、窗口裁剪让老图整条掉出去。前两条本仓**已经有了**，
所以这不是"做不到"。真正的代价在**改形状**：本仓注入历史走的是裸 `role`/`content`
字符串，而 `agent/context.py`（`_msg_text` / `_last_user_msg` / `_prev_user_msg` …）、
gate、`strip_fact_lines` 有十几处按字符串读它 —— 把某几轮换成 content 数组，
是**动图的消息形状**，要逐处核，是另一件事、另一轮验证。故拆开：先把记录补完整
（这一份），再谈"模型能不能回忆"（触发条件见头部）。

**为什么不是 `GET /api/chat/image/<id>` 懒加载 + `<img src>`。** 那是行业里更省的形态
（浏览器原生缓存、懒加载、响应体小），代价是要把 JWT 从 header 挪进 `<img>` 能带的位置
（查询串会进访问日志与 Referer）。主人明确选了 dataURL 进库这一档。

**已知的、接受了的边界：**

- **图片寿命 < 文本寿命**：会话过 500 条之后，旧轮的图被级联删掉，而 `content` 里的
  `[图片]` 标记还在 ⇒ 那些旧轮永远显示"已过期"。这是预期的。
- **中断轮（abort）不清图**：断连清理只删 `role='assistant'` 的行，永不碰用户消息
  ⇒ 用户点了"停止生成"，图仍永久留着。这与"中断保留用户消息"的既有语义一致；
  与 `POST /api/chat/discard` 的全删语义**不对称**，知道就好。
- **老轮次不回填**：这一刀之前的那 7 张图已经没了，无从回填。
- **保留期口径**：`eval/retention_manifest.py` 只审计 `logs/` 树，**PG 表进不去那份清单**
  （与 ADR-0012 的 PG 表同一条欠账）。这一条挂在这里：`chat_image` 的保留期**必须与
  `chat_history` 一起裁**（分开清会把"记录不完整"这个洞重新打开）。

## 复验

上线后人工核对（**不是自动化判据** —— 存量那 7 条旧行永远命中第一条）：

```sql
-- 迁移后**新增**的带标记行若命中，就是"图没存上"（插图失败只会 warn 一行）
SELECT h.id, h.content FROM chat_history h
  LEFT JOIN chat_image i ON i.message_id = h.id
 WHERE h.content ~ '\[图片' AND i.id IS NULL;

SELECT pg_size_pretty(pg_total_relation_size('chat_image'));
```

`[图片×N]` 里的 N 与 `chat_image` 实际行数**应当相等**（插图走一条 `insert_many`，
PG 里多行 INSERT 是原子的 ⇒ 要么全落要么不落，不会有半截态）。
