# LangChain Agent 🚀

生产级 LangChain Agent（LangGraph 架构），基于 **DeepSeek API** 构建，
支持自定义工具、对话记忆（checkpointer）和交互式 / 单次查询两种运行模式。

---

## 📁 项目结构

```
langchain/
├── agent/                  # Agent 核心逻辑
│   ├── __init__.py
│   ├── agent.py            # create_agent() — LangGraph 图构建
│   ├── memory.py           # MemorySaver checkpointer (对话持久化)
│   └── prompts.py          # 系统提示词模板
│
├── chains/                 # [预留] LCEL 链组合
│   └── __init__.py
│
├── config/                 # 集中配置管理
│   ├── __init__.py
│   └── settings.py         # pydantic-settings，从 .env 加载
│
├── models/                 # LLM 模型层
│   ├── __init__.py
│   └── llm.py              # ChatOpenAI → DeepSeek 封装
│
├── tools/                  # 自定义工具
│   ├── __init__.py
│   └── base.py             # @tool 装饰器 + _TOOL_REGISTRY
│
├── utils/                  # 工具函数
│   ├── __init__.py
│   ├── helpers.py          # 格式化等通用函数
│   └── logging.py          # 统一日志配置
│
├── .env                    # ⚠️ 环境变量 (已 gitignore，不要提交!)
├── .env.example            # 环境变量模板
├── .gitignore
├── main.py                 # 入口点 (CLI + 交互/单次模式)
├── pyproject.toml           # 项目元数据 + 依赖 (uv)
├── uv.lock                  # uv 锁定文件
└── README.md
```

---

## 🚦 快速开始

### 1. 环境准备

```bash
cd langchain

# uv 已安装 → 自动创建 .venv + 安装依赖
uv sync
```

> 若未安装 uv，参见 [docs.astral.sh/uv](https://docs.astral.sh/uv/#installation)

### 2. 配置 API Key

```bash
cp .env.example .env
```

编辑 `.env`，填入你的 DeepSeek API Key：

```ini
DEEPSEEK_API_KEY=sk-your-real-key-here
```

> ⚠️ `.env` 已在 `.gitignore` 中，不会误提交。

### 3. 运行

**交互式聊天：**
```bash
uv run python main.py
```

**单次问答：**
```bash
uv run python main.py --ask "计算 2 + 3 * 4 等于多少？"
```

---

## 🧩 各模块说明

| 模块 | 路径 | 职责 |
|------|------|------|
| **config** | `config/settings.py` | 基于 `pydantic-settings` 管理所有配置，支持类型校验 |
| **models** | `models/llm.py` | 封装 `ChatOpenAI` 连接 DeepSeek API |
| **agent** | `agent/agent.py` | `create_agent()` — LangGraph 图构建，组装 LLM + Tools + Checkpointer |
| **memory** | `agent/memory.py` | `MemorySaver` checkpointer，实现跨轮次对话记忆 |
| **prompts** | `agent/prompts.py` | 系统提示词定义 |
| **tools** | `tools/base.py` | `@tool` 装饰器定义的自定义工具集，通过 `_TOOL_REGISTRY` 注册 |
| **utils** | `utils/logging.py` | 统一日志格式 |
| **main.py** | 入口 | CLI 参数解析 + 交互/单次两种运行模式 |

### 技术栈

| 组件 | 说明 |
|------|------|
| **langchain 1.3+** | Agent 框架，采用 **LangGraph** 图架构 |
| **create_agent()** | 新版 graph-based agent，替代旧的 AgentExecutor |
| **MemorySaver** | langgraph 内置 checkpointer，自动保存对话状态 |
| **DeepSeek API** | 通过 `langchain-openai` 的 `ChatOpenAI` 兼容接口调用 |

---

## 🛠️ 添加新工具

在 `tools/base.py` 中用 `@tool` 装饰器定义函数，然后加入 `_TOOL_REGISTRY`：

```python
@tool
def my_custom_tool(param: Annotated[str, "参数说明"]) -> str:
    """工具描述 — LLM 看到的说明"""
    # 你的业务逻辑
    return result

_TOOL_REGISTRY = [
    calculator,
    echo,
    my_custom_tool,  # ← 注册即可自动生效
]
```

---

## 🔧 配置项速查

所有配置项见 `.env.example`，关键项：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | — | DeepSeek API Key |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 模型名 |
| `DEEPSEEK_TEMPERATURE` | `0.7` | 生成温度 |
| `AGENT_VERBOSE` | `true` | 是否输出思考过程 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

---

## 📄 许可

MIT
