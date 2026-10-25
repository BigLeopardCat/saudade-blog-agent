"""LLM initialization module.

Factory function to create a LangChain ChatOpenAI-compatible LLM.
Reads the active provider's config from ``settings.active_llm_*``.
"""

import logging
import uuid

from langchain_openai import ChatOpenAI

from config import settings
from models.session import current_session

logger = logging.getLogger(__name__)

# 认不出的 provider **只报一次**：这里是每个请求都会走的路径（图内四个调用点），
# 一次一条会把 agent.log 淹掉、把真信号埋了。常态下走不到这里——`LLM_PROVIDER` 的
# 启动校验在 `settings.check_provider()`（认不出就拒绝启动）；这一条兜的是**不经
# server 启动**的入口（`scripts/`、`eval/` 那批直接把设置传进来的脚本）。
_WARNED_PROVIDERS: set[str] = set()


def _thinking_extra(provider: str, thinking: bool, model: str = "") -> dict:
    """Translate per-call control using the provider and model's accepted dialect.

    The opencode DeepSeek endpoint accepts thinking.type. Its MiniMax endpoint
    rejects that field, so other gateway model families keep provider defaults.
    """
    if provider == "qwen":
        return {"enable_thinking": bool(thinking)}
    if provider == "deepseek" or (provider == "opencode" and model.startswith("deepseek-")):
        return {"thinking": {"type": "enabled" if thinking else "disabled"}}
    if provider == "opencode":
        return {}
    if provider not in _WARNED_PROVIDERS:
        _WARNED_PROVIDERS.add(provider)
        logger.warning(
            "[llm] provider=%r 没有思考开关的译法 ⇒ 这一次的 `enable_thinking=%s` "
            "**被丢弃**（本进程只报一次）。图内四个调用点都靠它关思考，而开着思考的 "
            "assistant 消息回放会让严格服务商整轮 400 ⇒ 要么在这里登记它的字段名，"
            "要么确认这家根本没有思考开关。", provider, thinking)
    return {}


def _provider_headers(provider: str) -> dict:
    """OpenCode requires conversation affinity; unbound calls get an isolated identifier."""
    if (provider or "").strip().lower() == "opencode":
        return {"x-opencode-session": current_session() or uuid.uuid4().hex,
                "User-Agent": "saudade-blog-agent/1.0"}
    return {}


# ── 结构化输出的方言（20261019 批 G7：从 `eval/llm_judge.py` 上移）────────────
# **方言不是通用的**（20261012 实测）：DeepSeek 对 `json_schema` 直接 400
# `This response_format type is unavailable now`，它只认 `{"type": "json_object"}`
# （官方 JSON Output 文档：设 response_format 为它、提示词里必须出现 json 字样**并给出
# 目标格式的样例**、max_tokens 要给够，且它自己承认有概率返回空 content）。
#
# **为什么上移**：这一族此前住在 `eval/llm_judge.py`，而 `eval.*` 不能被生产引
# （wheel 边界，`tests/test_packaging.py`）⇒ 复盘器（`agent/graph.py::reflector_node`）
# 要用同一套方言就只能抄第二份。它与本模块的 `_thinking_extra` 是**同一个形状**：
# 按服务商取译法 + 认不出的**出声**再按默认走。
#
# ⚠️ **`json_object` 那一档的字段契约必须同时写进提示词**（两边的调用方都照办了）：
# 那一档下 schema 只是装饰（服务商不校验），"契约只长在 schema 里"的后果不是"判得松
# 一点"，是**整份输出作废**——模型回一个字段齐缺的对象，下游按"没解析出来"静默走兜底，
# 而夜里汇总照旧 ALL PASS（20261012 那 181 条的形状）。
_JSON_OBJECT_PROVIDERS = frozenset({"deepseek"})   # 只认它，见上一段 400 原文
_JSON_SCHEMA_PROVIDERS = frozenset({"qwen", "openai", "opencode"})  # json_schema + strict 的原生地
# 上面两个集合**就是**分派表（不是给判据看的第二份拷贝）：下面按它们分派，认不出的才
# 落进缺省支并出声。登记与分派同源 ⇒ 加服务商只改一处。
DIALECT_PROVIDERS = _JSON_OBJECT_PROVIDERS | _JSON_SCHEMA_PROVIDERS
_WARNED_FORMAT_PROVIDERS: set[str] = set()


def response_format(provider: str, schema: dict, *, name: str = "result") -> dict:
    """`(服务商, JSON schema)` → 该服务商的 `response_format` 取值。

    **每一家都在 `DIALECT_PROVIDERS` 里登记过**，不写成 `if provider == "deepseek" …
    else 默认`——那种写法的缺省支是**隐式**的，加一个服务商时"它落进 else 了"这件事没有
    任何地方说过（同 `_thinking_extra` 少 else 的那个 bug）。认不出的服务商**出声**再按
    默认走：静默降级的后果是"整份输出作废而没人知道"。

    取值域与 `config.settings.KNOWN_LLM_PROVIDERS` 由 `tests/test_llm_provider_registry.py`
    对锁：**加服务商却忘了登记这一族 ⇒ 离线套件红**（机械闸门，不靠人记得）。
    """
    key = (provider or "").strip().lower()
    if key in _JSON_OBJECT_PROVIDERS:
        return {"type": "json_object"}
    if key not in DIALECT_PROVIDERS and key not in _WARNED_FORMAT_PROVIDERS:
        _WARNED_FORMAT_PROVIDERS.add(key)
        logger.warning(
            "[llm] provider=%r 的结构化输出方言没登记 ⇒ 按默认 json_schema 走"
            "（本进程只报一次）。换服务商请先探针实测它认哪种方言，再登记进 "
            "`models/llm.py::DIALECT_PROVIDERS`——未登记就默认，正是 20261012 那 181 条"
            "判据全废的形状。", provider)
    # json_schema 的原生地；qwen 已在产线端点上实测**被强制**（enum/required 拦得住）
    return {"type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema}}


def get_llm(**kwargs) -> ChatOpenAI:
    """Create and return a configured LLM instance.

    The active provider is selected via the ``LLM_PROVIDER`` env var.
    Each provider (deepseek / qwen / openai) keeps its own
    ``*_API_KEY``, ``*_BASE_URL`` and ``*_MODEL`` environment variables.

    Args:
        **kwargs: Override any default setting (model, base_url, etc.).

    Returns:
        ChatOpenAI: A LangChain LLM connected to the configured API.
    """
    # Qwen 思考模式开关：默认取 settings.llm_enable_thinking，调用方可传
    # enable_thinking=False 强制关闭（图内四个调用均显式关闭：20260830 planner/
    # reflector、20260831 executor——慢调用实证，见 agent-architecture §7）
    thinking = kwargs.pop("enable_thinking", settings.llm_enable_thinking)
    # 种子同 enable_thinking 一档：**0/缺席都表示"不设"**（settings.llm_seed 默认 0），
    # 只有非零才落到 params。绝不能写 `"seed": None`——那会把一个显式 null 发到
    # OpenAI 兼容端点上（不是所有服务商都容忍），而"不设"本来就是这里的默认语义。
    seed = kwargs.pop("seed", settings.llm_seed)
    params = {
        "model": kwargs.pop("model", settings.active_llm_model),
        "api_key": kwargs.pop("api_key", settings.active_llm_api_key),
        "base_url": kwargs.pop("base_url", settings.active_llm_base_url),
        "temperature": kwargs.pop("temperature", settings.llm_temperature),
        "max_tokens": kwargs.pop("max_tokens", settings.llm_max_tokens),
        "streaming": kwargs.pop("streaming", settings.llm_streaming),
        "verbose": kwargs.pop("verbose", settings.agent_verbose),
        # 无数据超时：OpenAI 兼容客户端默认 600s，API 偶发无响应会无限挂起
        # （曾导致 8 个线程池全部占满、后续所有对话排队卡死）
        "timeout": kwargs.pop("timeout", settings.llm_timeout),
        **kwargs,
    }
    from agent.runtime.control import model_options

    deadline_options = model_options(params["timeout"])
    if deadline_options:
        params.update(deadline_options)
        params["max_retries"] = 0
    from agent.runtime.budget import current_budget
    from agent.runtime.model_budget import ModelBudgetHandler

    budget = current_budget()
    if budget is not None:
        from langchain_core.callbacks.manager import CallbackManager

        params["callbacks"] = CallbackManager.configure(
            params.get("callbacks"), [ModelBudgetHandler(budget)])
        params["max_retries"] = 0
    # enable_thinking 是各家自有参数，须走 extra_body（model_kwargs 只接受标准
    # OpenAI 参数）。总开关默认开（settings.llm_enable_thinking，可 LLM_ENABLE_THINKING
    # 覆盖），但图内四个调用均 per-call 显式关闭（见上方注）——关闭是 per-call
    # 覆写、与总开关无关（思维链经 reasoning_content 返回，历史有混入正文风险）
    #
    # **开关的字段名每家不一样，必须各译各的**（20261012 实测补的 deepseek 那一支）：
    # qwen 用 `enable_thinking`，DeepSeek 用 `thinking.type`。此前只译 qwen ⇒ 切到
    # DeepSeek 后四个调用点写的 `enable_thinking=False` **被静默丢掉**（本仓最忌的那种
    # 失效），而 `deepseek-flash` 默认**思考是开的**（同题 completion_tokens：开 125 / 关 1）。
    # 后果不是"想多了"，是**整轮空回复**：多轮累积里 assistant 消息带着思考输出，回放时
    # 不带 `reasoning_content` ⇒ `400 The reasoning_content in the thinking mode must be
    # passed back to the API`，炸在 **narrator 那一次调用**（golden 三条冒烟 2 条空回复，
    # 而 planner/execute 那几轮全绿——因为它们的消息里还没有"前一条 assistant"要回放）。
    #
    # 合并而不是覆盖：调用方自己传了 extra_body 时**不许**把它悄悄抹掉——那正是上面
    # 这个 bug 的同一个形状（"写了没人读"与"写了被丢掉"是一回事）。
    _extra = dict(params.get("extra_body") or {})
    # 译法表在 `_thinking_extra`（缺省支会出声，见该函数头注）；provider 的归一化
    # 只认 `settings._provider_prefix` 那一处，别在这里再 strip/lower 一遍。
    _extra.update(_thinking_extra(settings._provider_prefix, thinking, params["model"]))
    if _extra:
        params["extra_body"] = _extra
    # 自定义请求头同理：合并而不是覆盖（调用方自己传了 default_headers 时不许抹掉）。
    _hdrs = dict(params.get("default_headers") or {})
    _hdrs.update(_provider_headers(settings._provider_prefix))
    if _hdrs:
        params["default_headers"] = _hdrs
    if seed:                       # 0/空 ⇒ 不设，见上面的注
        params["seed"] = int(seed)
    return ChatOpenAI(**params)
