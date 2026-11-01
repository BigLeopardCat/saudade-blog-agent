"""Function complexity and length ratchet over Python source files.

Oversize functions must match eval/complexity_baseline.json; removed or smaller
entries require tightening the ledger. Nested functions have separate metrics.
Run with --update to regenerate the reviewable ledger; this is not a McCabe score."""


import argparse
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


LEDGER = ROOT / "eval" / "complexity_baseline.json"


C_MAX = 30          # McCabe 上限：超过就进台账


LINES_MAX = 300     # 函数行数上限：同上


_SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules", "dist", ".mypy_cache"}


_BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
             ast.With, ast.AsyncWith, ast.Assert, ast.IfExp)


_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _complexity(fn: ast.AST) -> int:
    """Count this function’s decision points, excluding nested functions and lambdas."""
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
    """Return per-function metrics and file count; parse errors fail the scan."""
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

    # Reject noncanonical ledger formatting and ordering.
    unordered = {"b.py::z": {"c": 40, "lines": 100}, "a.py::y": {"c": 31, "lines": 50}}
    o_cases = [
        ("恰好是 dump() 的产物 ⇒ 不报", dump(unordered), False),
        ("键序没排好 ⇒ 报", json.dumps(unordered, indent=2) + "\n", True),
        ("少了结尾换行 ⇒ 报",
         json.dumps(dict(sorted(unordered.items())), ensure_ascii=False, indent=2), True),
    ]
    for desc, raw, want_bad in o_cases:
        got_bad = order_violation(raw) is not None
        ok = got_bad == want_bad
        print(f"  {'✅' if ok else '❌'} 键序判据：{desc}")
        if not ok:
            fails.append(f"键序判据自检：{desc}")
    return not fails and ctrl


def dump(ledger: dict) -> str:
    """`--update` 写盘用的**唯一**格式。读侧的 `order_violation()` 拿它做往返判据——
    两处**必须**是同一个函数，否则"写出来的"与"判出来的"会各走一套。
    """
    return json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def order_violation(raw: str) -> str | None:
    """Check that the ledger bytes match the canonical dump() serialization."""
    want = dump(json.loads(raw))
    if want == raw:
        return None
    return (f"{LEDGER.relative_to(ROOT)} 的盘上格式/键序与 `--update` 的产物不一致"
            f"（往返 {len(want)} 字节 vs 盘上 {len(raw)} 字节）——"
            f"跑一次 `--update` 校正。⚠️ 那一次 diff **只该动键序与空白，值一个都不许变**；"
            f"值变了就是另一次放宽，要单独说理")


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
        ledger = {k: v for k, v in current.items()
                  if v["c"] > args.c_max or v["lines"] > args.lines_max}
        LEDGER.write_text(dump(ledger), encoding="utf-8")
        print(f"\n✅ 已重写台账 {LEDGER.relative_to(ROOT)}：{len(ledger)} 条"
              f"（{len(current)} 个函数里超过 C≤{args.c_max} / {args.lines_max} 行的）")
        return 0

    if not LEDGER.exists():
        print(f"\n❌ 台账不存在：{LEDGER.relative_to(ROOT)} —— 先跑一次 `--update`", file=sys.stderr)
        return 1
    raw = LEDGER.read_text(encoding="utf-8")
    ledger = json.loads(raw)

    print(f"\n[扫描] {n_files} 个文件 / {len(current)} 个函数；"
          f"预算 C ≤ {args.c_max}、{args.lines_max} 行；台账 {len(ledger)} 条")
    bad = judge(current, ledger, args.c_max, args.lines_max)
    drift = order_violation(raw)
    if drift:
        bad.append(drift)
    if bad:
        for b in bad:
            print(f"  ❌ {b}")
        print(f"\n失败 {len(bad)} 项：超预算的函数要么拆开、要么 `--update` 记账；"
              f"台账只许缩小（细则见本文件头注）")
        return 1
    print("\n✅ 全在预算内（或已在台账里且没长大）")
    return 0


def test_complexity_budget():


    _argv = sys.argv
    if "pytest" in sys.modules:      # 只在 pytest 轨收窄（直跑轨的 CLI 照旧可用）
        sys.argv = _argv[:1]
    try:
        _rc = main()
    finally:
        sys.argv = _argv
    assert _rc == 0, "main() 非零：上面那些 ❌ 就是原因"


if __name__ == "__main__":
    test_complexity_budget()
