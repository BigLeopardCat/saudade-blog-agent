# -*- coding: utf-8 -*-
"""`eval/type_gate.py` 的**运行版本守卫**（20261020 补的那个洞）。

**为什么单起一套、而不是加进 `tests/test_packaging.py` ④**：那一套已经
C=124 / 318 行（复杂度台账贴着顶），往里加一节要 `--update` 放宽台账——而棘轮的纪律是
**台账只许缩小**。新起的这一套只判一件事，自带的本地清单（不是模块级 `FAILS`）不会
跨用例泄漏。

**为什么这件事非在测试里走一遍不可**：CI 与夜间**恒跑对版本**（`uv run --frozen` 拿
dev 分组钉的那份）⇒ `running != pin` 那条分支在 CI 里**永远不被执行**。没有这一套，
它会静默腐烂成死代码，而它守的正是"**换了一个量**被读成**变差了**"：

    python eval/type_gate.py --mypy "uvx mypy@<别的版本>"
    → [扫描] mypy 2.4.0 / py3.12：193 个错误 / 30 个文件（基线 190 / 30 个）
      ❌ agent/graph.py  81（基线 79）—— 这一处变多了
      ❌ 总数 193 > 基线 190                      ⇒ 退 1

读起来是"`agent/graph.py` 变脏了"，真相是换了 mypy、错误集整个变了。而且扫描行印的是
`pin`（`mypy 2.4.0`）——**日志上看不出区别**。同族的 `lint_gate` / `except_gate` 都读
运行版本（`ruff --version`），只有这里漏了。

用法：
    uv run pytest tests/test_type_gate.py -v
    uv run python tests/test_type_gate.py
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
_GATE = ROOT / "eval" / "type_gate.py"


def _gate_module():
    """加载闸本身（只跑模块体：常量 + 定义，无副作用）。"""
    spec = importlib.util.spec_from_file_location("_type_gate_under_test", _GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _report(bad: list[str]) -> None:
    assert not bad, f"{len(bad)} 条未通过：" + "；".join(bad)


def test_running_mypy_parser_is_not_vacuous():
    """① `_running_mypy` 真读得出版本号；读不出时给 None（不是猜一个）。"""
    bad: list[str] = []
    print("① 运行版本的读法")
    gate = _gate_module()
    real = gate._running_mypy([sys.executable, "-c", "print('mypy 9.9.9 (compiled: yes)')"])
    print(f"  {'✅' if real == '9.9.9' else '❌'} 认得出 `mypy X.Y.Z (compiled: yes)`  [{real}]")
    if real != "9.9.9":
        bad.append("版本行的解析不对")
    junk = gate._running_mypy([sys.executable, "-c", "print('nope')"])
    print(f"  {'✅' if junk is None else '❌'} 认不出时返回 None（**不猜**一个版本号）  [{junk}]")
    if junk is not None:
        bad.append("认不出却编了一个版本号出来")
    _report(bad)


def test_wrong_running_version_exits_three():
    """② 反控：跑起来的版本 != pin ⇒ **退 3**，而且**在跑 mypy 之前**就退。

    这一条是整套的重点：它证明那条分支真的在，且它拦得住。`_gather` 换成哨兵——
    若守卫排在 `_gather()` 之后，这里会当场炸（而不是"绿得看不出来"）。
    """
    bad: list[str] = []
    print("② 反控：版本对不上 ⇒ 退 3（且不付那次 mypy）")
    gate = _gate_module()
    argv_bak = sys.argv
    real_run, real_gather = gate._running_mypy, gate._gather
    try:
        sys.argv = ["type_gate.py"]

        def _boom(_prog):
            raise AssertionError("守卫排在 `_gather()` 之后 —— 白跑一分钟 mypy 才退 3")

        gate._running_mypy = lambda _p: "0.0.0"
        gate._gather = _boom
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            rc = gate.main()
        out = sink.getvalue().strip()
        print(f"  {'✅' if rc == 3 else '❌'} 退的是 **3**（不是 1）  [{rc}]")
        if rc != 3:
            bad.append(f"版本对不上却退了 {rc}（1 = 被读成'代码变差了'）")
        named = "0.0.0" in out and gate._pinned_mypy() in out
        print(f"  {'✅' if named else '❌'} 消息里两个版本都点名（否则读者不知道该跑哪个）")
        if not named:
            bad.append("消息没把两个版本都写出来")
        print("  ✅ 守卫排在 `_gather()` 之前（`_gather` 被换成哨兵也没炸）")
    finally:
        sys.argv = argv_bak
        gate._running_mypy, gate._gather = real_run, real_gather
    _report(bad)


def test_matching_version_does_not_take_that_branch():
    """③ 正控：版本对上时**不**走那条分支（否则它是「见谁都红」）。

    `_gather` 打桩成空读数 ⇒ 不打桩这里要真跑一分钟 mypy。空读数对着真账 = 全清，
    闸的其余判据都该放行（`total 0 < 190`、没有 grew）⇒ 退 0。
    """
    bad: list[str] = []
    print("③ 正控：版本对上时放行")
    gate = _gate_module()
    argv_bak = sys.argv
    real_run, real_gather = gate._running_mypy, gate._gather
    try:
        sys.argv = ["type_gate.py"]
        gate._running_mypy = lambda _p: gate._pinned_mypy()
        gate._gather = lambda _p: ({}, 0, 0)
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            rc = gate.main()
        print(f"  {'✅' if rc == 0 else '❌'} 退 0  [{rc}]")
        if rc != 0:
            bad.append(f"版本对上却退了 {rc}：那条守卫是「见谁都红」")
    finally:
        sys.argv = argv_bak
        gate._running_mypy, gate._gather = real_run, real_gather
    _report(bad)


def test_main_actually_reads_the_running_version():
    """④ `main()` 真的**调用**了它（源码里得有调用点，不能只有定义）。"""
    bad: list[str] = []
    print("④ 调用点")
    src = _GATE.read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_running_mypy"]
    print(f"  {'✅' if calls else '❌'} 有 `_running_mypy(...)` 的调用点  [{len(calls)} 处]")
    if not calls:
        bad.append("只有定义、没有调用点（守卫是死代码）")
    _report(bad)


if __name__ == "__main__":
    for _fn in (test_running_mypy_parser_is_not_vacuous,
                test_wrong_running_version_exits_three,
                test_matching_version_does_not_take_that_branch,
                test_main_actually_reads_the_running_version):
        _fn()
    print("\n全部通过")
