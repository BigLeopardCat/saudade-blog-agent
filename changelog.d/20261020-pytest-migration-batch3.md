- **测试迁移收尾（批 3）：`tests/run_all.py` 删除，CI 与夜间切到 `pytest`**。双轨期到此
  结束，`tests/` 从此就是一套**原生 pytest 套件**。逐项：
  ① **删 `tests/run_all.py`**（自研 runner，1 个文件）与其两根拐杖 `tests/test_pytest_parity.py`
  （台账/磁盘/`collect_ignore` 三方一致——台账没了，判据对象随之消失）、
  `tests/test_run_all_parallel.py`（判 `-j N` 的并行档——选项没了）。**出厂档钉子搬家不
  抄家**：`_PINNED` 从 run_all.py **搬进** `tests/conftest.py`，`_MIGRATED` 台账与
  `collect_ignore` 一并删掉（它们唯一的存在理由是"挡住未迁移的裸脚本"）。
  ② **CI 与夜间换入口**：`.github/workflows/eval.yml` 两处 `uv run python tests/run_all.py -j 4`
  → `uv run --frozen pytest -q -n 4`；`scripts/nightly_regression.sh` → `$PY -m pytest -q`
  （夜间不加 `-n`：在本机跑、没人等它，而本机内存比 runner 紧）。`SAUDADE_REQUIRE_PARENT`
  与父仓 checkout 的接线一字未动。
  ③ **一处判据换持有者，不是删**：「入口收走的就是盘上该收走的」原来拿 `run_all.suites()`
  的枚举去比磁盘，今天枚举规则归 pytest 自己 ⇒ 搬进 `tests/test_tests_dir_shape.py` ④，
  改判**两件真事**：`pyproject.toml` 的收集配置没被改掉（`python_files` / `testpaths`），
  以及**盘上每个 `test_*.py` 都至少有一个顶层 `def test_*`**（否则收 0 个用例、静默不跑
  ——这条只有 AST 看得出来，跑一遍看不出来）。同时 ④ 里加一条"**自研 runner 不许回来**"
  （多一个入口就是多一份清单）。`tests/test_ci_suite_list.py` ①/② 改判「跑的是 pytest」
  且「不许出现收窄收集面的选项」（`-k` / `--deselect` / `--ignore` / `--lf` 一族——原来
  只判 `run_all.py -k` 一个口，那一个口随入口一起消失了）。
  ④ **五处"读 runner 源码"的判据跟着搬**：`test_fastpath_dial` / `test_hardening` /
  `test_provenance`（读 `_PINNED` 的源码文本或 AST）、`test_wordlist_gate` ⑧c（判"靠按
  目录收、不靠点名"）、`test_ci_suite_list` ④b（探针把钉子喂给子进程）——**判据的意思一字
  未变，只有持有者从 run_all.py 换成 conftest.py**。
  ⑤ **覆盖率那一档换实现**：原 `run_all.py --coverage`（逐套件 `coverage run` 子进程 +
  结尾 `combine`）改成 `scripts/dev/coverage.sh`（`pytest --cov`，1 行）。**两处麻烦同时
  消失**：产线 venv 里原本没有 coverage ⇒ 旧配方要临时 `uv venv /tmp/cov` 再把
  site-packages 挂 `PYTHONPATH`；旧配方还要自己给子进程挂 `tests/`（今天归 conftest）。
  `test_packaging.py` ⑤ 里围着旧配方写的那几条判据不是"漏了"，是**对象没有了**——换成
  判新脚本的四个真事实（落点两处同源 / 先清残片 / 射程只一处事实源 / 不再临时建 venv）。
  ⑥ **存量文档与注释跟着走**（51 个文件）：README / CONTRIBUTING / PR 模板 / issue 模板 /
  `config/settings.py` 三处 / `rag/search.py` / `agent/graph.py` / `agent/checkpoint.py` /
  `utils/trace_files.py` / `eval/` 六处头注 / 以及 30 多个套件头注里"由 run_all.py 按磁盘
  枚举自动收"这类话。**记录型文档只加标记、不重写**：CHANGELOG / `changelog.d/` / 三份 ADR /
  `docs/问题记录.md` 里"当时跑的是 run_all.py"那些句子照旧，只在同一行标「（已删）」。
  ⑦ **棘轮一道没放宽**：`eval/lint_gate.py` 基线不动；复杂度台账 `--update` 前逐条审计过
  ——**只有两条动**，一条是 `test_ci_suite_list`（C 46→40、行 169→166，都**变小**），另一条
  是 `test_packaging`（**C 124 未动**、行 315→317，多出来的两行是新增判据）。长段说明一律
  搬到函数外（模块级注释不算函数行数，函数体本身没长大）。
