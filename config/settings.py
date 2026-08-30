"""Application configuration using pydantic-settings.

Each LLM provider keeps its own environment variables.
Set ``LLM_PROVIDER=deepseek|qwen|openai`` to choose the active one.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

# ── Provider registry ──────────────────────────────────────────────
# Maps provider name → (env_prefix, default_model, default_base_url)
PROVIDER_DEFAULTS = {
    "deepseek": {
        "model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com",
    },
    "qwen": {
        "model": "qwen3.6-flash",
        "base_url": "https://ws-98l2m94bvvnta30m.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    },
    "openai": {
        "model": "gpt-4o",
        "base_url": "https://api.openai.com/v1",
    },
}


class Settings(BaseSettings):
    """Global application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Provider selection ─────────────────────────────────────────
    llm_provider: str = "qwen"

    # ── DeepSeek ───────────────────────────────────────────────────
    deepseek_api_key: str = ""
    deepseek_base_url: str = PROVIDER_DEFAULTS["deepseek"]["base_url"]
    deepseek_model: str = PROVIDER_DEFAULTS["deepseek"]["model"]

    # ── Qwen (通义千问) ────────────────────────────────────────────
    qwen_api_key: str = ""
    qwen_base_url: str = PROVIDER_DEFAULTS["qwen"]["base_url"]
    qwen_model: str = PROVIDER_DEFAULTS["qwen"]["model"]

    # ── OpenAI ─────────────────────────────────────────────────────
    openai_api_key: str = ""
    openai_base_url: str = PROVIDER_DEFAULTS["openai"]["base_url"]
    openai_model: str = PROVIDER_DEFAULTS["openai"]["model"]

    # ── Shared LLM params ─────────────────────────────────────────
    llm_temperature: float = 0.7
    llm_max_tokens: int = 8192
    llm_streaming: bool = True
    # LLM 无数据超时（秒）：API 偶发无响应时结束生成，避免调用无限挂起占满线程池
    llm_timeout: float = 120.0
    # Qwen 思考模式开关（默认开：A/B 全量 golden 13/13 + live 冒烟均无推理泄漏——
    # Qwen 的 thinking 走独立 reasoning_content 字段，不进回复正文；如遇泄漏可用
    # LLM_ENABLE_THINKING=0 关闭）
    llm_enable_thinking: bool = True

    # ── Agent ───────────────────────────────────────────────────────
    agent_verbose: bool = True
    agent_max_iterations: int = 10
    agent_early_stopping_method: str = "generate"

    # ── TTS (Text-to-Speech) ───────────────────────────────────────
    tts_enabled: bool = False
    tts_voice: str = "zh-CN-XiaoyiNeural"

    # ── Memory ──────────────────────────────────────────────────────
    memory_session_key: str = "default"

    # ── IoT 设备服务（ESP32 OLED 显示等）─────────────────────────────
    # 与博客共用 JWT_SECRET：agent 以对话用户身份签发 JWT 调用 device-service
    jwt_secret: str = ""
    device_service_url: str = "http://127.0.0.1:3100"

    # ── Logging ─────────────────────────────────────────────────────
    log_level: str = "INFO"

    # ── Tracing（对话执行 trace 落盘，见 utils/trace.py）──────────────
    # 每请求一份 JSON（输入/节点事件序列/分段耗时/回复/退出原因）；
    # 与项目 logs/ 目录对齐（日志体系规范见 CLAUDE.md §2），logrotate 轮转
    trace_dir: str = "/home/ubuntu/memory_blog_rust/logs/agent/traces"

    # ── Active provider helpers ─────────────────────────────────────

    @property
    def _provider_prefix(self) -> str:
        """Return the env-var prefix for the active provider."""
        return self.llm_provider.lower()

    @property
    def active_llm_api_key(self) -> str:
        return getattr(self, f"{self._provider_prefix}_api_key")

    @property
    def active_llm_base_url(self) -> str:
        return getattr(self, f"{self._provider_prefix}_base_url")

    @property
    def active_llm_model(self) -> str:
        return getattr(self, f"{self._provider_prefix}_model")

    @property
    def is_api_key_configured(self) -> bool:
        key = self.active_llm_api_key
        return bool(key) and key != "your-api-key-here"


settings = Settings()

