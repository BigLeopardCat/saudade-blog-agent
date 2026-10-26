# -*- coding: utf-8 -*-
"""Shared legacy assertion collectors must record failures and keep ledgers separate."""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import inspect
import io
import pathlib
import warnings

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

_JUDGE = HERE / "_judge.py"

_COLLECTOR_NAMES = ("check", "check_")
_ACCUMULATORS = ("FAILS", "FAILED")


def _load(path: pathlib.Path, name: str):
    """按路径加载一个模块（只跑模块体：常量 + 定义，无副作用）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sources() -> dict[str, str]:
    """`tests/` 下全部 `.py` 的源码 —— 判据读**盘上的现在值**，不读字面。"""
    return {p.relative_to(HERE).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(HERE.rglob("*.py"))}


def _parse(src: str) -> ast.Module:
    """`ast.parse` 但**闭嘴**：`test_docs_links.py` 里有个 `\\`` 转义（既有的 W605，
    已进 `eval/lint_baseline.json`）⇒ 每次解析都吐一条 SyntaxWarning。那是那本账上的事，
    不是这套判据要看的东西（这套只看 AST，不执行）。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(src)


def _report(bad: list[str]) -> None:
    assert not bad, f"{len(bad)} 条未通过：" + "；".join(bad)


# ── ① 全仓只有一处收集器实现 ────────────────────────────────────────────────


def local_collector_defs(sources: dict[str, str]) -> list[str]:
    """模块级自己定义 `check` / `check_` 的文件（实现只许住在 `_judge.py`）。

    **只认定义、不认 import**：`test_zako_role.py` 里 `check` 是 `agent.authz.check`
    （授权判据，另一件事）—— 那是 `ImportFrom`，不是 `FunctionDef`，不许误伤。
    嵌套定义也要认（`ast.walk`）：套件里再包一层 `def check` 同样是又抄了一份口径。
    """
    out: list[str] = []
    for name, src in sorted(sources.items()):
        if name == "_judge.py":
            continue
        for n in ast.walk(_parse(src)):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if n.name in _COLLECTOR_NAMES:
                out.append(f"{name}:{n.lineno} 自己定义了 `def {n.name}`")
    return out


def test_only_one_collector_implementation():
    """① 除了 `_judge.py`，没有第二个地方实现收集器。"""
    print("① 收集器只有一处实现")
    fake = {"test_x.py": "FAILS = []\n\n\ndef check(name, cond, detail=''):\n    pass\n"}
    assert local_collector_defs(fake), "反控失败：抄回去的收集器没被认出来"
    assert not local_collector_defs(
        {"test_y.py": "from agent.authz import check\n"}), "反控失败：import 被当成定义了"
    bad = local_collector_defs(_sources())
    print("  ✅ 除了 `_judge.py`，没有别处定义收集器（含反控两条）")
    _report(bad)


# ── ②③ 接线：接在同一处、累加器是本模块自己的、一个都不许漏 ────────────────


def collector_wiring(src: str) -> tuple[str | None, str | None]:
    """(收集器名, 累加器名)：模块级 `check = collector(FAILS)` 那两处。

    `(名字, None)` = 「有收集器名字，但不是 `collector(本模块的列表)`」；
    `(None, None)` = 这个文件没有收集器（新式套件直接 `assert`，不欠这一处）。
    """
    tree = _parse(src)
    assigned = {t.id for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
                if isinstance(t, ast.Name)}
    for n in tree.body:
        if not isinstance(n, ast.Assign) or len(n.targets) != 1:
            continue
        tgt = n.targets[0]
        if not (isinstance(tgt, ast.Name) and tgt.id in _COLLECTOR_NAMES):
            continue
        v = n.value
        if not (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                and v.func.id == "collector" and len(v.args) == 1
                and isinstance(v.args[0], ast.Name)):
            return tgt.id, None
        # 累加器必须是**本模块自己**的模块级名字：共用一个列表 ⇒ 同进程里会串台
        return tgt.id, (v.args[0].id if v.args[0].id in assigned else None)
    return None, None


def bad_wiring(sources: dict[str, str]) -> list[str]:
    out: list[str] = []
    for name, src in sorted(sources.items()):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        coll, acc = collector_wiring(src)
        if coll is None:
            continue
        if acc is None:
            out.append(f"{name}：`{coll}` 不是 `collector(本模块的列表)`")
        elif "from _judge import collector" not in src:
            out.append(f"{name}：接了 `collector`，却没有 `from _judge import collector`")
    return out


def stranded_accumulators(sources: dict[str, str]) -> list[str]:
    """留着模块级累加器、却没有收集器接上的套件（漏改的那种，尾部断言从此恒绿）。"""
    out: list[str] = []
    for name, src in sorted(sources.items()):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        tree = _parse(src)
        has_acc = any(isinstance(t, ast.Name) and t.id in _ACCUMULATORS
                      for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                      for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))
        if has_acc and collector_wiring(src)[0] is None:
            out.append(name)
    return out


def test_every_suite_wires_the_shared_collector():
    """② 每个有收集器的套件都接在 `_judge.collector` 上、累加器是它自己的。"""
    print("② 套件都接在同一处")
    assert bad_wiring({"test_x.py": "check = collector(别处的)\n"}), "反控失败：别人的累加器没认出"
    assert bad_wiring({"test_x.py": "check = lambda *a: None\n"}), "反控失败：抄回来的没被认出"
    assert not bad_wiring({"test_x.py": "from _judge import collector\n\nFAILS: list[str] = []\n"
                                        "check = collector(FAILS)\n"}), "反控失败：接对了也红"
    bad = bad_wiring(_sources())
    print("  ✅ 每个套件的收集器都接在 `_judge.collector` 上（含反控三条）")
    _report(bad)


def test_no_suite_is_left_unwired():
    """③ 没有「留着累加器却没接上收集器」的套件。"""
    print("③ 没有漏改的套件")
    assert stranded_accumulators(
        {"test_x.py": "FAILS = []\n"}), "反控失败：留着累加器却没接收集器，没被认出"
    bad = stranded_accumulators(_sources())
    print("  ✅ 没有漏改的套件")
    _report([f"{n}：留着累加器却没接收集器（漏改）" for n in bad])


# ── ④ 收集器本身的行为契约（真跑一遍） ──────────────────────────────────────


def test_collector_contract():
    """④ 名字在前、三条参数、打印与记账同一句、假判据真的进列表。"""
    print("④ 收集器的行为契约")
    mod = _load(_JUDGE, "_judge_under_test")
    sig = inspect.signature(mod.collector([]))
    names = list(sig.parameters)
    assert names == ["name", "cond", "detail"], f"参数名/顺序变了：{names}"
    assert sig.parameters["detail"].default == "", "明细的默认值不是空串"

    fails: list[str] = []
    check = mod.collector(fails)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        check("甲条", True)
        check("乙条", False, "为什么")
        check("丙条", False)          # ← 必假判据（下面那条反控）
    lines = buf.getvalue().splitlines()
    assert len(lines) == 3, f"打印行数不对：{lines}"
    assert "甲条" in lines[0] and "✅" in lines[0], lines[0]
    assert "乙条" in lines[1] and "❌" in lines[1] and "[为什么]" in lines[1], lines[1]
    assert "丙条" in lines[2] and "❌" in lines[2], lines[2]
    assert fails == ["乙条  [为什么]", "丙条"], f"记账不对：{fails}"
    assert lines[1].endswith(fails[0]) and lines[2].endswith(fails[1]), "记账与打印不是同一句"
    print("  ✅ 打印 / 记账 / 明细后缀 / 必假判据进列表（丙条就是那条反控）")


def test_collectors_do_not_share_the_ledger():
    """④b 两个套件各自的 `collector(自己的列表)` 互不影响（同进程的落点）。"""
    print("④b 各记各的账")
    mod = _load(_JUDGE, "_judge_under_test")
    a: list[str] = []
    b: list[str] = []
    with contextlib.redirect_stdout(io.StringIO()):
        mod.collector(a)("甲的假判据", False)
        mod.collector(b)("乙的假判据", False)
    assert a == ["甲的假判据"] and b == ["乙的假判据"], f"串台了：{a} / {b}"
    print("  ✅ 两个收集器各记各的")
