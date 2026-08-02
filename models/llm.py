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
    params = {
        "model": kwargs.pop("model", settings.active_llm_model),
        "api_key": kwargs.pop("api_key", settings.active_llm_api_key),
        "base_url": kwargs.pop("base_url", settings.active_llm_base_url),
        "temperature": kwargs.pop("temperature", settings.llm_temperature),
        "max_tokens": kwargs.pop("max_tokens", settings.llm_max_tokens),
        "streaming": kwargs.pop("streaming", settings.llm_streaming),
        "verbose": kwargs.pop("verbose", settings.agent_verbose),
        **kwargs,
    }
    # Qwen3 系列默认开启思考模式（enable_thinking=true），思维链经 reasoning_content 返回；
    # 在工具调用轮次存在间歇性混入正文的风险（表现为回复开头出现英文规划文本）。
    # 对话型场景直接关闭思考模式，从根源消除思维链泄露。
    # 注意：enable_thinking 是 Qwen 自有参数，须走 extra_body（model_kwargs 只接受标准 OpenAI 参数）
    if settings.llm_provider.lower() == "qwen":
        params["extra_body"] = {"enable_thinking": False}
    return ChatOpenAI(**params)
