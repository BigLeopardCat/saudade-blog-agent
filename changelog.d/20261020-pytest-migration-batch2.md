- **测试迁移批 2：179 个裸脚本套件全部转原生 pytest**（工装见上一条）。`tests/conftest.py`
  的 `_MIGRATED` 台账一次性填满（= 盘上全部 `test_*.py`），`collect_ignore` 随之退化成空。
  转写由 `scripts/dev/pytest_migrate.py` 完成，**165 个纯自动 / 14 个不动人工**：14 里
  11 个本来就是原生 pytest 形态（PR #38 的 10 个试点 + `test_pytest_parity`）—— 其中 8 个
  一字未动，3 个（`test_authz` / `test_cancel` / `test_error_frame`）只被 isort 自动修复
  碰了导入排序；另 3 个"值依赖运行时状态"的走人工点名（见下一条）。**放行口不是"转写成功"，是三层对拍
  `scripts/dev/migration_parity.py`**：判据条数与常量参数逐字相同 / 两轨退出码等价 /
  注入一条必假判据两轨都必须红。现况 **172 三层全过 / 0 红 / 8 未评估**（未评估 = 需要
  真库 DSN 的两件、本来就是原生 pytest 形态的一件、以及四件**形态 A′**（入口函数自己
  攒退出码、没有 `check()` 收集器 ⇒ 第 ③ 层没有注入点）。**报黄不报绿，退出码 3**）。

- **迁移把整棵 `tests/` 顶出了三道棘轮，三道都**没有**放宽基线**（这是本批最费工的一段，
  因为"迁移顺带把闸放宽"是最容易蒙混过去的那种坏）：
  ① **lint（`eval/lint_gate.py`）**：+4 缩进把 465 行顶过 100 列、工装留下的空行让 isort
  判 429 条 I001。做法是先 `ruff check --fix --select I001 tests/`（纯排序，零语义），再把
  **"迁移顶长"的那些行**（现在 >100 列、去掉那 4 个空格后 ≤100、且逐字出现在 HEAD 里 ——
  这个三重指纹才分得清"被顶长的"与"本来就长的"）就地折行。两处非显然：**ruff 的列宽不是
  字符数**（东亚宽字符算 2 列，实测 96 字符的 10 汉字行报 106），照字符数折会折了个寂寞；
  折法只在**括号内**的逗号/运算符处断，字符串一整行都是中文时按标点劈成**相邻字面量**
  （隐式拼接，值逐字不变）。收尾读数 **628 条 / 131 个文件（基线 749 / 148）** —— 比基线
  **少 121 条**，17 个文件清零销账，基线随之收紧。
  ② **复杂度台账**（`tests/test_complexity_budget.py`）：折行让 42 条目的**行数**变大。
  `--update` 之前逐条审计过：**`c` 一条都没动**（122 条全同），净增 153 行全是折出来的行。
  ③ **模块台账**（`tests/test_module_budget.py`）：3 条变大（`scripts/dev/pytest_migrate.py`
  +4 是注释、`test_authz.py` +11、`test_workflows.py` +14 是折行）、10 条变小（I001 自动
  修复），`top_defs` 一条没动。
  另修掉工装自己生成的一处 lint 缺陷：形态 A′ 的收口 `assert _rc == 0, f"main() 非零…"`
  是**没有占位符的 f-string** ⇒ `F541`，把三个套件从"零 error"顶成"新文件带 error"。

- **一处迁移期的事故留档**：三层对拍的第 ③ 层是**就地改写再还原**，中途掐断进程会让
  变异体留在文件里（实测 `tests/test_iot_switch.py` 里留下 `FAILS.append('__MIGRATION_
  PARITY_MUTANT__')`，全量 pytest 当场多一条红）。工装为此有**启动自检**：扫到 `MUTANT`
  标记就拒绝开跑、退 2。**别在它跑到一半时掐它**。

- **转写过程里改掉的五处 codemod 缺陷**（每一处都**静默**改语义，靠三层对拍或 F821 才
  现形，逐条留档在 `scripts/dev/pytest_migrate.py` 的注释里）：
  ① **comprehension 的目标名被当成模块级依赖**：`DIRD = {r['id']: r for r in DIR}` 里的
  `r` 是 comprehension 的局部，却被算成"这条语句读模块级的 `r`" ⇒ 同名的那条
  `try: … r = _run_exec(…)` 被整块救回模块级，它内部重绑的 `_payload` 也就留在模块级，
  而驱动里读 `_payload` 的判据跑在它之后 ⇒ 读到字典不是字符串
  （`test_chat_quota`：`_payload[:90]` 当场 `KeyError`）。`_read_names` 现在减掉
  comprehension 的目标名（海象 `:=` 除外 —— 它会泄漏到外层）。
  ② **绑定写在 `try:` / `with:` 里的名字看不见**：只认顶层 `Name` 赋值目标 ⇒
  `try: CONN = psycopg.connect(…)` 整块被搬进驱动，而模块级 `def _count()` 还在读 `CONN`
  ⇒ 19 条 F821（`test_pg_retention_live` / `test_checkpoint_setup_concurrent`）。规则 ③ 的
  救援判据改成「**任何一个**绑定的名字被留模块级的代码读到 ⇒ 整条留下」。
  ③ **`pytest.skip` 在 import 期要 `allow_module_level=True`**：`_skip()` 既在 import 期被调
  （psycopg 没装 / 连不上）也在驱动里被调（没给 DSN），不给这个开关，import 那一次会退化成
  `ERROR … Interrupted: 1 error during collection` —— **红**，不是黄（那两件是"未评估"，
  必须报黄）。这一处是**手补**，两件套件的 `_skip()` 里各一段，理由写在代码里。
  ④ **`sys.argv` 收窄只在 pytest 轨做**：形态 A′ 的收口 `assert main() == 0` 里，入口若带
  `argparse`，pytest 的命令行会被当位置参数 ⇒ 退 2。无条件收窄又会把直跑轨的
  `--update` 弄坏（`test_module_budget.py --update` 返回 1），所以按 `"pytest" in sys.modules`
  分支。
  ⑤ **入口函数体不许搬进驱动**：它那套 `return 0/1` 老退出路径会让尾部的 `assert` 变成
  不可达代码 ⇒ 恒绿。

- **三件"值依赖运行时状态"的套件走人工点名（`--force --sink-def <名字>`）**：静态上分不出
  "这条模块级 `def` 读的是场景变量还是常量"，猜错的方向是**静默改语义**，所以不做自动规则、
  留成人工点名，成品照样过三层对拍。逐件：`test_todo_schedule`（`A.datetime = _FrozenDatetime`
  钉钟面被搬进驱动，而 `_TOMORROW` 在 import 期就要用钉住的钟面算 ⇒ 5 条红；
  ⚠️ 这一件 `--check --all` 报的是 `OK` —— 它的 hazard **认不出来**，`--all` 直接产出的
  是坏的那一版，配方里必须**先还原再 `--force --sink-def _date_ok`**）；
  `test_cross_language_tables`（`UNIVERSE` 被救回模块级，而填 `sites` 的循环在驱动里 ⇒
  整族判据**空跑**）；`test_reconcile_offline`（`ids` 被救回模块级，于是 `r`/`D` 那十条赋值
  全被救回、驱动里的判据跑在最后一次赋值之后 ⇒ 27 条红）。**判据全部来自三层对拍**，
  不是"看着对"。

- **`--force` / `--sink-def` 是本批新增的两个口子**，都写在 codemod 的用法里：`--force` 让
  "值依赖运行时状态"的 hazard 只警告不拦（`--all` 那次转写留下的 MANUAL 不再是死路），
  `--sink-def` 把人工点名的那条 `def` 也搬进驱动。两个口子的成品**都必须再过三层对拍** ——
  "人工过了一遍"不免检。
