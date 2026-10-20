- 产线那份 checkout 在本地 `main` 上攒的 7 笔（确认卡问句形状 + 短应答块撤除 +
  槽稳定性读数）并回干线。它们**从未 push**、任何远端分支上都没有，而本地 `main`
  与 `origin/main` 已分叉 24 笔 ⇒ 产线上 `git pull --ff-only` 一直是拒绝的。
- 冲突四处，全是"两侧动过同一格"：两份台账按**合并后的实测**取值；
  `tests/test_confirm.py` / `tests/test_golden_keys.py` 取干线版再把产线那几行接回去
  （产线改的是句尾字面与用例数 193，落在干线已重写过的区域里）。
- 产线新加的两个套件是迁移前的裸脚本形态（无顶层 `def test_*` ⇒ pytest 收 0 个、
  **静默不跑**）⇒ 转原生 pytest 并接上 `tests/_judge.py` 的共享收集器。
- `eval/slot_stability.py` 原先把子进程要读的用例 JSON 写死进
  `/tmp/slot_stability_cases`（bandit B108；新文件必须零 finding）⇒ 改成
  `tempfile.mkdtemp` 的进程私有目录，退出时收掉。
