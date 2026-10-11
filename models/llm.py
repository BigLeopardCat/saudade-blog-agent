"""LLM initialization module.

Factory function to create a LangChain ChatOpenAI-compatible LLM.
Reads the active provider's config from ``settings.active_llm_*``.
"""

from langchain_openai import ChatOpenAI
from config import settings


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
    _prov = settings.llm_provider.lower()
    if _prov == "qwen":
        _extra["enable_thinking"] = bool(thinking)
    elif _prov == "deepseek":
        _extra["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if _extra:
        params["extra_body"] = _extra
    if seed:                       # 0/空 ⇒ 不设，见上面的注
        params["seed"] = int(seed)
    return ChatOpenAI(**params)
