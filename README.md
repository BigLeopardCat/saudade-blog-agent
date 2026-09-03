# Saudade Blog AI Agent（泠月喵）🐱

博客看板娘"泠月喵"的对话 Agent 后端（FastAPI，:8010）。真实生产部署在
[Saudade-Blog](https://github.com/BigLeopardCat/Saudade-Blog)（Rust 后端 :3000 + React 前端）中，
负责：对话生成、博客内容查询、导航/特效/夜间模式命令、IoT 设备（ESP32 OLED）屏幕显示。

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
RAG 设计总结（面试材料）：[docs/rag-design.md](docs/rag-design.md)。

---

## 📁 目录结构

```
saudade-blog-agent/
├── server.py               # FastAPI 入口：/chat、/chat/stream、/health；流式编排（生产唯一入口）
├── agent/
│   ├── graph.py            # ★ 手写 LangGraph 图：planner(唯一决策) ⇄ execute(确定性执行) → model(零工具叙述) → gate(确定性检查)
│   ├── skills.py           # ★ 技能注册表：8 技能静态定义 + NAV_MAP 导航映射（业务唯一数据源）
│   ├── agent.py            # create_agent：手写图入口（build_graph，planner ⇄ execute → model → gate）
│   ├── memory.py           # MemorySaver 兼容存根（实际不承担记忆，见文档 §4.6）
│   └── prompts.py          # BLOG_ASSISTANT_PROMPT：猫猫女仆人设 + 叙述规则（model 零工具 narrator 用）
├── rag/                    # ★ RAG 检索管线（20260830）：词法 2/3-gram BM25 内存倒排索引，
│   │                       #   语料=线上可见文章（20260901 净化：说说/留言/公告移出检索池），
│   │                       #   10 分钟懒刷新；检索只定位（候选 ID+标题+分），解读走 get_article_detail 全文
│   └── search.py           # RagIndex + search()：检索 eval 直接测本实现（评测即线上行为）
├── config/settings.py      # pydantic-settings 配置
├── models/llm.py           # LLM 工厂：provider 三选一（qwen/deepseek/openai）
├── tools/base.py           # 22 个 @tool 工具 + _TOOL_REGISTRY + IoT JWT 代签 + 显示幂等去重
├── utils/                  # logging（trace_id/日志）+ trace（对话 trace 落盘）+ tts（未启用）+ helpers
├── eval/                   # 评测：golden set（55 条）+ run_golden.py（L2 任务级，真实 LLM）
│   │                       #       + golden_case_runner.py/golden_full_run.py（20260902 进程隔离跑法）
│   │                       #       + recall_eval.py（L1 检索：recall@k/MRR，直接测 rag/search.py）
├── scripts/                # agent_metrics（质量指标）+ nightly_regression（cron 每 4:00）
├── test_skills.py          # L0 单元级（技能注册表 + plan 契约，秒级，无 LLM）
└── docs/                   # 架构文档 + 评测可观测设计
```

---

## 🚦 快速开始

```bash
cd saudade-blog-agent
uv sync                       # 创建 .venv + 安装依赖
cp .env.example .env         # 填入 LLM API Key（生产：qwen → qwen3.8-flash；代码默认值见下方配置表）
```

**以服务方式运行（生产形态）**：systemd 服务 `saudade-agent`（2 workers，`Restart=always` 崩溃自愈，
`TimeoutStopSec=120` 优雅停等在途对话），改动后 `sudo systemctl restart saudade-agent` 生效。
健康检查：`curl http://127.0.0.1:8010/health`（agent_ready）。

**日志**（20260830f 日志分组）：`logs/agent/agent.log`（systemd StandardOutput/Error append）
+ `logs/agent/traces/`（对话 trace JSON，路径由 `trace_dir` 配置）——排障直接读 trace 的分段
耗时（planner/execute/model/gate，20260903 起四段），不必翻日志。

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
| `trace_dir` | `logs/agent/traces` | 对话 trace 落盘目录（20260830f 随日志分组迁移） |

## ✅ 测试与评测

```bash
.venv/bin/python test_skills.py               # L0：秒级，无 LLM（映射表/计划实例化/解析容错/execute 确定性执行/gate 声称闸与 fallback）
.venv/bin/python eval/run_golden.py           # L2：55 条真实 LLM 端到端（导航/特效/夜间/多轮/设备显示/注入攻击/摘要/闲聊/RAG 内容问答）；--limit N / --only <id> 单跑
.venv/bin/python eval/golden_full_run.py      # L2 进程隔离全量跑（逐条独立进程 + 180s 超时，防悬挂污染）
.venv/bin/python eval/recall_eval.py          # L1 检索：recall@k/MRR（21 条 queries = 12 正例 + 9 噪声）
```

- nightly cron 自动跑上述两项，失败标记 `~/agent_regression.failed`。
- **改技能注册表 / plan 契约 / 摘要逻辑 / prompt 后必跑**（golden 断言含"回复不得包含 SUMMARY:"）。

---

## 📄 许可

MIT
