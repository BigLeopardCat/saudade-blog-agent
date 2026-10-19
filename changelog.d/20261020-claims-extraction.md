- **`graph.py` 判据族（53 函数 + 66 常量）整块搬进新模块 `agent/claims.py`**（graph.py
  11115→9469 行；⚠️ **agent 对外行为零变更**）。这一族是"从模型正文里判声称"的文本判据
  （命令前缀 `_CMD_PREFIX_RE`、写声称、缺席声称、能力否认、暗黑模式别名……），与图编排
  （节点/边/状态流转）本就是两件事；graph.py 是全仓最大文件、超预算台账第一名，本次评审把它
  列为 P0 之①。三件：
  ① **搬家不换门牌**：函数体逐字同源（53 个函数 `inspect.getsource` 与搬运脚本里的原文
  逐字比对通过），graph.py 顶部按原名 re-import（`from agent.claims import …`），下游
  `agent.graph.X` 引用零改动——先例是 textnorm / plan_codec / name_grounding / write_grounding
  四次同模式搬家，守卫 `tests/test_graph_reexports.py` 全绿。工具注册常量族（`_TOOLS` /
  `_TOOL_MAP` / `_TOOL_NAMES_ALT` 及派生正则）**刻意留在 graph.py**：它们被 claims 反向
  引用，跟着搬会成环。
  ② **取值点跟着代码走、判据字面不动**（`tests/_ctx_src.py` 的拼接视图先例）：
  `test_cross_language_tables.py` 的 SITES 表两处指向 claims（`_CMD_PREFIX_RE` 随迁、
  `PAYLOAD` 组帧正则留 graph——它本来就在图那一侧）、`_SWEEP_FILES` 收进 claims.py；
  `test_authz.py` / `test_capability_denial.py` 的源码视图改"graph + claims 拼接"；
  `eval/judge_registry.json` 33 条 file/id 从 graph 改 claims（词表登记表跟着家走），
  `eval/wordlist_gate.py --update` 台账重出（按文件穷举量棘轮）。
  ③ **顺手清掉 graph.py 里 15 个拆分后才暴露的 unused import**（F401；引用它们的判据族
  已随迁）。三个**保留**并压 `# noqa: F401`：`SITE_GUIDE` / `_title_relevant` /
  `_EXECUTOR_PROMPT`——仍被 tests 经 `agent.graph` 引用，门牌不许摘。台账与文档同步：
  `tests/test_module_budget.py --update`（claims.py 1920 行入豁免名单）、
  `docs/agent-architecture.md` §2 模块树（65→66 个模块）、`eval/lint_gate.py --update`
  台账收紧 772→754（graph.py 的 F401 清理是主要减项——记账跟上现实，别让还清的债留在
  基线里给将来占位）。全量回归 `run_all -j 4` 174/178，与搬前基线逐位相同（4 红全是
  需要父仓 Rust 源码的跨语言守卫，外部环境预期）。
