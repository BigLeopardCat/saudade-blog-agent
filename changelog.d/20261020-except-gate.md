- **新增第五道棘轮：盲捕获（`BLE001`）只许变小**（`eval/except_gate.py` +
  `eval/except_baseline.json`，首次读到 **117 条 / 53 个文件**）。**只记账、不批量改**——
  存量里那些宽 `except` **大多是对的**：工具层读不到数据要返回 `unavailable` 让模型换条路，
  而不是把整轮打成 500；`agent/graph.py` 那几处是"这一帧取不到就少一帧"的降级。机械按行号
  换成 `except (KeyError, …)` 会把这些**降级路径**改成**崩溃路径**，而且改的时候判据全绿、
  没人看得出区别。要收窄得一处一处看"这里该不该吞、吞了要不要留痕"，本闸只负责让进度可见、
  且不许倒退。
- **为什么不把它加进 `pyproject.toml` 的 `select`**：`eval/lint_gate.py` 把 `select` 当三个量
  之一 ⇒ 加一个规则族那道闸当场退 3；而重出的账里 622 条会变成 740 多条，**两件事混进一个
  数**。更要紧的是它把 117 条存量一次性变成红 ⇒ CI 恒红 ⇒ 人人学会无视它。所以这份
  `select`（`["BLE001"]`）的落点就是 `eval/except_gate.py` 自己，形制抄同族四道。
- **这一道最真的洞（已堵）**：`ruff check --select BLE001` **仍然服从** `pyproject.toml` 的
  `[tool.ruff.lint].ignore` 表——谁往里加一句 `"BLE001"`，本闸会静默读到 **0 条**、然后报
  "✅ 没有变差"，变成一条没有内容的判据。故基线里记着出账时那份 `ignore`，对不上退 3。
  实测反控：给 `[tool.ruff.lint]` 加 `ignore = ["BLE001"]` ⇒ 闸退 3（**不是**报绿），
  新套件 ② 同时变红；还原后两处都回绿。
- **新套件 `tests/test_except_gate.py`**（6 条，跑 0.08s）：接线与账 —— 闸/账在位、三个量
  （ruff 版本 / `select` / `ignore`）与各自的事实源同源、账自洽（逐文件之和 == `total`）
  且无幽灵键、`_ignored_ble` 这个守卫认得 `BLE001` 与整个 `BLE` 族又不误判无关的 ignore
  且 `main()` 真的调用了它、CI 有一步会执行它且那一行不手写工具版本、以及**跑一遍闸自带的
  `_self_check()`**。幽灵键与 `ignore` 洞两组都带反控（喂一个不存在的键必须被抓到、
  喂一个真文件不许被抓）。
- CI 加一步 `uv run --frozen python eval/except_gate.py`；`docs/lint-baseline.md` 补「同族的
  第五道」一节（含上面那条洞的来龙去脉）。
