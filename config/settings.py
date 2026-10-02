"""Application configuration using pydantic-settings.

Each LLM provider keeps its own environment variables.
Set ``LLM_PROVIDER=deepseek|qwen|openai`` to choose the active one.
"""

import os

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
        # `SAUDADE_IGNORE_ENV_FILE=1` ⇒ **整份 .env 不读**（环境变量照读）。离线套件用
        # （`tests/run_all.py` 给每个子进程设上）：本机 `.env` 是**产线那份**，离线判据若
        # 跟着它走，同一份代码在本机与 CI 上的结论会不同——而那正是 20260928 实测到的形状：
        # `tests/test_confirm.py` 的弹窗矩阵只有在 `JWT_SECRET` 非空时签得出令牌
        # ⇒ **本机恒绿、CI 恒红**，红得与代码一个字都没关系。判据钉代码形状，不钉这台机器
        # 恰好装了什么。要按别的档跑，自己显式构造（如 `test_planner_engine.py` 传参）。
        env_file=None if os.environ.get("SAUDADE_IGNORE_ENV_FILE") else ".env",
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

    # ── 服务间身份断言（20260917）────────────────────────────────────
    # Rust 用 jwt_secret 签 `X-Agent-Assertion: {sub: uid, aud: "agent", exp:+60s}`，
    # agent 验签后据此覆盖请求体里的 user_id（见 server._resolve_user_id）。
    # 默认 False 是**滚动上线**需要的：Rust 还没发这个头时打开它会把在途请求打成 401。
    agent_require_assertion: bool = False

    # ── 权限模型（20260920，秘书类功能地基）──────────────────────────
    # False（默认）= **shadow**：execute 照算 authz 决策、把「拒绝」记进 trace，
    # 但不改变行为。先跑一段真实流量看谁会撞上授予表边界，用证据校准
    # agent/authz.py 的角色→scope 表，再打开开关——与上面的断言开关同一条
    # 滚动上线纪律（先观测、后收口）。打开后拒绝走既有 blocked 链路。
    authz_enforce: bool = False

    # ── planner 接口层（20260927 新主线第一批）───────────────────────
    # 取值 `text`（默认，历史行为：渲染文本菜单 → 模型写五行文本 → 正则抠）
    # / `native`（API 的 tools 字段 + tool_calls 返回，见 agent/native_plan.py）
    # / `shadow`（两条路都跑一遍、只比对不改变行为；**只给离线/调试用**）。
    # 默认 `text` 是刻意的：上线后线上行为逐字节不变，回滚也只是把这个值改回来
    # ——不需要回滚代码。`shadow` 写进生产 .env 会让每轮 planner 多一次 LLM 调用。
    planner_engine: str = "text"
    # native 档是否开思考。**单独一个开关是刻意的**：它是三个待拍板项之一
    # （"开思考的预算"），要能单独开关才产得出对照数据；而文本档恒关思考
    # （`graph.py` 那行 enable_thinking=False 是实测拍出来的，别跟着这个值走）。
    planner_native_thinking: bool = True
    # native 档的预算。**不能沿用文本档的 400**：模型开思考时思考链先吃掉额度，
    # tool_call 的 arguments 会被截断在中途（finish_reason=length，JSON 都不完整）。
    # 值取自 eval/d4 与 native 探针的实测上界，见 docs/native-toolcalls-mainline.md。
    planner_native_max_tokens: int = 1200
    # 同理，文本档的 30s 正压思考档的 p50 6.4s / max 28.5s（实测）⇒ 留出余量。
    planner_native_timeout: float = 60.0
    # 慢轮告警阈值（秒）：超了就记 WARNING。**参数化是为了"不许只放宽不监控"**
    # ——放宽 timeout 而不看这个数，等于把超时问题藏起来。
    planner_native_slow_s: float = 30.0

    # ── 会话级任务状态（20260927 批 D）───────────────────────────────
    # 未完成的意图（"带我过去后开启一个特效"的第二步）跨轮的载体，与 execution_log
    # （已发生事实）对偶。表 `agent_task`（迁移 agent_task_20260927.sql，已跑）。
    # **默认 off 是刻意的**：表先落地、代码先上线，线上行为逐字节不变；
    # 打开才登记/注入。理由与三端链路见 agent/tasks.py 头注。
    agent_task_state: bool = False
    # 注入上限（读侧截断，存量行无需迁移——与 execution_log 去重"在读侧"同一条纪律）。
    # 3 条是"一张卡装一件事"的量级上限：再多只会挤掉 page_ctx / 执行记忆的预算。
    # **时效（72h）刻意不在这里再设一个值**：那需要解析 `created_at` 的钟面格式，
    # 而解析失败在注入侧的后果是整块静默消失（同"枚举只有一个入口"那类坑）——
    # 时效的单执行者是 Rust 读侧（`TASK_READ_TTL_HOURS`，见迁移头注）。
    agent_task_inject_max: int = 3

    # ── IoT 设备服务（ESP32 OLED 显示等）─────────────────────────────
    # 与博客共用 JWT_SECRET：agent 以对话用户身份签发 JWT 调用 device-service
    jwt_secret: str = ""
    device_service_url: str = "http://127.0.0.1:3100"
    # 物联网平台（EMQX + device-service + 静态控制台）是**可选件**：三块源码收在
    # 博客仓 `iot/`，装不装由部署者决定。**出厂默认关**——克隆下来直接部署的人没有
    # 这个平台，agent 却照样指路、narrator 照样介绍，就是系统在说假话。
    # 关掉时的四道收口（页面侧的收口在部署侧：nginx 不 include、Rust sitemap 不列）：
    #   ① `skills.NAV_MAP` 里那十个别名映射为 None，注记是「未部署」而非「已下线」；
    #   ② 提示词不介绍这个入口（`skills._nav_map_lines`）与不作为"真实页面"参照；
    #   ③ `tools.base` 的路径白名单不收 `/device-console/`；
    #   ④ `device_display`/`device_query` 两个技能在 `visible_skills` 里不可见。
    # ⚠️ 各项的判据都读**这一个值**（`IOT_ENABLED`），别再各写各的环境变量名。
    iot_enabled: bool = False

    # ── 管理助手：以发起人身份代调后台（20260921）─────────────────────
    # agent 管理助手要读后台数据（留言审核视图、全站用户统计），而 /api/protected/*
    # 那道门只认 admin。**通道是"以发起人身份代调"**：agent 用本轮的 uid 现签一个
    # 60 秒 JWT 打本机 Rust，Rust 的 auth_guard 照旧按 claims.sub 查库判角色
    # ——所以"谁问的"就是准入结果，agent 自己不持有任何后台凭据，也不新增一处
    # 授权判据（Rust 侧零改动）。
    # 只走回环：走公网域名会绕 nginx 一整圈，且这里没有任何需要跨机的理由。
    agent_admin_base: str = "http://127.0.0.1:3000"

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

