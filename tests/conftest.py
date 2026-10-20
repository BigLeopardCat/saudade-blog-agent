# -*- coding: utf-8 -*-
"""pytest 的测试现场（20261020 批 1：测试体系转 pytest 的双轨起步）。

**为什么是双轨**：tests/ 下 179 个套件此前是「自研 runner（tests/run_all.py）+
裸脚本判据」的体系——判据长在**模块级**（import 即执行）、报红靠 `sys.exit(1)`、
输出靠 print 的 ✅/❌ 判据行。转 pytest 不是换条命令跑：**收集即 import** 会把裸脚本
的判据当场执行掉（sys.exit 直接杀收集器）。所以迁移按批走（每批一个 PR），期间
**两个 runner 并行跑同一批文件、红绿必须一致**（对拍判据 `test_pytest_parity.py`），
末批删 run_all。对拍不是形式：转写时把判据改弱（比如 FAILED 少 append 一条）、
conftest 的环境钉子与 run_all 的 _PINNED 漂移、同进程收集带来的属性污染——
这三种「看着迁移了、其实判据变了」只有对拍抓得住。

三件事（顺序敏感，逐条说明）：

① **出厂档环境钉子**（必须在任何 `config.settings` 的 import 之前设置——conftest
   在收集前加载，早于一切测试模块，这个顺序由 pytest 保证）。与
   `tests/run_all.py` 的 `_PINNED` 同源同值：离线判据必须钉在**代码的形状**上，
   不能跟着本机 `.env`（产线那份）或 shell 里恰好 export 过的档位走——那正是
   20260928 实测到的「本机恒绿、CI 恒红」形状。两处 _PINNED 的**一致性本身有判据**
   （`test_pytest_parity.py` ②），改一处不改另一处当场红。

② **tests/ 进 sys.path**：`_ctx_src` / `_planner_stub` / `_rust_src` 这批 `_*` 助手
   按脚本态的 import 方式（`import _ctx_src`）工作，pytest 的 rootdir 导入模式不会
   自动把 tests/ 放进 path——不补这一行，试点里三个拼接视图套件当场
   ModuleNotFoundError。

③ **迁移台账 `_MIGRATED`（本文件是真值源）**：pytest 只收集台账内文件，其余
   `test_*.py` 全部 `collect_ignore`。台账是**逐批增长**的（每个迁移 PR 往里加），
   末批清空、collect_ignore 退化为空。判据 `test_pytest_parity.py` ① 钉着
   「台账里的文件都真实存在且含 `def test_`（真的可被收集），台账外一个不多收」
   ——防止「名单忘了同步」造成的静默漏跑（那正是 run_all 头注里「按磁盘枚举、
   不写名单」要防的同一种坏法；双轨期名单不可避免，所以给它配了判据）。
"""
from __future__ import annotations

import os
import pathlib
import sys

# ① 出厂档钉子（值与 tests/run_all.py::_PINNED 逐字同源；parity ② 对拍）
_PINNED = {"AGENT_EVIDENCE": "0",
           "AGENT_FASTPATH": "1",
           "RAG_HYBRID_ENABLED": "0",
           "CHECKPOINT_DSN": "",
           # `AGENT_REQUIRE_ASSERTION`（20261019 安全审计 SAU-005，随并进 main 同步）：
           # 出厂档是 1（默认强制服务间身份断言）——不钉的话谁在 shell 里 export 过 0，
           # 凡是经由 `server._resolve_principal` 的用例都会按另一档判、红得与代码无关。
           # 要验回退档的用例在同一进程里临时拨 `settings.agent_require_assertion`。
           "AGENT_REQUIRE_ASSERTION": "1",
           "SAUDADE_IGNORE_ENV_FILE": "1",
           "IOT_ENABLED": "1"}
for _k, _v in _PINNED.items():
    os.environ[_k] = _v

# ② 助手模块的 import 方式与脚本态一致（run_all 是靠子进程 cwd=tests/ 的隐式 path；
# pytest 的收集不经过那一条，这里显式补上）
sys.path.insert(0, str(pathlib.Path(__file__).parent))

# ③ 迁移台账：批 1 = 试点 10 个 + 对拍判据自己。
#    每条后面标它代表的「迁移手法/风险面」，批 2 起按主题批量扩。
# ③ 迁移台账：批 2（20261020）起 = **盘上全部 `test_*.py`**（179 个）——
#    批 1 那 11 个的逐条注解见 git 历史；到这里双轨的**文件面**已经收敛，
#    台账不再有区分力，末批（删 run_all 那一批）把它与 `collect_ignore` 一并删掉。
_MIGRATED = frozenset({
    "test_account_freeze",
    "test_account_mute",
    "test_account_role",
    "test_account_roster",
    "test_action_text",
    "test_admin_write",
    "test_arm_capability",
    "test_artifact_retention",
    "test_async_blocking_join",
    "test_authz",
    "test_authz_shadow",
    "test_baseline_group",
    "test_block_reasons",
    "test_blog_api_base",
    "test_board_dto_keys",
    "test_board_visibility_judgement",
    "test_cancel",
    "test_capability_denial",
    "test_capability_truth",
    "test_changelog_fragments",
    "test_chat_quota",
    "test_checkpoint_adoption",
    "test_checkpoint_cascade",
    "test_checkpoint_drop_turn",
    "test_checkpoint_factory",
    "test_checkpoint_setup_concurrent",
    "test_ci_suite_list",
    "test_cliff_min_keep",
    "test_commit_msg_hook",
    "test_complexity_budget",
    "test_confirm",
    "test_confirm_leftovers",
    "test_confirm_second_write",
    "test_corpus_invariants",
    "test_corpus_terms",
    "test_cross_language_tables",
    "test_daily_metrics",
    "test_dial_matrix",
    "test_docs_facts",
    "test_docs_links",
    "test_effect_truthfulness",
    "test_embed_space",
    "test_entities",
    "test_error_frame",
    "test_exec_frame_incremental",
    "test_exec_rows_anchors",
    "test_exec_search_evidence",
    "test_factblock",
    "test_fastpath_dial",
    "test_frame_budget",
    "test_frame_caps",
    "test_frame_contract",
    "test_gate_replan",
    "test_golden_arm",
    "test_golden_corpus_provenance",
    "test_golden_family_policy",
    "test_golden_fixture",
    "test_golden_fixture_corpus",
    "test_golden_full_run",
    "test_golden_keys",
    "test_golden_prod_history",
    "test_golden_rerun_offline",
    "test_golden_runner_parity",
    "test_golden_thread",
    "test_golden_trace",
    "test_graph_build",
    "test_graph_reexports",
    "test_hardening",
    "test_health_dials",
    "test_hostinfo_trace_stats",
    "test_id_namespaces",
    "test_idem_noop",
    "test_identity_preflight",
    "test_intents_backfill",
    "test_iot_switch",
    "test_item_position_guide",
    "test_judge_offline",
    "test_landing_gate",
    "test_ledger_absence",
    "test_ledger_age",
    "test_link_shape",
    "test_llm_judge",
    "test_llm_param_knobs",
    "test_llm_provider_registry",
    "test_llm_usage",
    "test_logging_setup",
    "test_menu_deny",
    "test_module_budget",
    "test_module_exports",
    "test_module_index",
    "test_narrator_cache_layout",
    "test_narrator_empty_retry",
    "test_narrator_toolcall_correct",
    "test_narrator_window",
    "test_nav_truthfulness",
    "test_note_stats",
    "test_note_tags_codec",
    "test_own_action_conflict",
    "test_packaging",
    "test_param_matrix_tokens",
    "test_param_schema",
    "test_parent_repo_paths",
    "test_pending_ledger",
    "test_pg_retention",
    "test_pg_retention_live",
    "test_plan_channel",
    "test_plan_executor",
    "test_planner_llm",
    "test_planner_llm_error",
    "test_planner_wiring",
    "test_process_isolation",
    "test_prompt_prefix",
    "test_provenance",
    "test_pytest_parity",
    "test_query_expansion",
    "test_react_arm",
    "test_react_line",
    "test_react_narrator_assets",
    "test_ready",
    "test_reconcile_offline",
    "test_reflector_output",
    "test_registry_sync",
    "test_report_archive",
    "test_report_digest_structure",
    "test_reports",
    "test_reset_scope",
    "test_retention_manifest",
    "test_role_tables",
    "test_round_facts",
    "test_run_all_parallel",
    "test_sections",
    "test_session_input_source",
    "test_settings_surface",
    "test_side_tasks",
    "test_silent_degradation",
    "test_slim_workflows",
    "test_source_attribution",
    "test_state_keys",
    "test_state_reducers",
    "test_status_judgements",
    "test_sticker_repair",
    "test_structured_carrier",
    "test_style_hygiene",
    "test_sys_fetch_claim",
    "test_tag_admin",
    "test_tag_color_tables",
    "test_target_grounding",
    "test_task_continuation",
    "test_task_expiry",
    "test_task_ledger_probe",
    "test_task_steps",
    "test_tasks",
    "test_tests_dir_shape",
    "test_todo_schedule",
    "test_tool_call_ir",
    "test_tool_call_pairs",
    "test_tool_kind_contract",
    "test_tools_desc",
    "test_trace_io",
    "test_trace_reply_source",
    "test_trace_retention",
    "test_trace_truncation",
    "test_truth_source_parity",
    "test_turn_window",
    "test_undefined_names",
    "test_user_notice",
    "test_userdata",
    "test_vector_index",
    "test_version",
    "test_widget_pin_drift",
    "test_word_graph_build",
    "test_wordlist_gate",
    "test_workflows",
    "test_write_channel",
    "test_write_dead_end_guard",
    "test_write_done_claim",
    "test_write_facts",
    "test_write_idempotency",
    "test_write_param_words",
    "test_zako_role",
})

# 台账外的 test_*.py 一律不收集（理由见模块头注 ③；`test_*` 通配与 pytest 默认
# 收集规则一致，所以这里只需要「排除」不需要「点名」）。
_here = pathlib.Path(__file__).parent
collect_ignore = sorted(
    p for p in _here.glob("test_*.py") if p.stem not in _MIGRATED
)


# ── 跨套件全局的**保存 / 还原**（20261020 批 N+1）────────────────────────────
#
# 为什么必须有这一层：这 179 个套件此前是「**一个套件一个进程**」跑的（`run_all.py`
# 一个套件起一个子进程）⇒ 谁改了 `settings.jwt_secret`、谁把 `server._agent` 换成了
# 桩、谁 `sys.modules.pop("agent")`，**都不会影响别人**，所以没人写过还原。
# 全量进**一个** pytest 进程之后，这些全变成跨套件的隐式耦合：实测 179 个里 22 个
# 跑完没还原，其中 6 个当场把**别的**套件打成红（受害者看着像自己的判据坏了）。
#
# 这一层**只保护受害者**，它不假装那些套件干净：还原了什么**逐条打印**出来，
# 谁漏了还原、漏的是什么，看得见（`-s` 或失败时在 captured stdout 里）。
# 判据是 `test_process_isolation.py`：它拿两个「污染/被污染」的最小对子，
# 反控证明这一层**真的在还原**（把它关掉，那一对当场红）。
import pytest  # noqa: E402

# 被套件直接打桩/改写的模块全局（`patch()` 就是 `setattr(模块, 名, 桩)`）。
#
# 名单怎么来的（**别靠印象**）：对 `tests/*.py` 做一次 AST 普查，收集所有
# 「`<模块别名>.<属性> = …`」形态的赋值（`Import`/`ImportFrom` 的别名表还原成
# `sys.modules` 里的键），落点就是下面这些。比"逐个套件跑一遍看谁脏"更全：静态扫描
# 能看见**只在某条分支上**才发生的赋值，跑一遍未必命中。
#
# 名单是**静态扫描 ∪ 实测**两半的并集，缺一不可：
#   · 静态那半由判据 `test_process_isolation.py` 钉着（扫到的模块必须都在名单里，
#     新套件打了新模块的桩而忘了加名单 ⇒ 那条当场红）；
#   · 实测那半是 `patch(...)` / `setattr(模块, 名, 桩)` 这类**静态看不见**的落点
#     （`agent.confirm` / `agent.prompts` / `tools.notes` / `build_word_graph` 等
#     8 个就是只有跑一遍才现形的）——判据管不到，所以**不许**按"扫描没扫到"删它们。
#
# **为什么不能"把所有 sys.modules 都存一份"**：实测 1879 个模块 / 60105 个条目，
# 一次快照 ~10ms（一条用例两次 ⇒ 全量多 ~7s），更要命的是会把 `_pytest.*` 自己的
# 全局也纳入还原 —— 那些是 pytest 运行期要动的。所以名单只收**工程自己的模块**。
#
# 已知没收进来的：`sys.stdout`（`test_undefined_names` 那类换 stdout 的）。换 stdout
# 在 pytest 里是**会话级**的坑（模块级换掉 ⇒ 整个会话崩），不是"跨套件污染"，
# 该由那个套件自己收口，不归这一层。
_WATCHED_MODULES = (
    # 被打桩最密的几处（`agent.graph.get_llm` 90 处、`tools.base._client` 161 处、
    # `server._agent` 36 处 —— 三条都在十几个套件里出现）
    "agent.graph", "agent.react_arm", "agent.react_line",
    "agent.adminops_todo", "agent.adminops_confirm", "agent.confirm",
    "tools.base", "tools.device", "tools.notes", "server",
    "agent.workflow_tools", "agent.prompts",
    # 同一轮普查里出现的其余落点：检索侧（`rag.search._index` 就是
    # `test_cliff_min_keep` 把进程级单例换成 6 篇夹具索引、`test_gate_replan`
    # 因此看不见真语料的那一处）、上下文/授权/计划侧、以及 eval/ 下的共用助手
    # （助手模块同样进 sys.modules，被 A 套件打桩后 B 套件读到的是桩）。
    "agent.authz", "agent.checkpoint", "agent.deterministic_plan",
    "agent.hostinfo", "agent.planner_llm", "agent.prompt_context",
    "agent.tasks", "agent.workflow_plan", "config.settings",
    "rag.graph_build", "rag.search", "rag.vector_index", "rag.wordgraph",
    "utils.clock", "utils.trace",
    "build_word_graph", "corpus_provenance", "dial_matrix", "golden_fixture",
    "golden_fixture_todo", "golden_trace", "param_matrix", "report_archive",
    "run_golden",
)


def _snapshot() -> dict:
    """量**已经 import 过**的那几份全局 —— 不主动 import，免得改掉现场。"""
    snap: dict = {"env": dict(os.environ), "path": list(sys.path),
                  "modules": dict(sys.modules), "mods": {}}
    m = sys.modules.get("config.settings")
    if m is not None:
        snap["settings"] = dict(vars(m.settings))
    for name in _WATCHED_MODULES:
        mod = sys.modules.get(name)
        if mod is not None:
            snap["mods"][name] = dict(vars(mod))
    return snap


def _restore(snap: dict) -> list[str]:
    """还原，返回**人话**形式的差异清单（空 = 这个套件干净）。"""
    drift: list[str] = []
    if "settings" in snap:
        obj = sys.modules["config.settings"].settings
        for k, v in snap["settings"].items():
            if getattr(obj, k, None) is not v and getattr(obj, k, None) != v:
                drift.append(f"config.settings.settings.{k}")
                setattr(obj, k, v)
    for name, attrs in snap["mods"].items():
        mod = sys.modules.get(name)
        if mod is None:
            continue
        for k, v in attrs.items():
            if getattr(mod, k, None) is not v:
                drift.append(f"{name}.{k}")
                setattr(mod, k, v)
        for k in [k for k in vars(mod) if k not in attrs]:
            drift.append(f"{name}.{k}（新加）")
            delattr(mod, k)
    for k, v in snap["env"].items():
        if k.startswith("PYTEST_"):
            continue
        if os.environ.get(k) != v:
            drift.append(f"env.{k}")
            os.environ[k] = v
    for k in [k for k in os.environ if k not in snap["env"] and not k.startswith("PYTEST_")]:
        drift.append(f"env.{k}（新加）")
        del os.environ[k]
    if list(sys.path) != snap["path"]:
        drift.append("sys.path")
        sys.path[:] = snap["path"]
    for k, v in snap["modules"].items():
        if sys.modules.get(k) is not v:
            drift.append(f"sys.modules[{k!r}]")
            sys.modules[k] = v
    # `lru_cache` 是**进程级**的，而它缓存的正是"当时的注册表长什么样"：某个套件把
    # 工具的 `args_schema` 打桩、顺手调了一次 `_tools_desc_cached`，缓存里就留着一份
    # **降级后的菜单**（实测：`search_notes(keyword:str*)` 变成 `search_notes()`），
    # 后面的套件"现算 vs 缓存"对不上。清掉即可 —— 缓存是纯函数，重算没有代价。
    for name in (*_WATCHED_MODULES, "agent.graph"):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        for k, v in vars(mod).items():
            clear = getattr(v, "cache_clear", None)
            if callable(clear):
                clear()
    return drift


@pytest.fixture(autouse=True)
def _process_isolation(request):
    """每条用例前后把跨套件全局还原（差异逐条打印，见上面那段头注）。"""
    snap = _snapshot()
    yield
    drift = _restore(snap)
    if drift:
        print(f"\n[跨套件全局] {request.node.nodeid} 改了没还原："
              + "、".join(dict.fromkeys(drift)))
