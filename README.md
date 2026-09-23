# Saudade Blog AI Agent（泠月喵）🐱

博客看板娘"泠月喵"的对话 Agent 后端（FastAPI，:8010）。它真实跑在生产环境里——宿主博客是
一套 Rust 后端（:3000，鉴权/DB/SSE 编排）+ React 前端（含 Live2D 看板娘对话面板），
**那部分是私有仓库**（另一个仓库、不在本仓库范围内）。
本仓库负责：对话生成、博客内容查询、导航/特效/夜间模式命令、IoT 设备（ESP32 OLED）屏幕显示。

**核心定位：手写 LangGraph 图（planner ⇄ execute 决策-执行循环 → model → gate）+ 技能注册表受限规划，
20260903 起 planner 全权（自由 ReAct / reflector / REVISE 已废除）；对话记忆全部外置 MySQL（agent 无状态，每请求独立线程）。**

---

## 🏗️ 架构一句话

```
浏览器(autoload.js)
  → POST /api/chat/stream (SSE)          [nginx → Rust :3000]
  → Rust: 鉴权JWT → 消息入库 → 组装请求体（20 条历史 + 摘要 + 状态）
  → Python Agent :8010: 手写图（planner ⇄ execute 决策-执行 ≤4 轮 → model 叙述 → gate 检查/fallback）
  → Rust: 逐帧转发 + 流结束存回复 + __SUMMARY__ 帧摘要入库
  → 浏览器: 逐帧渲染 + 命令帧执行（导航/特效/夜间）
```

详细架构：[docs/agent-architecture.md](docs/agent-architecture.md)（全链路：时序、记忆、工具、防幻觉、超时、部署）。
评测与可观测设计：[docs/eval-observability.md](docs/eval-observability.md)。
RAG 检索设计总结：[docs/rag-design.md](docs/rag-design.md)（历史设计记录，检索现状见 [rag/search.py](rag/search.py) 头注释）。

---

## 📁 目录结构

```
saudade-blog-agent/
├── server.py               # FastAPI 入口：/chat、/chat/stream、/review（留言 AI 审核）、/graph/query（图谱检索）、/health
│                           #   流式编排 + 身份断言验签 + 输入限额/体积闸/并发闸
├── agent/
│   ├── graph.py            # ★ 手写 LangGraph 图（1648 行）：planner(唯一决策) ⇄ execute(确定性执行) → model(零工具叙述) → gate(确定性检查)
│   ├── decisions.py        # ★ 确定性决策层（523 行，零 LLM）：快道/动作意图扫描/检索候选裁决/终局计划
│   ├── context.py          # 上下文组装（210 行，纯函数叶子层）：消息文本/页面上下文/工具帧摘要/回执摘要
│   ├── skills.py           # ★ 技能注册表：8 技能静态定义 + NAV_MAP 导航映射（业务唯一数据源）
│   ├── agent.py            # create_agent：手写图入口（build_graph，planner ⇄ execute → model → gate）
│   ├── memory.py           # MemorySaver 兼容存根（实际不承担记忆，见文档 §4.6）
│   └── prompts.py          # BLOG_ASSISTANT_PROMPT：猫猫女仆人设 + 叙述规则（model 零工具 narrator 用）
├── rag/                    # ★ RAG 检索管线（20260830）：词法 2/3-gram BM25 内存倒排索引，
│   │                       #   语料=线上可见文章（20260901 净化：说说/留言/公告移出检索池；
│   │                       #   20260912 修：翻页累加，不再吃接口默认 pageSize=6 只索引 6 篇），
│   │                       #   10 分钟懒刷新；检索只定位（候选 ID+标题+分），解读走 get_article_detail 全文
│   ├── search.py           # RagIndex + search()：检索 eval 直接测本实现（评测即线上行为）；
│   │                       #   **索引不可用时返回 None**（区别于"没命中"的 []，工具层据此标 unavailable）
│   └── wordgraph.py        # 另一条独立检索线：词向量图谱的查询侧（1024 维余弦，纯 stdlib array+map，
│                           #   无 numpy；建图是离线脚本 scripts/build_word_graph.py）
├── config/settings.py      # pydantic-settings 配置
├── models/llm.py           # LLM 工厂：provider 三选一（qwen/deepseek/openai）
├── tools/base.py           # 22 个 @tool 工具 + _TOOL_REGISTRY + IoT JWT 代签 + 显示幂等去重
├── utils/                  # logging（trace_id/日志）+ trace（对话 trace 落盘）+ tts（未启用）+ helpers
├── eval/                   # 评测：golden set（78 条）+ run_golden.py（L2 任务级，真实 LLM）
│   │                       #       + golden_case_runner.py/golden_full_run.py（进程隔离跑法）
│   │                       #       + recall_eval.py（L1 检索：recall@k/MRR，直接测 rag/search.py）
│   │                       #       + trace_metrics.py/trace_alert.py（trace 指标与语义巡检）
│   │                       #       + golden_draft.py（真实 trace 现场 → golden 用例草稿，供人审后入库）
├── scripts/                # agent_metrics（质量指标）+ nightly_regression（cron 每 4:00）+ sticker_smoke（贴纸冒烟）
├── tests/                  # L0 单元级（16 个秒级套件 + 统一入口 run_all.py：技能注册表/plan 契约、权限模型、
│                           #   确认令牌与弹窗、写面、侧任务、分节、报表、加固、协作取消、实体摘要、自己的
│                           #   数据、golden trace、判据自测、回归组重跑、跨源对账）
└── docs/                   # 架构文档 + 评测可观测设计
```

---

## 🚦 快速开始

```bash
cd saudade-blog-agent
uv sync                       # 创建 .venv + 安装依赖
cp .env.example .env         # 填入 LLM API Key（生产：qwen → qwen3.8-flash；代码默认值见下方配置表）
```

**以服务方式运行（生产形态）**：systemd 常驻服务（2 workers，`Restart=always` 崩溃自愈，
`TimeoutStopSec=120` 优雅停等在途对话——SIGTERM 后在途对话自然收尾再停，不硬掐）。
**push 不等于部署**：改动要重启该服务才生效（具体服务名与运维命令属私有运行簿，不进仓库）。
健康检查：`curl http://localhost:8010/health`（agent_ready）。

**日志**（20260830f 日志分组）：`logs/agent/agent.log`（systemd StandardOutput/Error append）
+ `logs/agent/traces/`（对话 trace JSON，路径由 `trace_dir` 配置）——排障直接读 trace 的分段
耗时（planner/execute/reflector/model/gate 五段，reflector 未触发时无该段），不必翻日志。

---

## 🧠 关键机制

| 机制 | 说明 |
|---|---|
| **技能注册表 + 受限规划（20260903 planner 全权）** | 固定流程任务（导航/特效/夜间/设备显示/设备查询）落地为 `skills.py` 静态技能定义（8 技能 + NAV_MAP）；planner = **唯一决策者**：选技能 + 填参数（`SKILL:/PARAMS:` 结构化输出）并**每轮产出调用清单**，TOOLS 行 = "执行清单"而非"允许名单"——execute 确定性逐条执行，"点名了却不执行"在结构上不存在。**内容问答（content_query）**：planner 经 `PARAMS.tools`（无参只读点名，`_EXPLICIT_TOOLS` 白名单）或 `PARAMS.calls`（带参检索调用，`_CALLABLE_QUERY_TOOLS` 白名单）给调用清单 → instantiate_plan 白名单校验展开进 TOOLS 行 → execute 必执行（检索定位 → 看帧 → 读全文/换词再搜/收尾的多轮由 planner 驱动）；**自由 ReAct 已废除** |
| **确定性 gate（取代 reflector/REVISE）** | 执行正确性不需要检查（execute 是确定性执行器）；gate 只兜 model 叙述失真与计划注记不遵守：声称检查作用域收窄（宁可漏拦不可误伤），发现问题 **validate→fallback 直接收尾**（`[Fallback 决定]` + fallback_text，server 发 `__RESET__` 以如实文本替换最终回复）；**无 REVISE 重考轮 / 无质检预算 / 无 LLM 质检** |
| **记忆外置 MySQL** | 每请求独立线程（无 checkpointer），连续性靠 Rust 注入 20 条历史 + 滚动摘要；摘要由后端**独立任务调用**生成（`_summarize_dialogue`，与回复解耦，模型对记忆无写权限，防摘要幻觉污染）；流式经 `__SUMMARY__` 帧、非流式经 `new_summary` 字段入库 |
| **显示类请求保障链** | 意图识别确定性（显示快道 `_DISPLAY_FAST_RE` 强模式或 planner 决策）→ 计划模板固定展开 `device_oled_display`（屏幕文案由 execute 内小 LLM 结合对话创作，不进 planner 文本通道）→ execute 确定性执行（有执行必有帧）→ model 零工具叙述（无帧声称"已显示"结构上不可能，叙述失真由 gate 兜底）+ 30s 幂等去重；曾用后端强制路由（_force_display）先执行，20260828 影子系统事故（与主链路并存致决策漂移）后**移除**——20260903 起并入 planner 全权的单一确定性执行路径 |
| **SSE 帧协议** | JSON 编码 + `\n\n` 分隔；文本帧/命令帧/`__PROCESS__`（过程轨迹）/`__RESET__`（20260903 起仅 gate fallback 发：清屏重绘 + fallback 文本替换最终回复）/`__SUMMARY__`/`__END__`；Rust 逐帧转发，`X-Accel-Buffering: no` |
| **生成有界性** | planner ⇄ execute 轮次上限 `MAX_PLAN_ROUNDS=4`（超限确定性强制收尾）+ `recursion_limit=30` + LLM 120s + 流式空闲 120s + 总时长 300s + 16 线程池；空回复后端补发恢复语 |
| **服务间身份断言（20260917）** | agent 的 `user_id` 直接进 config 并被 IoT 工具用来签用户 JWT ⇒ 身份边界不能只靠"只听回环"。Rust 用同一 `JWT_SECRET` 签 60s 短时效断言（`X-Agent-Assertion`，`aud=agent` 防被当登录 token 复用），agent 验签后**用断言里的 uid 覆盖请求体**。滚动上线：`AGENT_REQUIRE_ASSERTION=0`（默认，缺头只 WARNING）→ Rust 部署 → 打开严格模式（缺头/验签失败 → 401） |
| **工具返回三类（20260917）** | `ToolResult`（str 子类 + `kind`）：`ok` / `empty`（结果就是空，**是事实**，照常进执行回执）/ `unavailable`（服务不可用，**不是事实**，checker 判 BLOCK、不进跨轮执行记忆）。此前 `_get` 把上游故障吞成 `[]`，"服务挂了"伪装成"查到了、就是空的"。⚠️ 工具出口必须走 `_shape()`——`str(ToolResult)` 会退化成普通 str 丢掉 kind |
| **输入限额与并发闸（20260916）** | 字段级 Pydantic 限额（message 4000 / history 60 / 图片 6 张且单张 ≤1.6M 字符 …）+ Content-Length > 12MB → 413（starlette 默认**不限制** body 大小）+ 流式并发闸（每 worker 8 槽，排队 3s 拿不到 → 503，槽位在 `event_stream` 的 finally 归还） |
| **协作取消的颗粒度（20260917）** | 断连 → `stop_event` → 循环级 + 节点级 + **逐 spec** 三层检查（一份 `[导航, 屏显]` 清单在中途断连时，后面的写操作不执行）。**边界如实**：in-flight 的 HTTP（LLM/设备/回执轮询）拦不住，最坏等它自己超时——所以承诺是"**写操作绝不发生在用户离开之后**"，不是"立刻停止一切副作用" |

---

## 🛠️ 添加新工具

在 `tools/base.py` 中用 `@tool` 装饰器定义函数，加入 `_TOOL_REGISTRY`（execute 经 `_TOOL_MAP` 调用，
planner 注入时自动带描述）。**可规划性由白名单决定（20260903）**：无参只读数据工具 → 加
`agent/skills.py` 的 `_EXPLICIT_TOOLS`（PARAMS.tools 点名）；带参检索/读全文 → 加
`_CALLABLE_QUERY_TOOLS`（PARAMS.calls 调用）；**动作工具（有副作用）只能经技能模板展开**——在
`agent/skills.py` 注册对应技能（触发条件 + 工具序列模板 + 回复契约），planner 才选得到它。
不注册不进白名单 = planner 不可规划、execute 必拒（`__ERROR__` 帧）——这是本项目的核心约定。

## 🔧 配置速查（.env）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `qwen` | `qwen` / `deepseek` / `openai` 三选一 |
| `QWEN_MODEL` | `qwen3.6-flash` | 模型名（按 provider 前缀：`QWEN_`/`DEEPSEEK_`/`OPENAI_`） |
| `LLM_ENABLE_THINKING` | `true` | Qwen 思考模式总开关；图内 LLM 调用均显式关闭思考（20260903：planner 决策 / model 叙述——narrator，20260831 46~106s 慢调用实证 / execute 屏幕文案创作 / 摘要），关闭是 per-call 覆写、与总开关无关 |
| `AGENT_RECURSION_LIMIT` | `30` | 工具循环上限（幻觉重试兜底，server.py 读取） |
| `AGENT_REQUIRE_ASSERTION` | `0` | 是否**强制**要求服务间身份断言（见「关键机制」表）；生产已开 `1`，本机调试可关 |
| `AGENT_MAX_CONCURRENT` | `8` | 每 worker 并发流上限（超了排队 3s 后 503） |
| `AGENT_MAX_BODY_BYTES` | `12582912` | 请求体上限（12MB → 413） |
| `trace_dir` | `logs/agent/traces` | 对话 trace 落盘目录（20260830f 随日志分组迁移） |

## ✅ 测试与评测

```bash
.venv/bin/python tests/test_skills.py    # L0：秒级，无 LLM（映射表/计划实例化/解析容错/execute 确定性执行/gate 声称闸与 fallback）
.venv/bin/python tests/test_hardening.py # L0：秒级（TLS 校验/输入限额/体积闸/并发闸/工具返回三类/身份断言/幂等并发/RAG 两态）
.venv/bin/python tests/test_cancel.py    # L0：秒级（协作取消：五节点入口/写操作零调用/中途取消/LLM 阻塞期间的能力边界）
.venv/bin/python tests/test_entities.py  # L0：秒级（数据工具回执的实体摘要：序号/计数/候选照抄，解析失败给空摘要，planner 规则 6b 契约在位）
.venv/bin/python eval/run_golden.py           # L2：78 条真实 LLM 端到端（导航/特效/夜间/多轮/设备显示/注入攻击/摘要/闲聊/RAG 内容问答/执行记忆）；--limit N / --only <id1,id2> 单跑
.venv/bin/python eval/golden_full_run.py      # L2 进程隔离全量跑（逐条独立进程 + 180s 超时，防悬挂污染）
.venv/bin/python eval/recall_eval.py          # L1 检索：recall@k/MRR（21 条 queries = 12 正例 + 9 噪声）
.venv/bin/python tests/run_all.py             # 全部离线套件一把跑（glob 枚举，单套件超时；CI 的同一批）
```

- **CI（`.github/workflows/eval.yml`）**：push 跑上面五个秒级套件（L0）。
- **golden 全量（L2）不进 CI（20260920 撤下）**：CI 侧那条 LLM 腿每次调用先吊住（3 条本机
  39 秒的用例在 CI 跑 21 分钟未完）、一次 120 分钟的全量跑被杀且零产出、且诊断出 CI 凭据
  `401 invalid_api_key`。全量改为**本机按需手动跑**（本机即生产服务器，链路真实，78 条约 19 分钟）：
  `.venv/bin/python eval/run_golden.py`（`--limit N` / `--only a,b`）或
  `.venv/bin/python eval/golden_full_run.py`（进程隔离 + 单条 180s 超时）。
- **门禁分两层判（20260921）**：`tags` 含 `regression` 的用例（16 条：防幻觉/契约/撤回话术/
  执行记忆）**硬判 100%**，不受 `--min-pass-rate` 放宽（回归题不许波动）；其余是能力题，按通过率判。
  报告里 `regression` 块单列，复审单把回归组红写在最前（当天必修）。
- nightly cron 自动跑 L1/L2 两项 + `eval/golden_draft.py`（把 trace_alert 命中的真实现场整理成
  用例草稿到 `eval/report/golden_drafts_*.md`，**只产草稿、人审后手抄入库**，含真实用户文本故不进 git），
  失败标记 `~/agent_regression.failed`。
- **改技能注册表 / plan 契约 / 摘要逻辑 / prompt 后必跑**（golden 断言含"回复不得包含 SUMMARY:"）。

---

## 📄 许可
Apache-2.0

