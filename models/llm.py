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

# Startup rejects unknown providers; direct script callers still get one warning.
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
            "未发送到上游（本进程只报一次）；接入新服务商时需核对其参数方言和消息回放契约。",
            provider, thinking)
    return {}


def _provider_headers(provider: str) -> dict:
    """OpenCode requires conversation affinity; unbound calls get an isolated identifier."""
    if (provider or "").strip().lower() == "opencode":
        return {"x-opencode-session": current_session() or uuid.uuid4().hex,
                "User-Agent": "saudade-blog-agent/1.0"}
    return {}


# DeepSeek uses JSON mode; callers must include the output contract in its prompt.
_JSON_OBJECT_PROVIDERS = frozenset({"deepseek"})
_JSON_SCHEMA_PROVIDERS = frozenset({"qwen", "openai", "opencode"})
DIALECT_PROVIDERS = _JSON_OBJECT_PROVIDERS | _JSON_SCHEMA_PROVIDERS
_WARNED_FORMAT_PROVIDERS: set[str] = set()


def response_format(provider: str, schema: dict, *, name: str = "result") -> dict:
    """Use the configured provider's supported structured-output dialect."""
    key = (provider or "").strip().lower()
    if key in _JSON_OBJECT_PROVIDERS:
        return {"type": "json_object"}
    if key not in DIALECT_PROVIDERS and key not in _WARNED_FORMAT_PROVIDERS:
        _WARNED_FORMAT_PROVIDERS.add(key)
        logger.warning(
            "[llm] provider=%r 的结构化输出方言没登记 ⇒ 按默认 json_schema 走"
            "（本进程只报一次）；接入新服务商前需验证并登记结构化输出契约。", provider)
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
    # Control thinking and seeds per call; zero means no seed parameter.
    thinking = kwargs.pop("enable_thinking", settings.llm_enable_thinking)
    seed = kwargs.pop("seed", settings.llm_seed)
    params = {
        "model": kwargs.pop("model", settings.active_llm_model),
        "api_key": kwargs.pop("api_key", settings.active_llm_api_key),
        "base_url": kwargs.pop("base_url", settings.active_llm_base_url),
        "temperature": kwargs.pop("temperature", settings.llm_temperature),
        "max_tokens": kwargs.pop("max_tokens", settings.llm_max_tokens),
        "streaming": kwargs.pop("streaming", settings.llm_streaming),
        "verbose": kwargs.pop("verbose", settings.agent_verbose),
        # Bound stalled upstream requests even outside the application deadline.
        "timeout": kwargs.pop("timeout", settings.llm_timeout),
        **kwargs,
    }
    from agent.runtime.control import model_options

    deadline_options = model_options(params["timeout"])
    if deadline_options:
        params.update(deadline_options)
        params["max_retries"] = 0
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.callbacks.manager import CallbackManager

    from agent.runtime.budget import current_budget
    from agent.runtime.model_budget import ModelBudgetHandler
    from models.media import ModelMediaHandler

    budget = current_budget()
    handlers: list[BaseCallbackHandler] = [ModelMediaHandler()]
    if budget is not None:
        handlers.append(ModelBudgetHandler(budget))
        params["max_retries"] = 0
    params["callbacks"] = CallbackManager.configure(params.get("callbacks"), handlers)
    # Provider-specific thinking fields belong in extra_body. Preserve caller fields:
    # strict providers require consistent reasoning metadata when replaying tool calls.
    _extra = dict(params.get("extra_body") or {})
    _extra.update(_thinking_extra(settings._provider_prefix, thinking, params["model"]))
    if _extra:
        params["extra_body"] = _extra
    # Session affinity and caller headers share the same SDK client configuration.
    _hdrs = dict(params.get("default_headers") or {})
    _hdrs.update(_provider_headers(settings._provider_prefix))
    if _hdrs:
        params["default_headers"] = _hdrs
    if seed:
        params["seed"] = int(seed)
    return ChatOpenAI(**params)
