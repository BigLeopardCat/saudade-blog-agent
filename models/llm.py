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
    # enable_thinking=False 强制关闭（如 _extract_display_intent 的内容提取）
    thinking = kwargs.pop("enable_thinking", settings.llm_enable_thinking)
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
    # Qwen3 系列默认开启思考模式（enable_thinking=true），思维链经 reasoning_content 返回；
    # 历史上有间歇性混入正文的风险（回复开头出现英文规划文本），默认关，可用
    # LLM_ENABLE_THINKING=1 打开（A/B 验证后决定默认值）。
    # 注意：enable_thinking 是 Qwen 自有参数，须走 extra_body（model_kwargs 只接受标准 OpenAI 参数）
    if settings.llm_provider.lower() == "qwen":
        params["extra_body"] = {"enable_thinking": bool(thinking)}
    return ChatOpenAI(**params)
