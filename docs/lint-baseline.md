# 静态检查基线（ruff）

这份文件回答一个问题：仓库自己的 `pyproject.toml` 声明了 ruff，但存量代码没清干净——
那么"新代码要干净"这条纪律的参照点在哪。参照点必须落盘，否则每次都是"看起来还行"。

## 事实（20260927 实测；**20261012 复测已漂**）

> ⚠️ 下表是 20260927 的读数，**别当现值用**。20261012 复测：**840 errors / 221 个文件**
> （241 fixable）——15 天里涨的是**文件数**（新增的测试与 `eval/` 入口），不是哪个文件变脏：
> 每文件的分布**形状没变**（`tests/test_workflows.py` 108→111、`tools/base.py` 45→52、
> `tests/test_reports.py` 39→38、`eval/admin_write_probe.py` 28→31、`agent/graph.py` 16→22）。
> 本文件是"新代码要干净"的**参照点**，不是"存量有多少"的**账本**——参照点没变，
> 所以下面的纪律一条都不用改；但引用绝对数之前先按上面那行命令**自己跑一遍**。

```bash
cd saudade-blog-agent
uvx ruff@0.14.4 check --no-cache .            # 与 CI 同版本
uvx ruff@0.14.4 check --no-cache --statistics .   # 按规则计数
```

版本的事实源是 `pyproject.toml` 的 `[dependency-groups] dev`（`ruff==0.14.4`，
经 `uv.lock` 定版）：CI 直接用 `uv run --frozen ruff …` 跑它，本机因为 `.venv` 是
**产线 venv、不许 `uv sync`**，才改用隔离的 `uvx ruff@0.14.4`（同版）。升版 = 改
`pyproject.toml` + `uv lock` + 本文件的 `uvx ruff@X`，`tests/test_ci_suite_list.py` ①b
会断言"文档里的版本 == dev 分组的版本"，漏掉一处就红。

- 578 errors（`select = ["E", "F", "I", "W"]`、`line-length = 100`，见 `pyproject.toml`）。
  **CI 同版本（0.14.4）与本机默认版本（0.16.9）实测同数**（578 / 139 fixable）——所以
  "版本不一致导致数字对不上"不是这里的解释，别往那个方向排查。
- 分布（`--statistics` 全量，**按数量降序，注意大头不是"缺陷"**）：

  | 规则 | 数量 | 性质 |
  |---|---|---|
  | E501 行长 >100 | 394 | 排版（中文注释按字符计数，实际显示更短） |
  | I001 导入未排序 | 101 | 排版（`--fix` 可自动修） |
  | E402 导入不在文件顶部 | 27 | **不全是刻意**：19 在 `tests/`、7 在 `eval/`（脚本形态）、**1 在 `agent/hostinfo.py`（库模块）** |
  | F401 未用导入 | 18 | 可能是真缺陷（也可能是"为副作用而导入"） |
  | F541 无占位 f-string | 12 | 无害 |
  | E741 歧义变量名 | 8 | 可读性（`l`/`O`/`I`） |
  | F841 未用变量 | 6 | 可能是真缺陷（死计算） |
  | F811 重复定义 | 3 | 可能是真缺陷 |
  | 其余（W605/E731/E401/E701/W293） | 9 | 无害或极小 |

  ⇒ **578 里约 86% 是行长与导入排序**。这条要说清楚：基线数字大不代表代码烂，
  只代表这个仓没把格式化当门禁。真正值得看的是 F841/F811 那 9 条。
- 最集中的文件：`tests/test_workflows.py` 108、`tools/base.py` 45、`agent/workflows.py` 41（20261018 批 2 收尾刀**已删**，此处是当时的读数）、
  `tests/test_reports.py` 39、`eval/admin_write_probe.py` 28、`agent/graph.py` 16。

CI 目前只跑一个更窄的闸：`eval.yml` 的 `uv run --frozen ruff check --select F821 .`（未定义名，秒级）。
F821 是"会炸"的那一类，E/F/I/W 是"会烂"的那一类。本文件管后者。

## 纪律（本批起所有新代码按此交付）

1. **新增/修改的文件必须 `ruff check` 零 error**（跑全规则，不只 F821）。
   命令：`uvx ruff check --no-cache <改动文件>`。
2. 存量文件不动它的存量条目：改动一个存量文件时，**不许把它的计数改大**；
   顺手清掉本次碰到的那几行是欢迎的——那属于缩小基线，不算"顺手重构"。
3. **别一次全清**：578 条里有 139 条可自动修，但自动修会动 40+ 个文件的导入与空行，
   在一次行为无关的提交里做这件事，等于把"行为变更"和"格式变更"混成同一个 diff——
   复审成本高、回滚粒度坏。存量清理单独成批（见下）。
   **E402 的大头不要修**：`eval/` 与 `tests/` 那 26 条在"先 `sys.path.insert` 再 import"
   的脚本里，是刻意的形态，改成包内相对导入是另一件事。**但库模块那几条不是**——
   20261012 已清掉 `agent/workflows.py` 里那 8 条（导入夹在 `NAV_MAP` 表之后；该文件 20261018 批 2 收尾刀**已删**），
   故这一条现在只作为历史读数留着；现存 1 条在 `agent/hostinfo.py`。
   这两处按"库代码 import 必须在顶部"办，不许再往回加。
4. **不因为"反正存量有一堆"就把新代码也写脏**：存量条目是历史遗留，新代码按当下标准要求。

## 后续（已排入规范化批次，不遗忘）

- 机械闸（**20261018 已落地**）：`eval/lint_gate.py` + `eval/lint_baseline.json`
  —— 跑 ruff、与按文件快照比对，**新文件带 error / 存量文件计数变大 / 总数变大**即非零
  退出（`--update` 是唯一放宽通道）。有了它，上面第 1/2 条才不靠自觉；在那之前这份
  文件只是快照，算不上闸。
  判据三条，第三条单独报（新文件与"存量又脏了一点"是两种事故，混在一句里读者会以为
  是自己动过的那个老文件）。**三个量任一换掉就退 3**（不是 1）：`ruff` 版本、
  `line-length`、`select` —— 它们都会改变条目集，换了就不是同一个量；三者的真值都
  只有 `pyproject.toml` 一处，接线由 `tests/test_packaging.py` ⑤ 盯着。
  自带 `_self_check()`（正控：计数解析得对、三条判据该红的红、没动的绿；反控：汇总行
  与逐行对不上必须抛）——**没有这一段，「闸其实是空的」没人看着**。
  本机跑法（产线 venv、不许 `uv sync`）：`python eval/lint_gate.py`
  （dev 组里就有同版 ruff）；隔离跑法 `--ruff "uvx ruff@0.14.4"`。
  ⚠️ 基线数字会漂（上面那张表就是 20260927 的旧读数）——**引用绝对数之前先自己跑一遍**，
  `eval/lint_baseline.json` 才是现值的事实源。
- **同族的另一半（20261009 已落地）**：`tests/test_complexity_budget.py` +
  `eval/complexity_baseline.json` —— 管的是"函数有多大"（McCabe 与行数），
  上面那份 `lint_gate.py` 管的是"文件有多脏"。规则同形：超上限的函数进台账、
  **只许缩小**、回到预算内必须销账、`--update` 是唯一的放宽通道。
  首次落地时 2738 个函数里 65 条超预算，居首是 `agent/graph.py::_planner_decide`
  （1470 行 / C=291）——那条已于 20261009 拆开（现 52 行、不在台账）。
  **现况：台账 65 条（20261012 复测），居首 `eval/run_golden.py::main`（C=248 / 900 行）**；
  数字会漂，别在别处抄——`eval/complexity_baseline.json` 是唯一事实源。
  **不走 ruff 的 C901** 的理由写在那份文件的头注里（阈值 10 ⇒ 264 个违规，
  阈值 ≥100 ⇒ 等于没闸）。它由 pytest 按目录收集（`pyproject.toml` 的 `testpaths`），
  CI 与夜间自动到手。
- **同族的第三道（20261013 已落地）**：`eval/type_gate.py` + `eval/mypy_baseline.json`
  —— 管的是**类型**。此前 `[tool.mypy]` 配了却从不执行（mypy 不在依赖、不在 `uv.lock`、
  CI 没有这一步），而函数签名已注释约 78% ⇒ 写了从不校验。首次真跑读到
  `220 errors in 21 files (checked 46)`，故与上面两道同规则：**记基线、只许变小**、
  `--update` 是唯一放宽通道；判据两条（总数变大 / **某文件**变大，防互相抵消）。
  本机跑它用隔离的 `uvx mypy@2.4.0`（同 dev 分组所钉版本，`uv sync` 本机永远不许跑）：
  `<隔离 3.12 环境>/bin/python eval/type_gate.py --mypy "uvx mypy@2.4.0"`。
  ⚠️ **换 mypy 版本 ⇒ 报出来的错误集整个变**，基线里记着出账时的版本，对不上直接退 3
  （升版要重出基线，见 `pyproject.toml` dev 分组那段注释）。
  ⚠️ **「用哪个解释器启动」也会换一个量**（20261013 上线当天被 CI 抓到的第三个洞）：
  mypy 去谁的 site-packages 里找三方库由 `--python-executable` 决定，不给就认自己所在的
  环境 ⇒ 隔离的 `uvx` 那份看不到 langgraph/openai/fastapi、import 全退化成 Any，
  同一条命令少报 8 个。`type_gate.py` 现在恒带该参数（值 = 启动器的解释器）**并且**
  要求启动器的版本 == `[tool.mypy].python_version`。接线由
  `tests/test_packaging.py` ④ 盯着（CI 有那一步、五处同源、账上的面 == 量到的面）。
- **同族的第四道（20261017 已落地）**：`tests/test_module_budget.py` +
  `eval/module_baseline.json` —— 管的是"**文件**有多大"（行数 + **模块级**定义数）。
  前三道都没看着这一格：复杂度那道只数函数，而一个 3000 行的文件可以是 5 个大函数、
  也可以是 200 个小函数，**治法是两回事**（前者拆函数，后者拆主题）。首次落地读到
  `agent/graph.py` **13994 行 / 259 个模块级定义**（占全仓 Python 的 39%），
  台账 **26 条**（阈值 lines>800 或 top_defs>60）。规则与前两道同形：超上限的进台账、
  **只许缩小**、回到预算内必须销账、`--update` 是唯一放宽通道。扫描面与
  `test_complexity_budget.py` 相同（全仓一级 `.py`，**含 `tests/`**，只排产物目录）。
  **本闸不是"禁止长文件"，是"长文件不许再长"**——把隐式增长变成一次需要辩护的动作。
  判据自带正控/反控（`_self_check()`：新增超预算 / 行数长大 / 定义变多 / 该销账 /
  台账过期 五路都要红，没动的要绿），理由同 `test_complexity_budget.py`：
  **没有这一段，「闸其实是空的」没人看着**。
- **同族的第五道（20261020 已落地）**：`eval/except_gate.py` + `eval/except_baseline.json`
  —— 管的是**盲捕获**（`BLE001`，`except Exception` / 裸 `except`）。首次读到
  **117 条 / 53 个文件**。**为什么单起一道而不是把它加进 `[tool.ruff.lint].select`**：
  那样 `lint_gate` 的三个量之一（`select`）当场换掉 ⇒ 那道闸退 3，而且 622 条的账会
  变成 740 多条，**两件事混进一个数**。所以这份 `select`（`["BLE001"]`）的落点就是
  `eval/except_gate.py` 自己。
  ⚠️ **这一道最真的洞**：`ruff check --select BLE001` **仍然服从** `pyproject.toml` 的
  `[tool.ruff.lint].ignore` 表——谁往里加一句 `"BLE001"`，本闸会静默读到 **0 条**、
  然后报"✅ 没有变差"（一条没有内容的判据）。故基线里记着出账时那份 `ignore`，对不上退 3；
  `tests/test_except_gate.py` ② 在 CI 之外再钉一遍，④ 单独盯着 `_ignored_ble` 这个守卫
  （认得出 `BLE001` 与整个 `BLE` 族、不误判无关的 ignore、且 `main()` 真的调用了它）。
  **只记账、不批量改**——这是本闸最重要的一条纪律：存量里那些宽 `except` **大多是对的**
  （工具层读不到数据要返回 `unavailable` 让模型换条路，而不是把整轮打成 500；图里那几处是
  "这一帧取不到就少一帧"的降级）。机械按行号换成 `except (KeyError, …)` 会把这些**降级路径**
  改成**崩溃路径**，而且改的时候判据全绿、没人看得出区别。要收窄得一处一处看"这里该不该吞、
  吞了要不要留痕"——那是人工活，本闸只负责让进度可见、且不许倒退。
  接线（CI 有那一步、三个量同源、账自洽、无幽灵键）由 `tests/test_except_gate.py` 盯着。
- 存量清理：按文件分批（先 `agent/` 与 `tools/` 这两个"会被 import 的"，后测试），
  每批单独提交、每批跑一次全套（`.venv/bin/python -m pytest -q`）。**`--fix` 之后必须看 diff**：
  F401/F811 的自动修会删掉"为了副作用而 import"的行与"刻意重复导入"的行。
- **F841/F811 那 9 条先单独看一眼**：这批条目与排版无关，指的是"某段计算/某个定义没人用"。
  大概率无害，但值得用一次阅读换掉"我猜它无害"。已看过的一条：
  `agent/workflows.py:1419-1420` 的 `tools`/`note` 是 `_expand_write_workflow` 里的死局部
  （每个分支各自早退/自建列表，两个变量从头到尾没人读）——删掉即可，无行为影响。
  **20261018 已按这条结论处理**：随批 2 刀 2d-2 搬进 `agent/workflow_expand_write.py`，
  两个死局部同时删掉（`eval/complexity_baseline.json` 那条的 `lines` 读数不变，仍是 473
  ——那个口径数的不是物理行，别拿"删了两行"去改台账）。
  其余 8 条在测试与 `eval/` 里，是"赋值了没断言"一类。
