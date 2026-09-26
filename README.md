# Saudade Blog AI Agent

博客看板娘“泠月喵”的生产型对话 Agent。项目运行在 FastAPI + LangGraph 上，负责站内内容问答、检索、页面动作、IoT 设备交互、后台管理助手和留言 AI 审核。

项目面向单一业务域，提供确定性执行、权限控制、流式输出和运行时可观测能力。

## 项目定位

当前系统解决的核心问题不是“模型能不能调用工具”，而是：

- 模型输出不稳定时，如何仍然按白名单执行；
- 工具失败、返回空结果、服务不可用时，如何区分事实和故障；
- 用户追问、短应答、多轮任务时，如何避免重复执行或编造执行；
- 写操作涉及后台数据时，如何同时做权限校验、目标校验和用户确认；
- 流式连接断开、LLM 超时、并发升高时，如何限制资源和副作用；
- 最终回复如何与真实工具回执保持一致。

## 架构

```text
浏览器 Live2D 对话面板
        │ SSE
        ▼
Rust 后端：鉴权、MySQL 记忆、SSE 转发、身份断言
        │ HTTP
        ▼
Python Agent :8010
        │
        ├─ planner：唯一的正常路径决策者
        ├─ execute：按计划确定性执行工具
        ├─ planner：读取工具帧，继续、修正或收尾
        ├─ reflector：重复受阻时的受限诊断
        ├─ model：零工具 narrator，只组织最终回复
        └─ gate：确定性事实/声称校验，失败直接 fallback
```

正常任务的主循环是：

```text
planner → execute → planner → ... → model → gate → END
```

只有同一执行项重复受阻时才进入 `reflector`。第一次受阻回到 planner，允许基于真实错误帧修正参数；重复受阻才升级诊断，避免每个普通参数错误都启动额外 LLM 复盘。

## 关键设计

### 1. 受限规划，而不是自由 ReAct

`agent/skills.py` 维护技能注册表。planner 只能选择注册技能、填写参数并生成调用清单；`instantiate_plan()` 再做模板展开和白名单校验。

工具调用有三种入口：

- 无参只读数据工具：显式工具白名单；
- 带参检索和全文读取：参数调用白名单；
- 有副作用的动作和后台写操作：只能由技能模板展开，不能通过普通查询通道越权。

因此 execute 不再判断“要不要调用”，也不重新生成参数，而是执行已经确定的计划。

### 2. 执行回执与受阻状态

每个工具调用经过确定性 checker，结论只有两种：

- `PASS`：进入 `receipts`，成为系统确认过的事实；
- `BLOCK`：进入 `blocked`，不进入跨轮事实记忆。

判据来自工具返回值的 `kind`（`tools/base.py` 的 `ToolResult`）。`kind` 有四个取值，checker 把它们归成上面那两态——**这是两个层级，数目别混着数**（早先的文档既写“工具返回三态”又想把 PASS/BLOCK 算进去，结果两边都对不上）：

| `kind` | 含义 | checker |
|---|---|---|
| `ok` | 正常返回 | PASS |
| `empty` | 工具确实执行了，结果就是空的——空结果是事实 | PASS，照常进回执 |
| `not_found` | 查无此物（上游 404），并点明所查 id 的来路 | BLOCK，原因码 `target_not_found` |
| `unavailable` | 服务不可用——**不是事实** | BLOCK |

上游调用失败返回 `unavailable` 而**不是** `[]`，因此“查不到内容”和“检索服务挂了”在状态上可区分，也不会被 narrator 讲成“站内没有”。工具出口一律用 `_shape(data)` 而不是 `str(data)`（`str()` 作用在 `ToolResult` 这个 str 子类上会把 `kind` 丢掉）。

### 3. 权限与确认分离

权限回答“这个身份能不能调用工具”，确认回答“这一次用户是否同意执行”。

- `Principal` 是调用者身份载体；
- `authz.py` 维护 scope、工具权限声明和角色授予表；
- 后台管理读写使用硬权限闸，不受 shadow 模式放行；
- 写操作确认使用无状态 HMAC 令牌（TTL 10 分钟），绑定用户、会话、签发时刻和具体工具参数，并带一次性 `jti`——同一张令牌重放第二次会被如实拒绝；
- execute 只执行验签后的确认计划，不让模型在确认轮重新解释用户意图。

前端确认卡片只由系统事件 `__CONFIRM__` 触发，模型正文不能凭空制造确认框。用户那一下“确定”进入的是一跳**跳过 planner 的执行轮**：意图已经由令牌固定，不再重新规划。

### 4. 无状态 Agent 与外部记忆

Agent 不依赖进程内会话记忆。Rust 后端从 MySQL 组装并注入：

- 最近对话历史；
- 滚动摘要；
- 页面上下文（当前 URL、当前文章 id、特效与夜间模式等状态）；
- 系统台账：**已执行**（`execution_log` 里 checker PASS 过的回执）与**待主人点头**（`pending_action` 表）两块合一，互斥对照；
- 当前调用者身份和权限上下文。

两块台账合到一个注入块里是刻意的：分成两条时 narrator 有机会把一半读成另一半（把“还没做的”说成“已经办好了”）。点“确定”那一跳里，待办那一半会被改写成“正在执行”，避免它照着“尚未执行”的旧样板文本复述。

摘要由独立任务生成，Agent 回复模型没有记忆写权限，避免模型把自己的猜测污染长期记忆。

### 5. 最终回复不是执行器

`model` 节点不绑定工具，结构上没有 `tool_calls` 通道。它只根据计划、工具帧、回执和叙述规则组织自然语言。

`gate` 负责拦截：

- 工具失败却声称成功；
- 无工具帧却声称查过、读过或执行过；
- 确认式导航却声称已经到达；
- 编造资源 URL；
- 回复中出现伪命令前缀；
- 系统台账已经给出确定性结论却被错误改写。

gate 失败不再把回复丢回 LLM 重考，而是生成确定性 fallback，减少再次幻觉的机会。

### 6. SSE 帧协议（Python / Rust / 前端三端契约）

帧分隔 `\n\n`，带载荷的帧其载荷一律 JSON 编码（防换行破坏帧）。改协议必须三端同步：

| 帧 | 载荷形态 | Rust 侧行为 |
|---|---|---|
| 普通文本 | `data: <json 字符串>` | 转发前端，并累积进本轮 `reply` |
| `__PROCESS__:<步骤>` / `__RESET__:<原因>` / `__CONFIRM__:<json>` | **整帧再 JSON 编码** | 按前缀判定；`__PROCESS__` 只转发不入历史；`__RESET__` 清空已累积的 `reply`（被否定的整轮连同标记都不入库）；`__CONFIRM__` 只转发、不落库 |
| `__PENDING__:<json>` / `__EXEC__:<json>` | 裸帧，但**必须带 `data: ` 前缀** | 收到即**落库**，绝不转发前端（前端无此帧协议） |
| `__SUMMARY__:<json>` | 裸帧 | 独立摘要结果，必须在 `__END__` 之前到达 |
| `__END__` / `__NAV_END__` / `__ERROR__:<json>` | 终止帧 | 见终止帧即停止解析并收尾 |

两个反复踩过的坑，写在这里当护栏：

- **`__PENDING__` / `__EXEC__` 的 `data: ` 前缀不能省**。Rust 的 SSE 解析是 `strip_prefix(b"data: ")`，裸 yield 的帧 payload 是空的、会被静默丢弃——`__EXEC__` 上线首轮就因此从未到达落库分支，而链路两端都以为对方有问题。
- **落库帧要先于终止帧发出**。Rust 见到 `__END__` 就停止解析循环，排在它后面的帧一律读不到。断连同样如此：客户端一见 `__END__` 就断开，所以回复的落库是从生成器生命周期里摘出来单独跑的。

另外，`__CONFIRM__` / `__PENDING__` 的帧体带确认令牌，所以 Rust 在遇到无法解析的帧时那条 WARN 只截前 24 个字符——整帧落进日志文件等于把令牌记在了盘上。

## 目录导览

```text
saudade-blog-agent/
├── server.py              FastAPI 入口、SSE 编排、输入限制、并发和取消
├── agent/
│   ├── graph.py           LangGraph 状态、节点和条件边
│   ├── decisions.py       零 LLM 确定性决策和快道
│   ├── context.py         上下文、工具帧和回执组装
│   ├── skills.py          技能注册表、计划模板和业务映射
│   ├── authz.py           scope 权限模型
│   ├── confirm.py         无状态 HMAC 确认令牌
│   ├── adminops.py        后台目标解析、写操作确认文案和回执摘要
│   ├── refs.py            结构化参数引用
│   ├── moderator.py       留言 AI 审核侧任务
│   ├── summarizer.py      对话摘要侧任务
│   └── entities.py        执行回执实体摘要
├── tools/base.py          48 个工具、工具注册表和 ToolResult 契约
├── rag/search.py          BM25 内存倒排检索
├── eval/                  检索评测、golden 评测、trace 分析与跨源对账
├── tests/                 秒级离线回归测试
└── docs/                  架构、评测可观测性和问题记录
```

技能注册表当前有 29 个技能，覆盖：导航、页面特效、夜间模式、设备显示与查询、内容检索、文章读取、运维和审核报表、后台文章/标签/分类/公告/留言管理、收藏和通知处理、闲聊等。

## 可靠性边界

系统已经具备以下生产防线：

- planner 轮次上限和 LangGraph `recursion_limit`；
- LLM 调用超时、流式空闲超时、总时长上限；
- 线程池和流式并发闸；
- 请求体、字段、历史条数和图片大小限制；
- 客户端断连后的循环级、节点级和逐工具调用检查；
- 服务间身份断言和短时效 JWT；
- 工具返回 `ok / empty / not_found / unavailable` 四态；
- 结构化 trace：节点事件、分段耗时、回执、受阻原因和结束原因；
- L0 离线单测、L1 检索评测、L2 golden 任务评测、L3 跨源对账和夜间自动化。

## 评测

评测分四层（L0–L3），分层口径以 `docs/eval-observability.md` 为准：

```bash
uv sync

# L0 离线、秒级、无 LLM、无网络——整个秒级套件
.venv/bin/python tests/run_all.py

# L0 局部（改技能注册表/计划契约后必跑 test_skills.py）
.venv/bin/python tests/test_skills.py
.venv/bin/python tests/test_authz.py
.venv/bin/python tests/test_confirm.py

# L1 检索基准（recall@k / MRR，直接测线上 rag/search.py，秒级、无网）
.venv/bin/python eval/recall_eval.py

# L2 真实 LLM 任务评测：144 条 golden（其中 3 条会真写生产库，默认不跑、需显式放行），约 25 分钟，按需运行
.venv/bin/python eval/run_golden.py
.venv/bin/python eval/run_golden.py --only <id>,<id>   # 只跑指定用例
.venv/bin/python eval/golden_full_run.py               # 全量跑（与 run_golden 共用判据）

# L3 跨源对账（零 LLM、零网络、只读）：trace ↔ agent.log ↔ 前端上报
.venv/bin/python eval/trace_reconcile.py --days 1

# 产物保留：先审计盘上有没有"没人认领"的产物，再按登记表执行保留期（默认只列不删）
.venv/bin/python eval/retention_manifest.py          # 未登记 0 条 = 每类产物都有主人
.venv/bin/python eval/artifact_retention.py          # dry-run
.venv/bin/python eval/artifact_retention.py --apply  # 真删（夜间脚本已接）
```

三点要知道：

- **L0 适合 CI**（秒级、无外部依赖），push 即拦截；L2 依赖真实模型和外部服务，不进普通 push 门禁——它由 `scripts/nightly_regression.sh` 在夜间跑，联动 L1 与 L3，任一门禁项失败会在磁盘上留一个标记文件由心跳探针带出来。
- **golden 分两类判**：带 `regression` 标签的回归组硬判 100%，能力题按通过率。平均数会把两类红混在一起，严重度不同，所以分开。
- **有几条用例需要“真实身份”**（要以某个 uid 真调上游），由环境变量给出（见下一节）；未设时它们**响亮地跳过并计入报告**，不静默豁免。
- **有意不覆盖三处**（不是缺口，别照着“补上”）：`search_knowledge_base`（`/knowledge` 端点返回空）、
  `get_chat_history`（占位实现）、`device_oled_display`（真硬件副作用，不适合进自动化）。

### 产物保留（谁负责清）

盘上每一类产物都在 `eval/retention_manifest.py` 的登记表里有主：登记 `managed`（谁清、留多久）
/ `frozen`（刻意永不删）/ `open`（登记了、暂未接管）。`audit()` 报出**没登记的条目**——
那就是"缺口"的定义；表里 `managed` 且带 `rule` 的那几类由 `eval/artifact_retention.py`
**真的执行**（默认只列不删，`--apply` 才动手，已接进夜间脚本）。

保留期与路径**只写在表里这一处**，执行者与夜间脚本都不复述——改保留期改表里的常量即可。
trace 与 golden trace 的保留各有其执行者（`eval/trace_retention.py`、
`eval/golden_trace.py::prune`），表里 `rule=None` 各指一处。

这条规矩的由来：同一族坑踩过四次（R2 的保留数、logrotate 的 `rotate 14`、`logs/archive/`、
`eval/report/runs/`），形态都是**「策略写了，但没有任何东西在执行它」**——装饰性配置比没有配置
更坏，因为它看起来是对的。

## 环境变量

配置分两处：`config/settings.py`（pydantic-settings，字段名的全大写即变量名，读 `.env`）与 `server.py` 直接读的几个进程变量。常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `qwen` | 选 `qwen` / `deepseek` / `openai`，各家的 key/base_url/model 各自独立 |
| `QWEN_API_KEY` / `QWEN_BASE_URL` / `QWEN_MODEL` | — | 当前生产提供方的三项（其余提供方同名同形） |
| `LLM_TIMEOUT` | `120` | 单次 LLM 调用超时（秒） |
| `JWT_SECRET` | — | 与 Rust 侧共用，服务间身份断言与短时效 JWT 的签名键 |
| `AGENT_ADMIN_BASE` | `http://127.0.0.1:3000` | 管理读接口的上游地址 |
| `DEVICE_SERVICE_URL` | `http://127.0.0.1:3100` | IoT 设备服务地址 |
| `TRACE_DIR` | 部署方的日志目录 | 对话 trace 落盘目录（默认值绑定部署环境，自建部署须覆盖）。**20260925 起写进 `<TRACE_DIR>/<YYYYMMDD>/`**：枚举由 `eval/trace_files.py` 单点负责（四种读取端共用），保留期由 `eval/trace_retention.py` 执行（>24h 压缩、>30 天删；默认只列不删，`--apply` 才动手） |
| `TRACE_TOOL_RESULT_LIMIT` | 不设 | trace 里工具返回留多长（字符）。不设 = 按工具分档（正文 8000／其余 4000／`rag_search` 全文）；设了就**全局**覆盖（≤0 = 全文）。golden 轮设为 40000 供评审员取材料。任何截断都带标记 |
| `AGENT_REQUIRE_ASSERTION` | `0` | 置 1 时缺失身份断言的请求直接拒绝（默认只记 WARNING，便于滚动上线） |
| `AUTHZ_ENFORCE` | `0` | 权限模型的强制开关 |
| `AGENT_RECURSION_LIMIT` | `30` | 图递归上界，防止幻觉重试循环烧满流式总时长 |
| `AGENT_MAX_BODY_BYTES` | `12 MiB` | 请求体上限，超限 413 |
| `AGENT_MAX_CONCURRENT` | `8` | 每 worker 的流式并发闸，超限 503 |
| `GOLDEN_ADMIN_UID` / `GOLDEN_USER_UID` | — | 只给 golden 里“需要真身份”的用例用；未设则那些用例响亮跳过 |

流式超时（空闲 120s / 总时长 300s）当前是 `server.py` 里的常量，不通过环境变量调。

## 后续维护方向

当前维护重点是稳定性和可维护性：

- 稳定现有主链路；
- 强化目标歧义和短应答的确定性继承；
- 保持 feature freeze，只增加稳定性、安全性、可观测性和回归测试；
- 在保持现有注册表边界的前提下，逐步抽取可复用的工具、技能、权限和评测接口。

跨轮状态结构化已经落地两块：**执行台账**（checker PASS 过的回执落 `execution_log`，读侧去重后注入）与**待办台账**（`pending_action` 表记录弹窗那一轮的结构化提议，含目标与参数）。不单独引入更重的通用 `TaskState`：这两块台账合起来已经覆盖“做过了什么 / 还欠什么”这两个跨轮问题，再叠一层任务状态机只会多出一份需要同步的真源。后续若要扩展，方向是把任务 id 与台账绑定，而不是新增状态层。

## 许可

Apache-2.0
