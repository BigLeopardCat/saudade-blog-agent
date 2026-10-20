- **173 个套件各自手抄的 `check()` 收成一处实现**（`tests/_judge.py`，每个套件两行：
  `from _judge import collector` + `check = collector(FAILS)`）。转 pytest 之前那 173 份
  一共 **15 个版本**——打印用 `✅/❌` 还是 `✓/✗`、失败清单带不带明细，各不相同；改口径
  要改 173 个文件。**无对外行为变更**：调用点、累加器、尾部那句 `assert not FAILS`
  一个字没动（`scripts/dev/judge_collector_migrate.py` 是 AST 搬位置，不碰判据）。
- **⚠️ 为什么不是「每条判据一个 pytest item」（这条是量出来的，不是口味）**：判据在循环里
  按运行时数据生成 ⇒ **静态调用点与运行时条数对不上**（`test_authz` 202→269、`test_confirm`
  189→211、`test_reports` 198→224、`test_account_freeze` 123→146、`test_hardening` 65→97），
  名字里还有 **558 处 f-string** ⇒ 参数化 item 既不能按下标、也不能按名字在收集期生成。
  「一个套件 = 一个 item、判据收齐了一起报」是这套语料的正确形状。
- **唯一一处故意的不同**：失败清单里带明细（`名字  [明细]`），转之前只有 13 个套件带 ⇒
  方向是**信息更多**（失败清单是给人读的）。
- **放行口是 `scripts/dev/judge_collector_parity.py`**（改前 vs 改后）：把**两版收集器换成
  同一只间谍**（只记录、不打印、不记账），各跑一遍驱动函数，比 `(名字, 条件, 明细)` 三元组的
  序列逐条相同 —— 收集器改坏了只有这一种形状（某条没喂进来 / 名字变了 / 条件变了 / 明细变了）。
  **不比 stdout**：`✅/❌` 与 `✓/✗` 归一是这次故意的不同，比 stdout 只会把故意的不同报成缺陷。
- **易变族先问「能不能从根上消掉」，能消的不要打码**（打码有代价：把语义一起吃掉的形状写不
  出判据）。从根上消掉三族：**集合/字典迭代序**（子进程 `PYTHONHASHSEED=0`）、
  **`tempfile` 随机名**（子进程里把名字序列换成定种子 + 每遍从空 TMPDIR 起跑）、
  **一切随机 id**（`os.urandom` 与 `secrets._sysrand` 一起换 —— 只换前者时 `uuid4` 定了、
  `secrets.token_hex` 还在变，3.12 的 `secrets` 走 C 层熵源）。**为什么这一族非消不可**：
  `test_confirm.py` 有一条判据把令牌载荷截断着打出来，`jti` 只剩两个十六进制字符，没有任何
  形状能认（认了就是把所有两位数都吃掉）。
- **消不掉的那几族才打码**（墙钟秒 / unix 秒 / JWT 紧凑串 / 十六进制 id / 两种钟点写法 /
  内核给的端口 / 带键名的 pid），**逐族都是实测逼出来的**（出处写在正则旁）。打码器自己有判据：
  **计数 / 结局 / 纯数字 / 日期 / 别的 host 都不许被吃掉**（放宽一格 ⇒ 对拍变橡皮图章）。
- **判据 `tests/test_judge_collector.py`（8 条，每条带反控）**：① 除 `_judge.py` 外没有第二处
  收集器实现（反控：抄回去的必须被认出；`from agent.authz import check` 那种 **import 不许
  误伤**）；② 每个套件都接在 `_judge.collector` 上、累加器是**本模块自己**的（共用一个列表
  ⇒ 全量进一个进程会串台）；③ 没有「留着累加器却没接上」的漏改；④ 收集器行为契约（名字在前、
  三条参数、**记进去的那条与打印出来那行逐字相同**、必假判据真的进列表）；④b 两个收集器各记
  各的账；⑤ 八族归一 + ⑤b 窄；⑤c 那两族「从根上消掉」的还在（两个独立子进程必须
  逐字相同 —— 去掉 `PYTHONHASHSEED=0` 或那段 `tempfile` 补丁，这条就红）。
- **codemod 两个坑记一笔**：① `from _judge import collector` 插到**模块 docstring 上面**会把
  docstring 变成普通表达式（第一版就栽在这里，6 个试点文件全中）；② 落点由 ruff 的 isort
  收口而不是自己算——它在 stdlib/第三方/第一方之间的位置取决于**那个文件开头本来有哪几节**
  （`test_cancel.py` 开头就有一节 `langchain_core`），手抄那套规则只会抄出第二个 isort。
- **棘轮跟着收紧**：`eval/module_baseline.json` 里 11 条 `tests/*.py` 的台账（每个套件少了
  3–6 行 / 1 个顶层定义）按实测改小 —— 全是收紧，没有一条变大。
- 工装：`scripts/dev/judge_collector_migrate.py`（AST 搬位置，认不出的形状一律不动）、
  `scripts/dev/judge_collector_parity.py`（放行口）；`docs/eval-observability.md` §2 补
  「L0 的判据口径归一」一段（含上面那张量出来的表）。
