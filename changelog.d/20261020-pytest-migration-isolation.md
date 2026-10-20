- **测试迁移批 N+1：跨套件全局还原层**（`tests/conftest.py` 的 autouse `_process_isolation`
  + 判据 `tests/test_process_isolation.py`）。这 179 个套件此前是**一个套件一个进程**跑的，
  谁改了 `settings.jwt_secret`、谁把 `server._agent` 换成桩、谁把 `rag.search._index` 换成
  6 篇夹具索引，都不会影响别人 ⇒ **没人写过还原**。全量进一个 pytest 进程之后这些全变成
  跨套件隐式耦合：实测 A 套件跑完没还原、B 套件红，而 B 的判据本身是好的。

- **这一层只保护受害者，不假装那些套件干净**：每条用例前后比对 `config.settings.settings`
  的逐属性取值、`sys.modules`（含整份模块对象）、`os.environ`（跳过 pytest 自己写的
  `PYTEST_*`）、`sys.path`，以及**被套件直接打桩的那些模块全局**；改了没还原的**逐条打印**
  在用例输出里（谁漏了、漏的是什么，看得见）。另外清掉 `lru_cache` 型可调用对象 ——
  `agent.graph._tools_desc_cached` 缓存的正是"当时的注册表长什么样"，某套件把工具的
  `args_schema` 打桩后调过一次，缓存里就留着一份降级后的菜单（`search_notes(keyword:str*)`
  变成 `search_notes()`），后面的套件"现算 vs 缓存"对不上。

- **名单是「静态普查 ∪ 实测」两半，判据钉着静态那一半**：对 `tests/*.py` 做 AST 普查，
  收集所有 `模块.属性 = …` 的落点（`Import`/`ImportFrom` 的别名还原成 `sys.modules` 的键）。
  判据 `test_process_isolation.py` ④ 断言**扫到的 ⊆ 名单** —— 新套件打了新模块的桩而忘了
  加名单，那条当场红。实测那半（`patch(...)` / `setattr` 这些静态看不见的落点，8 个模块）
  不许按"扫描没扫到"删掉。同一份判据 ⑤ 抓到一个真鬼：名单里的 `agent.workflows` 早就随
  "收尾刀"删掉了（`agent/workflow_params.py` 头注写着），**那一项从删掉那天起就永远不还原
  任何东西，而它不会让任何套件变红**。

- **这一层自己也有反控**（「正控不红不算数」）：正控/受害者对子（上一轮真改了三处进程级
  全局并当场自证改到了，下一轮必须看见原值）、对 `_snapshot`/`_restore` 的直接反控（把
  `_restore` 改成只返回空清单 ⇒ 那条当场红）、fixture 真的挂着（`autouse` 掉了的话
  受害的那条会**跳过**而不是红，而跳过不算绿）。两处突变都实测过会红。

- **一处真缺陷**：`tests/test_cliff_min_keep.py` 把 `rag.search._index`（进程级单例）指向
  6 篇夹具索引后不还原 ⇒ `tests/test_gate_replan.py` 的"站内公开数据"那族判据看不见真语料，
  ⑥d/⑥e 两段全红。它只在**全量收集**时现形（单跑那两件套件都绿），靠"逐文件二分"定位到
  具体是哪个套件污染了谁。修法是把 `rag.search` 加进还原名单，**不是**改那个套件的判据。
