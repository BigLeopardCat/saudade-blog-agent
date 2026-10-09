#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复杂度 / 函数长度**预算闸**（20261009）。

要回答的问题：本仓有一批函数圈复杂度偏高、还有几个超过 300 行（**落地前实测**：
1343 个函数里 264 个 C>10、157 个 C>15、52 个 C>30，四个超过 300 行，居首
`agent/graph.py::_planner_decide` 1470 行 / C=291——该函数随后已拆开、现不在台账）
——**而没有任何机制看着这件事**。`eval.yml` 只跑 `ruff --select F821`（未定义名），`docs/lint-baseline.md`
自己写着「机械闸 `eval/lint_gate.py` … **没做之前，这份文件只是快照，还算不上闸**」。

**为什么不走 ruff 的 C901**（实测过，免得重议）：本机 ruff 要 `uvx ruff@0.14.4`；
`[tool.ruff.lint]` 的 `select = ["E","F","I","W"]` 不含 C901；而开 C901 取常规阈值 10
会报 **264 个**违规，取任何能挡住 `_planner_decide` 的阈值（≥100）又等于没闸。
改成「预算 + 台账」不受阈值语义影响：**判据是「这个函数没变大」**，`C_MAX`/`LINES_MAX`
只要**稳定**即可，不必是规范 McCabe 值——这让读数便宜可辩护。

**判法**（`judge()`，一句话）：超上限的函数必须在台账里、且只许缩小；台账里的条目
一旦回到预算内就要销账；台账里找不到对应函数的条目是过期的。

台账 = `eval/complexity_baseline.json`，`--update` 是唯一的放宽通道（reviewer 直接
看那份 diff）。本套件由 `tests/run_all.py` 按磁盘枚举 ⇒ **CI 与夜间 04:00 自动到手**，
零 workflow 改动。

用法（cd 仓根，用仓的 venv）：
  .venv/bin/python tests/test_complexity_budget.py              # 检查（run_all.py 这样调）
  .venv/bin/python tests/test_complexity_budget.py --update     # 重写台账
  .venv/bin/python tests/test_complexity_budget.py --c-max 10   # 临时换阈值（看它会不会红）
退出码：0 全在预算内 / 1 有函数超预算、长大了，或台账过期。
"""
import argparse
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEDGER = ROOT / "eval" / "complexity_baseline.json"

C_MAX = 30          # McCabe 上限：超过就进台账
LINES_MAX = 300     # 函数行数上限：同上

# 扫描面 = 全仓一级 `.py`（与 ruff 同口径），只排掉产物目录。
# **含 `tests/`**：规矩是「任何地方新写的大函数都要红」，不给测试开后门。
_SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules", "dist", ".mypy_cache"}

# ── McCabe 计数器（自写，无依赖）─────────────────────────────────────────────
# 判定点集合：`if`/`elif`（每个 If）、`for`/`async for`、`while`、**每个**
# `except` 分支、`with`/`async with`、`assert`、三元（IfExp）、布尔算子
# （`and`/`or` 的每个额外操作数）、推导式的每个 `for` 与 `if`。
# ⚠️ 这**不是**逐条复刻某个现成实现（mccabe 与 radon 在布尔算子上就不一致），
# 也**不需要**是：本闸判的是 ratchet（「它没变大」），常数只要稳定就有意义。
# 改这个集合 = 全仓读数平移 ⇒ 必须同一次 `--update`，并在这里记一笔。
_BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
             ast.With, ast.AsyncWith, ast.Assert, ast.IfExp)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _complexity(fn: ast.AST) -> int:
    """函数自己的判定点数 + 1。

    不下钻到嵌套函数 / `lambda` 体——它们的判定点算它们自己的（`_collect` 会把它们
    当独立条目逐个报出来），否则一个"外壳 + 大内部函数"会把复杂度记两遍。
    """
    total = 1
    stack = [fn]
    while stack:
        for child in ast.iter_child_nodes(stack.pop()):
            if isinstance(child, _NESTED):
                continue
            if isinstance(child, _BRANCHES):
                total += 1
            elif isinstance(child, ast.BoolOp):
                total += len(child.values) - 1
            elif isinstance(child, _COMPREHENSIONS):
                total += sum(1 + len(gen.ifs) for gen in child.generators)
            stack.append(child)
    return total


def _collect(node: ast.AST, prefix: str, out: dict) -> None:
    """把 `node` 底下所有函数（含嵌套、含类方法）记进 `out`：限定名 → (行数, 复杂度)。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qn = prefix + child.name
            out[qn] = ((child.end_lineno or child.lineno) - child.lineno + 1,
                       _complexity(child))
            _collect(child, qn + ".", out)
        elif isinstance(child, ast.ClassDef):
            _collect(child, prefix + child.name + ".", out)
        else:
            _collect(child, prefix, out)


def scan() -> tuple[dict, int]:
    """扫描全仓，返回 `({条目: {"lines": n, "c": m}}, 文件数)`。

    条目键 = `相对路径::限定名`。**解析失败不静默跳过**：一个读不进来的文件会让它
    整族函数从闸下溜走（"守卫静默跳过"正是本仓最贵的失效模式之一）⇒ 当场非零退出。
    """
    out: dict = {}
    n_files = 0
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if set(rel.parts) & _SKIP_DIRS:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=rel.as_posix())
        except (SyntaxError, UnicodeDecodeError) as e:  # pragma: no cover
            raise SystemExit(f"❌ 解析不了 {rel.as_posix()}：{type(e).__name__}: {e}")
        n_files += 1
        fns: dict = {}
        _collect(tree, "", fns)
        for qn, (lines, c) in fns.items():
            out[f"{rel.as_posix()}::{qn}"] = {"lines": lines, "c": c}
    return out, n_files


def judge(current: dict, ledger: dict, c_max: int, lines_max: int) -> list[str]:
    """返回违规清单（空 = 全在预算内）。规则见模块头注。"""
    bad: list[str] = []
    for key, m in sorted(current.items()):
        base = ledger.get(key)
        if base is None:
            if m["c"] > c_max or m["lines"] > lines_max:
                bad.append(f"新增超预算  {key}  C={m['c']}（上限 {c_max}）/ "
                           f"{m['lines']} 行（上限 {lines_max}）"
                           f" —— 拆开它，或 `--update` 记进台账")
            continue
        if m["c"] <= c_max and m["lines"] <= lines_max:
            bad.append(f"台账该销账  {key}  C={m['c']} / {m['lines']} 行都已回到预算内"
                       f" —— 删掉这一条（`--update` 会删）")
        if m["c"] > base["c"]:
            bad.append(f"复杂度长大  {key}  C={m['c']}（台账 {base['c']}，只许缩小）")
        if m["lines"] > base["lines"]:
            bad.append(f"行数长大    {key}  {m['lines']} 行（台账 {base['lines']}，只许缩小）")
    for key in sorted(ledger):
        if key not in current:
            bad.append(f"台账过期    {key}  源码里没有这个函数了 —— 删掉这一条")
    return bad


# ── 自检：计数器 + 判据（没有这一段，「闸其实是空的」没人看着）──────────────────
_SELF_SRC = '''
def f0():
    pass


def f1(x):
    return 1 if x else 2


def f2(xs):
    return [y for y in xs if y]


def f3(a, b, c):
    return a and b and c


def f4(x):
    if x:
        return 1
    return 2


def f5(xs):
    for x in xs:
        try:
            pass
        except ValueError:
            pass


def f6(x):
    with open(x) as fh:
        assert fh
        return fh


def f7(xs):
    def inner(a):
        return a and a
    return inner(xs)


def f8(xs):
    return [y for y in xs for z in y if z]


class C9:
    def m(self, x):
        while x:
            x = x - 1
'''
# 手算值（逐条对着上面那段源码数出来的；改计数器就必须重数这一张表）。
# 注意 `f8` 是 4 = 基数 1 + 两个 for 各 1 + 那个 if 1（`f2` 只有一个 for ⇒ 3）——
# 这张表建好当天就抓出过一次手算错（把基数漏了），所以它是必须留的。
_SELF_EXPECT = {"f0": 1, "f1": 2, "f2": 3, "f3": 3, "f4": 2, "f5": 3,
                "f6": 3, "f7": 1, "f7.inner": 2, "f8": 4, "C9.m": 2}


def _self_test() -> bool:
    print("[自检] 计数器（手算表）：")
    got: dict = {}
    _collect(ast.parse(_SELF_SRC), "", got)
    fails = []
    for name, want in sorted(_SELF_EXPECT.items()):
        have = got.get(name, (0, 0))[1]
        ok = have == want
        print(f"  {'✅' if ok else '❌'} {name}  C={have}（期望 {want}）")
        if not ok:
            fails.append(f"{name}: C={have} != {want}")
    # 反控：把计数器打桩成恒真/恒 1，上面这张表必须散架（本仓规矩：判据要能抓"它没生效"）
    ctrl = got.get("f3", (0, 0))[1] != 1
    print(f"  {'✅' if ctrl else '❌'} 反控：`f3` 不是 1（计数器真的在数，不是恒返回 1）")

    print("\n[自检] 判据（构造现场，看它红不红）：")
    led = {"a.py::big": {"lines": 400, "c": 60}}
    cases = [
        ("同值 ⇒ 不报",
         judge({"a.py::big": {"lines": 400, "c": 60}, "a.py::ok": {"lines": 10, "c": 2}},
               led, 30, 300), 0, None),
        ("复杂度长大 ⇒ 报", judge({"a.py::big": {"lines": 400, "c": 61}}, led, 30, 300), 1, "复杂度长大"),
        ("行数长大 ⇒ 报", judge({"a.py::big": {"lines": 401, "c": 60}}, led, 30, 300), 1, "行数长大"),
        ("新增超预算 ⇒ 报",
         judge({"a.py::big": {"lines": 400, "c": 60}, "a.py::ok": {"lines": 10, "c": 2},
                "b.py::new": {"lines": 5, "c": 31}}, led, 30, 300), 1, "新增超预算"),
        ("已还清 ⇒ 必须销账（否则那条台账把闸放开了）",
         judge({"a.py::big": {"lines": 100, "c": 20}}, led, 30, 300), 1, "台账该销账"),
        ("台账过期 ⇒ 报", judge({"a.py::ok": {"lines": 10, "c": 2}}, led, 30, 300), 1, "台账过期"),
    ]
    for desc, bad, want_n, want_kw in cases:
        ok = len(bad) == want_n and (want_kw is None or any(want_kw in b for b in bad))
        print(f"  {'✅' if ok else '❌'} {desc}  [{'; '.join(bad) or '无'}]")
        if not ok:
            fails.append(f"判据自检：{desc}")
    return not fails and ctrl


def main() -> int:
    ap = argparse.ArgumentParser(description="复杂度 / 函数长度预算闸")
    ap.add_argument("--update", action="store_true", help="重写台账（唯一的放宽通道）")
    ap.add_argument("--c-max", type=int, default=C_MAX, help=f"McCabe 上限（默认 {C_MAX}）")
    ap.add_argument("--lines-max", type=int, default=LINES_MAX,
                    help=f"函数行数上限（默认 {LINES_MAX}）")
    args = ap.parse_args()

    if not _self_test():
        print("\n❌ 自检没过——先修计数器/判据，再看扫描结果（否则读数不可信）", file=sys.stderr)
        return 1

    current, n_files = scan()
    if args.update:
        ledger = {k: v for k, v in sorted(current.items())
                  if v["c"] > args.c_max or v["lines"] > args.lines_max}
        LEDGER.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        print(f"\n✅ 已重写台账 {LEDGER.relative_to(ROOT)}：{len(ledger)} 条"
              f"（{len(current)} 个函数里超过 C≤{args.c_max} / {args.lines_max} 行的）")
        return 0

    if not LEDGER.exists():
        print(f"\n❌ 台账不存在：{LEDGER.relative_to(ROOT)} —— 先跑一次 `--update`", file=sys.stderr)
        return 1
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))

    print(f"\n[扫描] {n_files} 个文件 / {len(current)} 个函数；"
          f"预算 C ≤ {args.c_max}、{args.lines_max} 行；台账 {len(ledger)} 条")
    bad = judge(current, ledger, args.c_max, args.lines_max)
    if bad:
        for b in bad:
            print(f"  ❌ {b}")
        print(f"\n失败 {len(bad)} 项：超预算的函数要么拆开、要么 `--update` 记账；"
              f"台账只许缩小（细则见本文件头注）")
        return 1
    print("\n✅ 全在预算内（或已在台账里且没长大）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
