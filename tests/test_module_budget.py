"""Module size ratchet over Python source files, including tests.

Oversize modules must match eval/module_baseline.json exactly; shrinking or
removing an entry requires tightening the ledger. Parse errors fail the scan.
Run with --update to regenerate a reviewable, canonically serialized ledger."""


import argparse
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


LEDGER = ROOT / "eval" / "module_baseline.json"


LINES_MAX = 800     # 文件行数上限：超过就进台账


DEFS_MAX = 60       # 模块级定义数上限：同上


_SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules", "dist", ".mypy_cache"}


def measure(text: str) -> dict:
    """一份源码的两个读数。`lines` 与 `top_defs` 的口径见模块头注。"""
    tree = ast.parse(text)
    top = sum(1 for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    return {"lines": len(text.splitlines()), "top_defs": top}


def scan() -> tuple[dict, int]:
    """Scan source modules; return metrics and file count, failing on parse errors."""
    out: dict = {}
    n_files = 0
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if set(rel.parts) & _SKIP_DIRS:
            continue
        try:
            out[rel.as_posix()] = measure(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as e:  # pragma: no cover
            raise SystemExit(f"❌ 解析不了 {rel.as_posix()}：{type(e).__name__}: {e}")
        n_files += 1
    return out, n_files


def judge(current: dict, ledger: dict, lines_max: int, defs_max: int) -> list[str]:
    """返回违规清单（空 = 全在预算内）。规则见模块头注。"""
    bad: list[str] = []
    for key, m in sorted(current.items()):
        base = ledger.get(key)
        if base is None:
            if m["lines"] > lines_max or m["top_defs"] > defs_max:
                bad.append(f"新增超预算  {key}  {m['lines']} 行（上限 {lines_max}）/ "
                           f"{m['top_defs']} 个模块级定义（上限 {defs_max}）"
                           f" —— 按主题拆开它，或 `--update` 记进台账")
            continue
        if m["lines"] <= lines_max and m["top_defs"] <= defs_max:
            bad.append(f"台账该销账  {key}  {m['lines']} 行 / {m['top_defs']} 定义"
                       f" 都已回到预算内 —— 删掉这一条（`--update` 会删）")
            continue    # 已在预算内，"收紧/长大"这两个方向都不再适用（只会重复报同一件事）
        if m["lines"] > base["lines"]:
            bad.append(f"行数长大    {key}  {m['lines']} 行（台账 {base['lines']}，只许缩小）")
        if m["top_defs"] > base["top_defs"]:
            bad.append(f"定义变多    {key}  {m['top_defs']} 个（台账 {base['top_defs']}，只许缩小）")
        # Tighten the ledger after shrinkage so its recorded ceiling stays exact.
        if m["lines"] < base["lines"] or m["top_defs"] < base["top_defs"]:
            bad.append(f"台账该收紧  {key}  实测 {m['lines']} 行 / {m['top_defs']} 定义，"
                       f"台账还记着 {base['lines']} / {base['top_defs']} —— 台账比实测松，"
                       f"把这一条改小（棘轮只许缩小）")
    for key in sorted(ledger):
        if key not in current:
            bad.append(f"台账过期    {key}  源码里没有这个文件了 —— 删掉这一条")
    return bad


def rewrite(current: dict, lines_max: int, defs_max: int) -> dict:
    """Return current oversize entries; dump() owns canonical ordering."""
    return {k: v for k, v in current.items()
            if v["lines"] > lines_max or v["top_defs"] > defs_max}


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


_SELF_FLAT = '''\
def a():
    pass


class B:
    def m(self):
        pass


async def c():
    pass
'''


_SELF_NESTED = '''\
def outer():
    def inner():
        pass

    class C:
        pass
'''


def _self_check() -> None:
    """正控 + 反控各来一遍：计数器数得对、`judge` 该红的红、该绿的不红。"""
    m = measure(_SELF_FLAT)
    assert m["top_defs"] == 3, f"模块级定义应数 3 个（a/B/c），实得 {m['top_defs']}"
    assert m["lines"] == 11, f"行数口径漂了：{m['lines']}"
    assert measure(_SELF_NESTED)["top_defs"] == 1, "嵌套函数/类方法不该另计"

    over = {"lines": 900, "top_defs": 70}
    small = {"lines": 100, "top_defs": 5}

    # ① 台账空 + 超预算 ⇒ 红；台账空 + 不超 ⇒ 绿
    assert judge({"x.py": over}, {}, 800, 60), "新增超预算没红 —— 闸是空的"
    assert not judge({"x.py": small}, {}, 800, 60), "没超预算却红了"

    # ② 台账在 + 长大 ⇒ 红；原样 ⇒ 绿；两个读数各自独立触发
    assert judge({"x.py": {"lines": 901, "top_defs": 70}},
                 {"x.py": over}, 800, 60), "行数长大没红"
    assert judge({"x.py": {"lines": 900, "top_defs": 71}},
                 {"x.py": over}, 800, 60), "定义变多没红"
    assert not judge({"x.py": over}, {"x.py": over}, 800, 60), "没动却红了"

    # ③ 回到预算内 ⇒ 该销账（红）；④ 源码里没有这个文件 ⇒ 过期（红）
    assert judge({"x.py": small}, {"x.py": over}, 800, 60), "回到预算内没提示销账"
    assert judge({}, {"x.py": over}, 800, 60), "台账过期没红"

    # Each metric independently requires an exact ledger match.
    _shrunk = judge({"x.py": {"lines": 850, "top_defs": 70}}, {"x.py": over}, 800, 60)
    assert _shrunk and any("收紧" in b for b in _shrunk), \
        f"台账比实测松却没红 —— 缩小之后会留下永久免费额度：{_shrunk}"
    assert judge({"x.py": {"lines": 900, "top_defs": 60}}, {"x.py": over}, 800, 60), \
        "定义变少却没提示收紧"
    assert not judge({"x.py": {"lines": 900, "top_defs": 70}},
                     {"x.py": {"lines": 900, "top_defs": 70}}, 800, 60), \
        "台账与实测相等却红了 —— 那是 `--update` 之后的正常态"

    # Reject noncanonical ledger formatting and ordering.
    unordered = {"b.py": over, "a.py": small}
    assert order_violation(dump(unordered)) is None, "自写自读却判红 —— 判据是空的"
    assert order_violation(json.dumps(unordered, indent=2) + "\n") is not None, \
        "键序没排好却没红（`dump()` 排、盘上不排，正是 A5 那一格）"
    assert order_violation(json.dumps(dict(sorted(unordered.items())),
                                      ensure_ascii=False, indent=2)) is not None, \
        "少了结尾换行却没红"


def main() -> int:
    ap = argparse.ArgumentParser(description="模块体量预算闸")
    ap.add_argument("--update", action="store_true", help="按当前扫描重写台账")
    ap.add_argument("--lines-max", type=int, default=LINES_MAX)
    ap.add_argument("--defs-max", type=int, default=DEFS_MAX)
    args = ap.parse_args()

    _self_check()

    current, n_files = scan()
    if args.update:
        LEDGER.write_text(dump(rewrite(current, args.lines_max, args.defs_max)),
                          encoding="utf-8")
        print(f"台账已重写：{LEDGER.relative_to(ROOT)}（{len(rewrite(current, args.lines_max, args.defs_max))} 条）")
        return 0

    raw = LEDGER.read_text(encoding="utf-8") if LEDGER.exists() else ""
    ledger = json.loads(raw) if raw else {}
    bad = judge(current, ledger, args.lines_max, args.defs_max)
    print(f"扫描 {n_files} 个文件；台账 {len(ledger)} 条；"
          f"预算 lines≤{args.lines_max} / top_defs≤{args.defs_max}")
    if raw:
        drift = order_violation(raw)
        if drift:
            bad.append(drift)
    if bad:
        print("\n".join("✗ " + b for b in bad))
        print(f"\n{len(bad)} 条违规。**别急着 --update**：先看能不能按主题拆。")
        return 1
    print("✓ 全部模块都在预算内（或台账内只减不增）")
    return 0


def test_module_budget():


    _argv = sys.argv
    if "pytest" in sys.modules:      # 只在 pytest 轨收窄（直跑轨的 CLI 照旧可用）
        sys.argv = _argv[:1]
    try:
        _rc = main()
    finally:
        sys.argv = _argv
    assert _rc == 0, "main() 非零：上面那些 ❌ 就是原因"


if __name__ == "__main__":
    test_module_budget()
