"""Application configuration using pydantic-settings.

Each LLM provider keeps its own environment variables.
Set ``LLM_PROVIDER=deepseek|qwen|openai|opencode`` to choose the active one.
"""

import os
from typing import Literal

from pydantic import Field, model_validator
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
    # OpenCode requires a session header; models.llm adds it to compatible requests.
    "opencode": {
        "model": "deepseek-v4.1-flash",
        "base_url": "https://opencode.ai/zen/go/v1",
    },
}

KNOWN_LLM_PROVIDERS = frozenset(PROVIDER_DEFAULTS)


SUMMARY_MAX_CHARS = 12000


def check_retired_runtime(value, key):
    """Accept old native deployment labels without restoring runtime selection."""
    if value and str(value).strip().lower() != "native":
        raise ValueError(f"{key} is retired; remove it. Only the native runtime is supported.")


class Settings(BaseSettings):
    """Global application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        # Offline tests ignore local deployment configuration.
        env_file=None if os.environ.get("SAUDADE_IGNORE_ENV_FILE") else ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
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

    # ── opencode zen ───────────────────────────────────────────────
    opencode_api_key: str = ""
    opencode_base_url: str = PROVIDER_DEFAULTS["opencode"]["base_url"]
    opencode_model: str = PROVIDER_DEFAULTS["opencode"]["model"]

    # ── Shared LLM params ─────────────────────────────────────────
    llm_temperature: float = 0.7
    llm_max_tokens: int = 8192
    llm_streaming: bool = True
    # LLM 无数据超时（秒）：API 偶发无响应时结束生成，避免调用无限挂起占满线程池
    llm_timeout: float = 120.0
    # Defaults for direct SDK clients. Runtime and side tasks set their own modes.
    llm_enable_thinking: bool = True
    # Zero leaves sampling to the provider.
    llm_seed: int = 0

    # ── Native request policy (tokens include cached input) ──────────
    # Deployment sets the verified endpoint capacity; unknown models stay conservative.
    agent_context_tokens: int = Field(default=128000, gt=0)
    agent_request_tokens: int = Field(default=8000000, gt=0)
    agent_model_calls: int = Field(default=12, ge=2)
    agent_tool_calls: int = Field(default=16, gt=0)
    agent_reasoning_effort: Literal["none", "low", "high", "max"] = "none"
    agent_output_tokens: int = Field(default=1536, ge=128)
    agent_summary_output_tokens: int = Field(default=4096, ge=128)
    agent_compaction_trigger: float = Field(default=0.8, gt=0, lt=1)
    agent_compaction_target: float = Field(default=0.4, gt=0, lt=1)
    agent_verbose: bool = True

    @model_validator(mode="after")
    def validate_output_budgets(self):
        ceiling = min(self.agent_context_tokens, self.agent_request_tokens)
        if max(self.agent_output_tokens, self.agent_summary_output_tokens) >= ceiling:
            raise ValueError("Agent output limits must leave room for input tokens")
        if self.agent_compaction_target >= self.agent_compaction_trigger:
            raise ValueError("Compaction target must be below its trigger")
        return self

    @model_validator(mode="before")
    @classmethod
    def reject_retired_runtime(cls, values):
        # .env extras reach this validator; unknown process variables do not.
        selection = next((v for k, v in values.items() if k.lower() == "agent_arm"),
                         os.environ.get("AGENT_ARM"))
        check_retired_runtime(selection, "AGENT_ARM")
        return values

    # ── HTTP admission and deadlines ────────────────────────────────
    stream_idle_timeout: float = Field(default=120.0, gt=0,
                                       validation_alias="AGENT_STREAM_IDLE_TIMEOUT")
    stream_total_timeout: float = Field(default=300.0, gt=0,
                                        validation_alias="AGENT_STREAM_TOTAL_TIMEOUT")
    max_body_bytes: int = Field(default=12 * 1024 * 1024, gt=0,
                                validation_alias="AGENT_MAX_BODY_BYTES")
    max_concurrent_streams: int = Field(default=8, gt=0, validation_alias="AGENT_MAX_CONCURRENT")
    review_concurrency: int = Field(default=4, gt=0, validation_alias="AGENT_MAX_REVIEW")

    # ── TTS (Text-to-Speech) ───────────────────────────────────────
    tts_enabled: bool = False
    tts_voice: str = "zh-CN-XiaoyiNeural"

    # Require a verified host assertion before resolving caller identity.
    agent_require_assertion: bool = True

    # ── IoT 设备服务（ESP32 OLED 显示等）─────────────────────────────
    # 与博客共用 JWT_SECRET：agent 以对话用户身份签发 JWT 调用 device-service
    jwt_secret: str = ""
    device_service_url: str = "http://127.0.0.1:3100"
    # Optional demo device integration; hide unavailable tools and routes.
    iot_enabled: bool = False

    # Call the host backend with the verified caller identity.
    agent_admin_base: str = "http://127.0.0.1:3000"

    # Public host API used by tools and retrieval.
    blog_api_base: str = "https://saudade.site/api/public"

    # Corpus source for rebuilding the graph; it does not define the public site origin.
    graph_api_base: str = "http://127.0.0.1:3000/api/public"

    # Optional vector retrieval and graph embeddings share this configuration.
    # Changing embedding space requires rebuilding stored vectors.
    rag_hybrid_enabled: bool = False       # RAG_HYBRID_ENABLED=1/true/yes/on
    embedding_api_key: str = ""            # EMBEDDING_API_KEY
    embedding_base_url: str = ""           # EMBEDDING_BASE_URL（留空 = SDK 默认端点）
    embedding_model: str = "qwen3.7-text-embedding"
    # 0 = **不向 API 传 `dimensions`**，以返回向量的长度为准（不同供应商支持度不一，
    # 传了可能 400）。想要固定维度才填，填了则校验返回长度必须一致。
    embedding_dim: int = 0                 # EMBEDDING_DIM
    embedding_batch_size: int = 10         # EMBEDDING_BATCH_SIZE（聚合平台单请求上限）
    embedding_timeout: float = 15.0        # EMBEDDING_TIMEOUT（查询向量在热路径上，别设大）
    embedding_profile: str = ""            # EMBEDDING_PROFILE（可选、固定 tokenizer 的 JSON 路径）
    # 查询向量的内存 LRU。**只进内存不落盘**：查询串是访客可控的无界输入，落盘会长成
    # 一个没人清理的文件。语料侧的缓存是另一回事（内容寻址、落 data/rag_vectors/）。
    embedding_query_cache: int = 256       # EMBEDDING_QUERY_CACHE
    rrf_k: int = 60                        # RRF_K（标准常量；k 越大对低排名越宽容）
    rag_vector_dir: str = "data/rag_vectors"   # RAG_VECTOR_DIR

    # ── Logging ─────────────────────────────────────────────────────
    log_level: str = "INFO"

    # ── Tracing（对话执行 trace 落盘，见 utils/trace.py）──────────────
    # 每请求一份 JSON（输入/节点事件序列/分段耗时/回复/退出原因）；
    # 与项目 logs/ 目录对齐（日志体系规范见 CLAUDE.md §2），logrotate 轮转
    trace_dir: str = "/home/ubuntu/Saudade-Blog/logs/agent/traces"
    # Optional PostgreSQL conversation persistence. Empty disables persistence;
    # invalid configuration aborts startup without an in-memory fallback.
    # Use a separate database: framework setup manages its own schema.
    checkpoint_dsn: str = ""               # CHECKPOINT_DSN
    # Reject a business database name supplied by the deployment.
    checkpoint_forbid_db: str = ""         # CHECKPOINT_FORBID_DB

    # ── Active provider helpers ─────────────────────────────────────

    @property
    def _provider_prefix(self) -> str:
        """Normalize provider names consistently for validation and lookup."""
        return (self.llm_provider or "").strip().lower()

    def check_provider(self) -> str:
        """Reject unsupported providers before building the runtime."""
        prov = self._provider_prefix
        if prov not in KNOWN_LLM_PROVIDERS:
            raise ValueError(
                f"LLM_PROVIDER={self.llm_provider!r} 不是已知的服务商"
                f"（只有 {sorted(KNOWN_LLM_PROVIDERS)}）——拒绝启动。"
                "不回退默认：静默回退会把'这一刻是谁在跑'从日志里抹掉，"
                "而且此后每一条请求都会以某种方式不对（`/health` 不会报错）。")
        return prov

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

    @property
    def embedding_configured(self) -> bool:
        """Whether the active embedding provider has usable credentials."""
        key = self.embedding_api_key.strip()
        return bool(key) and key != "your-api-key-here" and bool(self.embedding_model.strip())


settings = Settings()

