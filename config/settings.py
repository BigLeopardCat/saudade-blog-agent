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
        # 恰好装了什么。要按别的档跑，自己显式构造（如 `test_llm_usage.py` 传参）。
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
    # 采样种子（0 = 不设，走服务商自己的随机）。**默认不设是刻意的**：设了 seed
    # 等于换一条采样通路（服务商侧的对齐/批处理都可能变），在本仓"换臂必须两臂
    # 交替读计数"的纪律下，没有 A/B 就不该把一个未验证的通路设为默认。
    # 它在这里是为了**调优实验**：`LLM_SEED` 可逐臂拨，和温度分开成两个因子。
    llm_seed: int = 0

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
    # **接口只有一种：native tool calls**（API 的 `tools` 字段 + `tool_calls` 返回，
    # 见 agent/native_plan.py）。20261004 把"文本契约"那一档连同它的解析器整族删掉
    # ——全量 384 份 trace 里那条兜底路径 `native_fallback` 命中 **0** 次，即删掉它
    # 不改变任何一轮生产对话的形状；留着一个从不被走到的分支只会让"模型没做出决策"
    # 与"响应不可解析"混成同一个归宿。所以**没有** `planner_engine` 这个拨盘了：
    # 别再加快关回来，多一档就是多一条没人验证的通路。
    # native 是否开思考。**单独一个开关是刻意的**：它是三个待拍板项之一
    # （"开思考的预算"），要能单独开关才产得出对照数据。
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
    # 规划温度。**20261006 由写死的 0.2 改为 0.0（路由确定性）**：决策是分类不是
    # 创作，而 8 次同分母全量把抖动量化出来了——134 条里 **41 条的 round 0 决策在
    # 8 跑里换过技能**，其中 27 条**输入逐字相同**（页上下文/recent_tail/短应答/
    # 文档锚点全等、时钟归一化后比字节）却仍然换分支 ⇒ 那 27 条是**纯采样**，
    # 温度是唯一能治它们的旋钮（另 51 条输入本身有差异——真实库值/台账行首年龄——
    # 温度治不了，如实记着）。分布也支持这个读法：41 条里 27 条是 5:1 或 6:1 的
    # **尾事件**，正是"偶尔掉出众数分支"的形状。
    # **可环境变量覆盖（PLANNER_TEMPERATURE）**：即将进行的"不同模型/不同参数"
    # 调优实验要能逐臂拨它，而改代码跑矩阵等于每臂一次提交，不可比。
    # 回退路径：PLANNER_TEMPERATURE=0.2 即逐字节回到 20261006 之前的行为。
    planner_temperature: float = 0.0

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

    # ── 工具出口：博客公开接口的基址（20261006）───────────────────────
    # `tools/base.py` 的 `API_BASE` 就是它：所有读接口（文章、分类、标签、留言板、
    # 站内搜索、知识库）与 **RAG 语料**（`rag/search.py::_fetch_corpus`）都以它为前缀。
    # 默认值是**本站的公网域名**——生产一直这么跑（与上面 `graph_api_base` 默认走回环
    # 不同：那个只服务后台建图任务，这个是访客热路径，历史如此，别顺手"统一"）。
    # 自己部署的人**必须改**：不改的话工具问的是本站，拿回来的答案与你自己的库无关，
    # 而 golden 的语料前置会先把它拦成「未评估」（见 `eval/golden/provenance.json`）。
    blog_api_base: str = "https://saudade.site/api/public"

    # ── 向量图谱重建（20261003 用户第 2 条）────────────────────────────
    # 建图脚本从哪个公开接口拉语料。**只影响"读文章与读数"，不影响产物归属站点**：
    # 归属站点（manifest.site）由后台页面把浏览器自己的 origin 传上来——若从这里推，
    # 推出来的是 `http://127.0.0.1:3000` 而访客在 `https://<域名>`，两个 origin 一比
    # 不一致，首页就永远不画图（且不报错，只是没有图）。
    # 迁移到新机器时若 Rust 不在 3000，改这一项。
    graph_api_base: str = "http://127.0.0.1:3000/api/public"

    # ── 向量 + RRF 混合检索（20261005）───────────────────────────────
    # 检索有两路：词法（`rag/search.py` 的 BM25，恒在）与向量（本块配置的
    # embedding 端点）。**开关只有这一个值**（`rag_hybrid_enabled`）：关掉 = 逐字节
    # 退回今天的纯词法输出（含 BM25 原分与相对断崖），不是"少一路、分数换个尺度"。
    # 语料小的时候词法基线本身就打满（22 query 实证），要开多半是为了"语料长大 /
    # 语义型 query 变多"，所以**出厂默认关**——按文章规模自己拨，与 `iot_enabled`
    # / `agent_task_state` 同一条纪律。
    #
    # 向量模型**独立配置、供应商不进代码**：任何 OpenAI 兼容的 embeddings 端点都行。
    # 用聚合平台的话，把 `QWEN_BASE_URL` / `QWEN_API_KEY` 的值**复制**过来一份：
    #   EMBEDDING_BASE_URL=<与 QWEN_BASE_URL 同值>
    #   EMBEDDING_API_KEY=<与 QWEN_API_KEY 同值>
    #   EMBEDDING_MODEL=<聚合平台上的 embedding 模型名，如 text-embedding-v4>
    # ⚠️ 刻意**不**回落 `active_llm_*`（20260927 的教训，见 rag/wordgraph.py:169）：
    # 跟着 active provider 走，切到一个没有 embeddings 端点的 provider 就会哑掉，
    # 而且哑得没有声音。独立一份 = 换对话模型不会顺带打断向量路。
    # 代价是换聚合平台要改两处，这是"独立配置"的必然代价，不替它们做隐式绑定。
    rag_hybrid_enabled: bool = False       # RAG_HYBRID_ENABLED=1/true/yes/on
    embedding_api_key: str = ""            # EMBEDDING_API_KEY
    embedding_base_url: str = ""           # EMBEDDING_BASE_URL（留空 = SDK 默认端点）
    embedding_model: str = ""              # EMBEDDING_MODEL（留空 = 未配置，向量路不可用）
    # 0 = **不向 API 传 `dimensions`**，以返回向量的长度为准（不同供应商支持度不一，
    # 传了可能 400）。想要固定维度才填，填了则校验返回长度必须一致。
    embedding_dim: int = 0                 # EMBEDDING_DIM
    embedding_batch_size: int = 10         # EMBEDDING_BATCH_SIZE（聚合平台单请求上限）
    embedding_timeout: float = 15.0        # EMBEDDING_TIMEOUT（查询向量在热路径上，别设大）
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

    @property
    def embedding_configured(self) -> bool:
        """向量路**凭据**是否齐（key 与 model 都非空）。

        只判"配置齐没齐"，不判"向量库建好没建好"——后者是运行时状态，判据在
        `rag/vector_index.py::degraded_reason()`。两件事分开，是因为它们各自的
        处置不同：配置缺 = 这个部署从来没打算开向量路；库没建好 = 正在建 / 建挂了。
        """
        key = self.embedding_api_key.strip()
        return bool(key) and key != "your-api-key-here" and bool(self.embedding_model.strip())


settings = Settings()

