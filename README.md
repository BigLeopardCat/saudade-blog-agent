# Saudade Blog AI Agent（泠月喵）🐱

博客看板娘"泠月喵"的对话 Agent 后端（FastAPI，:8010）。真实生产部署在
[Saudade-Blog](https://github.com/BigLeopardCat/Saudade-Blog)（Rust 后端 :3000 + React 前端）中，
负责：对话生成、博客内容查询、导航/特效/夜间模式命令、IoT 设备（ESP32 OLED）屏幕显示。

**核心定位：手写 LangGraph 图（planner → model ⇄ tools → reflector）+ 技能注册表受限规划，
对话记忆全部外置 MySQL（agent 无状态，每请求独立线程）。**

---

## 🏗️ 架构一句话

```
浏览器(autoload.js)
  → POST /api/chat/stream (SSE)          [nginx → Rust :3000]
  → Rust: 鉴权JWT → 消息入库 → 组装请求体（20 条历史 + 摘要 + 状态）
  → Python Agent :8010: 手写图（planner 选技能 → model 模板执行 → tools → reflector 质检）
  → Rust: 逐帧转发 + 流结束存回复 + __SUMMARY__ 帧摘要入库
  → 浏览器: 逐帧渲染 + 命令帧执行（导航/特效/夜间）
```

详细架构：[docs/agent-architecture.md](docs/agent-architecture.md)（全链路：时序、记忆、工具、防幻觉、超时、部署）。
评测与可观测设计：[docs/eval-observability.md](docs/eval-observability.md)。

---

## 📁 目录结构

```
saudade-blog-agent/
├── server.py               # FastAPI 入口：/chat、/chat/stream、/health；强制显示路由；流式编排
├── main.py                 # CLI 调试入口（交互式 / --ask 单问）
├── agent/
│   ├── graph.py            # ★ 手写 LangGraph 图：planner(选技能) → model(模板执行) → tools → reflector(模板质检)
│   ├── skills.py           # ★ 技能注册表：7 技能静态定义 + NAV_MAP 导航映射（业务唯一数据源）
│   ├── agent.py            # create_agent：手写图入口（build_graph）
│   ├── memory.py           # MemorySaver 兼容存根（实际不承担记忆，见文档 §4.6）
│   └── prompts.py          # BLOG_ASSISTANT_PROMPT：猫猫女仆人设 + 工具约束
├── chains/                 # [预留] LCEL 链组合（仅占位）
├── config/settings.py      # pydantic-settings 配置
├── models/llm.py           # LLM 工厂：provider 三选一（qwen/deepseek/openai）
├── tools/base.py           # 21 个 @tool 工具 + _TOOL_REGISTRY + IoT JWT 代签 + 显示幂等去重
├── eval/                   # 评测：golden set + run_golden.py（L2 任务级，真实 LLM）
├── test_skills.py          # L0 单元级（技能注册表 + plan 契约，秒级，无 LLM）
└── docs/                   # 架构文档 + 评测可观测设计
```

---

## 🚦 快速开始

```bash
cd saudade-blog-agent
uv sync                       # 创建 .venv + 安装依赖
cp .env.example .env         # 填入 LLM API Key（生产：qwen → qwen3.6-flash）
```

**本地调试（CLI）**：
```bash
uv run python main.py                    # 交互式聊天
uv run python main.py --ask "打开物联网平台"
```

**以服务方式运行（生产形态）**：
```bash
.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8010 --workers 2
curl http://127.0.0.1:8010/health        # agent_ready
```

---

## 🧠 关键机制

| 机制 | 说明 |
|---|---|
| **技能注册表 + 受限规划** | 固定流程任务（导航/特效/夜间/设备显示/设备查询）落地为 `skills.py` 静态技能定义；planner 只**选技能 + 填参数**（`SKILL:/PARAMS:` 结构化输出），executor 按模板执行（`TOOLS:/NOTE:/REPLY:` 五行契约），不再自由写执行步骤 |
| **模板质检（reflector）** | 对照技能模板 + 工具轨迹出 VERDICT：TOOLS 行要求的工具缺失即 REVISE（覆盖"假装执行"）；chat 技能非空快道；REVISE 预算 2 次，预算耗尽收尾；轨迹按轮次裁剪（被否定轮不入判罚） |
| **记忆外置 MySQL** | 每请求独立线程（无 checkpointer），连续性靠 Rust 注入 20 条历史 + 滚动摘要；摘要由后端**独立任务调用**生成（`_summarize_dialogue`，与回复解耦，模型对记忆无写权限，防摘要幻觉污染）；流式经 `__SUMMARY__` 帧、非流式经 `new_summary` 字段入库 |
| **强制显示路由（_force_display）** | "显示到 OLED"类请求后端**先执行**（小 LLM 提取内容 → `device_oled_display` → 注记注入），模型只能基于事实回复——无论模型说什么，动作已完成 |
| **SSE 帧协议** | JSON 编码 + `\n\n` 分隔；文本帧/命令帧/`__PROCESS__`（过程轨迹）/`__RESET__`（否定轮清屏）/`__SUMMARY__`/`__END__`；Rust 逐帧转发，`X-Accel-Buffering: no` |
| **生成有界性** | `recursion_limit=30` + LLM 120s + 流式空闲 120s + 总时长 300s + 16 线程池；空回复后端补发恢复语 |

---

## 🛠️ 添加新工具

在 `tools/base.py` 中用 `@tool` 装饰器定义函数，加入 `_TOOL_REGISTRY` 即可（planner 注入时自动带完整
schema）。**若它服务于固定流程任务**（如新技能），应在 `agent/skills.py` 注册对应技能（触发条件 +
工具序列模板 + 回复契约），否则 planner 无法可靠选择它——这是本项目的核心约定。

## 🔧 配置速查（.env）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `qwen` | `qwen` / `deepseek` / `openai` 三选一 |
| `QWEN_MODEL` | `qwen3.6-flash` | 模型名（按 provider 前缀：`QWEN_`/`DEEPSEEK_`/`OPENAI_`） |
| `LLM_ENABLE_THINKING` | `true` | Qwen 思考模式总开关；reflector/显示提取/摘要三个低 token 调用强制关闭 |
| `AGENT_RECURSION_LIMIT` | `30` | 工具循环上限（幻觉重试兜底，server.py 读取） |

> `MAX_REFLECTIONS=2`（reflector REVISE 预算）是 graph.py 代码常量，非 env 配置。

## ✅ 测试与评测

```bash
.venv/bin/python test_skills.py        # L0：秒级，无 LLM（映射表/计划实例化/解析容错/确定性闸）
.venv/bin/python eval/run_golden.py    # L2：13 条真实 LLM 端到端（导航/特效/夜间/注入攻击/摘要/闲聊），约 6 分钟
```

- nightly cron 自动跑上述两项，失败标记 `~/agent_regression.failed`。
- **改技能注册表 / plan 契约 / 摘要逻辑 / prompt 后必跑**（golden 断言含"回复不得包含 SUMMARY:"）。

---

## 📄 许可

MIT
