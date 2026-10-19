# -*- coding: utf-8 -*-
"""双轨对拍判据（20261020 批 1）：pytest 轨与 run_all 轨跑的是**同一批套件、同一套
环境钉子**——两轨红绿必须一致，本文件钉住让对拍**有意义**的两个前提。

为什么需要它（`conftest.py` 头注 ③ 说的是"名单"那一半，这里补齐另一半）：迁移转写
最大的风险不是跑不起来，是**判据被改弱了还不知道**：

  · 台账忘了加文件 ⇒ 那个套件在 pytest 轨道**静默消失**（漏跑，绿灯照常）；
  · 台账加了但文件里没有 `def test_` ⇒ pytest 收集 0 个，绿灯是**空的**（假绿）；
  · `conftest._PINNED` 与 `run_all.py::_PINNED` 漂移 ⇒ 两轨跑的根本不是同一个
    出厂档，对拍从根上失效——单边改档不会有任何跑挂的信号；
  · `collect_ignore` 的通配与台账脱钩 ⇒ 名单外文件混进收集（判据被模块级 import
    当场执行，正是 conftest 头注 ① 要防的那种坏法）。

这四种都是"看着迁移了、其实判据变了"，套件自己抓不住自己的这一面，只有元判据抓得住。
（红绿**本身**的一致对拍不在单测里做——那是每次跑两条 runner 的流程动作：
`uv run pytest tests/` 与 `uv run python tests/run_all.py -j 4` 的名单逐一对得上。）

用法（双轨期与 run_all 并行；末批 PR 删 run_all 时，② 的对拍对象没了，本文件随之退役）：
    uv run pytest tests/test_pytest_parity.py -v
    uv run python tests/test_pytest_parity.py
"""

from __future__ import annotations

import ast
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import conftest  # noqa: E402  —— 真值源（pytest 下 import 到的就是已加载的同一模块对象）


def _pinned_of(path: pathlib.Path) -> dict[str, str]:
    """从源码里取 `_PINNED = {…}` 字面量。

    用 ast 取值、**不 import 不执行**——`run_all.py` 是 runner，import 它等于开跑
    全量套件；`conftest.py` 虽已被 pytest 执行过，但同一条路取两边，规则只有一份
    （要防的恰是"两边各自演化"——包括取值方式本身）。
    """
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_PINNED"):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path.name} 里找不到 `_PINNED` 字面量——对拍对象没了，直接红")


def test_parity_ledger():
    """① 台账：登记的都在、都真的可收集；没登记的一个不多收。"""
    on_disk = {p.stem for p in HERE.glob("test_*.py")}
    ledger = set(conftest._MIGRATED)
    ignored = {pathlib.Path(p).stem for p in conftest.collect_ignore}

    ghosts = sorted(ledger - on_disk)
    assert not ghosts, f"台账登记了磁盘上不存在的文件（改名没同步名单）：{ghosts}"

    empty = sorted(
        s for s in (ledger & on_disk)
        if "def test_" not in (HERE / f"{s}.py").read_text(encoding="utf-8"))
    assert not empty, f"台账里这些文件没有 `def test_`（pytest 收集 0 个 = 假绿）：{empty}"

    assert ignored == on_disk - ledger, (
        f"collect_ignore 与台账脱钩：应忽略 {sorted(on_disk - ledger)}，"
        f"实际忽略 {sorted(ignored)}——名单外文件会被模块级 import 当场执行")
    print(f"✅ ① 台账对拍：{len(ledger)} 个登记文件都在、都可收集，名单外不多收")


def test_parity_pinned_env():
    """② 环境钉子同源：conftest 与 run_all 的 `_PINNED` 逐字相等（改一处必须同步另一处）。"""
    pytest_side = _pinned_of(HERE / "conftest.py")
    runner_side = _pinned_of(HERE / "run_all.py")
    assert pytest_side == runner_side, (
        f"两轨钉子漂移：pytest 轨 {pytest_side} ≠ run_all 轨 {runner_side}"
        "——对拍从根上失效（同改 `tests/conftest.py` 与 `tests/run_all.py`）")
    print(f"✅ ② 钉子同源：两轨 _PINNED 一致（{len(runner_side)} 格）")


if __name__ == "__main__":
    test_parity_ledger()
    test_parity_pinned_env()
    print("✅ 双轨对拍前提全部成立")
