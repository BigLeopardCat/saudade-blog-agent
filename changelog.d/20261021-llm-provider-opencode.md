- 新增第四家 LLM 服务商 **`opencode`**（opencode zen 网关，OpenAI 兼容）：
  `config/settings.py` 的 `PROVIDER_DEFAULTS` 加一项 + 三个 `OPENCODE_*` 字段，
  `models/llm.py` 加 `_provider_headers()`、`_thinking_extra` 那一支、方言表把它登记进
  `json_schema` 原生地支。**两条与别家不同的实测约束**（写在注册表注释里，改这一家之前先看）：
  ① 必须带 `x-opencode-session` 头，缺了回 400 `MissingSessionID`，而 `ChatOpenAI` 只会发
  `Authorization` ⇒ 由 `_provider_headers()` 补；② 免费档（`*-free`）只在 **`/zen/go/v1`**
  这个端点可用，非 go 的 `/zen/v1` 回 403 `FreeTierError`，端点自带月度额度、用光回 429
  `GoUsageLimitError`。`_thinking_extra` 对这家**刻意一个键都不发**：免费档里唯一又快又稳的
  `minimax-m3.1-flash-free` 见 `thinking` 键回 400 ⇒ 不发的代价是零。
- **为什么这件事非并进 main 不可**：产线 `.env` 是 `LLM_PROVIDER=opencode`，而这段注册表
  此前**只活在产线那份未提交的工作区里** ⇒ `origin/main` 上**没有任何一版能在这台产线上
  启动**（`check_provider()` 直接 `ValueError: LLM_PROVIDER='opencode' 不是已知的服务商`
  ⇒ 导入期崩溃循环，`Restart=always` 把它变成 `NRestarts` 一路涨）。这条部署路径从 opencode
  上线那天就断了，只是没人走到 `pull` 那一步；20261021 实测踩到（产线停服一次）。
- 文档面跟着改：`README.md` 的 `LLM_PROVIDER` 那一行、`docs/agent-architecture.md` 两处
  provider 枚举（此前都写「三选一」，而注册表已经是四家）。
- **判据**：`tests/test_llm_provider_registry.py` 的集合断言（三家 → 四家）；
  `tests/test_settings_surface.py` 的豁免名单**不再手抄**——改成从 `PROVIDER_DEFAULTS`
  的键派生，于是「给 settings 加第五家」只改一处，不会再红在「名单漏了」上（那正是本仓
  点过名的「手抄第二份名单」）。`.env.example` 补 `OPENCODE_API_KEY` / `OPENCODE_BASE_URL` /
  `OPENCODE_MODEL` 三格（判据 ② 的覆盖要求）。
