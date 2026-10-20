# -*- coding: utf-8 -*-
"""pytest 的测试现场（20261020 起：`tests/` 就是一套原生 pytest 套件）。

**这一层只做两件事**（顺序敏感，逐条说明）：

① **出厂档环境钉子**（必须在任何 `config.settings` 的 import 之前设置——conftest
   在收集前加载，早于一切测试模块，这个顺序由 pytest 保证）。`_PINNED` 是**唯一事实源**：
   离线判据必须钉在**代码的形状**上，不能跟着本机 `.env`（产线那份）或 shell 里恰好
   export 过的档位走——那正是 20260928 实测到的「本机恒绿、CI 恒红」形状。改这张表
   等于改全部套件的跑法，`tests/test_ci_suite_list.py` ④b 判着「钉了真的生效」。

② **tests/ 进 sys.path**：`_ctx_src` / `_planner_stub` / `_rust_src` 这批 `_*` 助手
   按脚本态的 import 方式（`import _ctx_src`）工作，pytest 的 rootdir 导入模式不会
   自动把 tests/ 放进 path——不补这一行，三个拼接视图套件当场 ModuleNotFoundError。

③ **跨套件全局还原层**（`_process_isolation`，见文件末尾那一段）。

**这套体系怎么来的**（留档，不是现状）：这 179 个套件此前是「自研 runner
（`tests/run_all.py`）+ 裸脚本判据」——判据长在**模块级**（import 即执行）、报红靠
`sys.exit(1)`。转 pytest 不是换条命令跑：**收集即 import** 会把裸脚本的判据当场执行掉。
所以迁移分两批走、期间两个 runner 并行跑同一批文件、红绿必须逐字一致（对拍判据
`tests/test_pytest_parity.py`），末批把 `run_all.py` / 台账 / `collect_ignore` 一并删掉。
对拍不是形式：转写时把判据改弱（FAILED 少 append 一条）、两处 `_PINNED` 漂移、同进程
收集带来的属性污染——这三种「看着迁移了、其实判据变了」只有对拍抓得住。
逐批的过程与读数见 `changelog.d/20261020-pytest-migration-*.md`。
"""
from __future__ import annotations

import os
import pathlib
import sys

# ① 出厂档钉子（**唯一事实源**；`tests/test_ci_suite_list.py` ④b 判「钉了真的生效」）
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

# ② 助手模块的 import 方式与脚本态一致（`_ctx_src` 这批按 `import _ctx_src` 工作）
sys.path.insert(0, str(pathlib.Path(__file__).parent))


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
