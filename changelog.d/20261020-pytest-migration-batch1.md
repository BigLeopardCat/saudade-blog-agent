- **测试体系转 pytest · 批 1（双轨起步）**：引入 pytest 轨道与旧 runner（`tests/run_all.py`）
  **并行跑同一批套件、红绿必须一致**，第一批迁 10 个套件 + 对拍判据。为什么不是"换条命令
  跑"：179 个套件此前是裸脚本判据——判据长在**模块级**（import 即执行）、报红靠 `sys.exit(1)`，
  pytest **收集即 import** 会把判据当场执行掉（sys.exit 直接杀收集器）；且一次性全量迁移的
  转写风险是"判据被改弱了还不知道"（FAILED 少 append 一条、环境钉子漂移、同进程收集的
  属性污染），只有两轨对拍抓得住。四件：
  ① **基建 `tests/conftest.py`（新）**：出厂档钉子与 `run_all._PINNED` 同源同值（收集前
  设置，早于一切 `config.settings` import）；tests/ 进 sys.path（`_ctx_src` 这批 `_*` 助手
  的脚本态 import 方式）；迁移台账 `_MIGRATED`（pytest 只收集台账内文件，其余
  `collect_ignore`——双轨期名单不可避免，配了判据防"名单忘了同步"）。dev 组钉版本加
  pytest 四件套，`pyproject.toml` 加 `[tool.pytest.ini_options]`（串行默认，CI 上 `-n auto`
  是后批的事）。
  ② **10 个试点套件 = 五种手法的代理人**：`test_write_facts`（B·纯脚本最普遍形态）、
  `test_ready`（B+import server 副作用面）、`test_authz` / `test_capability_denial`
  （B+拼接视图）、`test_settings_surface`（B+环境敏感）、`test_commit_msg_hook`
  （subprocess 壳）、`test_changelog_fragments`（元测试）、`test_cancel`（A·多函数共享
  FAILED）、`test_error_frame`（C·模块属性 save/restore）、`test_board_dto_keys`
  （跨语言守卫）。统一手法：模块级执行段包进文件级 `def test_xxx()`，内部 check 结构
  **原样保留**（print ✅/❌ 判据行 `-s` 可见），尾部 `assert not FAILED` 收口；判据行数/
  取值点**一个没动**——A 类的子函数改 `_` 前缀（判据不在函数内的独立收集是假绿）、
  `test_capability_denial` 函数内重复 import 撞 `UnboundLocalError` 改别名
  `import … as _cap_defs`（"拿定义重算"的行为锁语义保留）。
  ③ **`test_board_dto_keys` 的 skip 语义**：pytest 轨无父仓 ⇒ `pytest.skip(allow_module_level=True)`
  （黄不是红——"环境缺父仓"不该混进"契约真破了"）；两个前提都不许绕：直跑/`run_all`
  轨语义原样（无父仓照旧红，与基线 4 红一致）、设 `SAUDADE_REQUIRE_PARENT=1`（CI/夜间
  门禁）时**不 skip**——那时"跑不到"本身就该红。
  ④ **对拍判据 `tests/test_pytest_parity.py`（新）**：钉两个让对拍有意义的前提——台账
  登记/磁盘/collect_ignore 三方一致（防静默漏跑与假绿）、两轨 `_PINNED` 逐字同源
  （ast 取字面量、不 import run_all——import 它等于开跑全量）。红绿本身的比对是流程
  动作：本 PR 实测 pytest 轨 11 passed + 1 skipped、`run_all -j 4` 175/179（红名单与
  基线逐字一致：board_dto_keys / note_tags_codec / role_tables / write_param_words
  四个跨语言守卫）。**形状台账同步**：`conftest.py` 与 `run_all.py` 同列"非套件"例外
  （`run_all.suites()` / `test_ci_suite_list` / `test_tests_dir_shape` **三处同源**的
  枚举规则，ci_suite_list ④"枚举 == 磁盘"把漂移当场报出来）；复杂度台账 +3 条、模块
  台账改 1 条（转写包函数的固有形状：`test_authz::test_authz` C=202/1222 行等，
  `--update` 记账——套件文件的"巨型函数"正是文件级判据的载体，拆它 = 拆判据）。
