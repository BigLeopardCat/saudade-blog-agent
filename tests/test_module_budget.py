#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块体量**预算闸**（20261017）——治"改动不断叠进同一个文件"。

**要回答的问题**：本仓已经用「预算 + 台账」治过**函数级**复杂度
（`tests/test_complexity_budget.py` + `eval/complexity_baseline.json`），但**文件级**
没有任何机制看着。落地前实测：`agent/graph.py` **13994 行 / 259 个模块级定义**，
占全仓 Python 的 39%（31 个模块共 35927 行）——而它是**一天天长出来的**，不是一天
写成的。没有闸的时候，这件事唯一的可见性来自有人偶尔 `wc -l` 一下。

**判法**（`judge()`，一句话）：超上限的模块必须在台账里、且只许缩小；台账里的条目
一旦回到预算内就要销账；台账里找不到对应模块的条目是过期的。

台账 = `eval/module_baseline.json`，`--update` 是唯一的放宽通道——**reviewer 直接看
那份 diff**。"这一版又长胖了"必须在 diff 里留下痕迹，这正是本闸要买的东西。

**两个读数**：`lines`（文件行数）与 `top_defs`（**模块级**定义数：函数/类，不下钻
方法）。为什么两个都要——一个 3000 行的文件可以是 5 个大函数、也可以是 200 个小函数，
**治法是两回事**（前者拆函数，后者拆主题）。`top_defs` 只数模块级：嵌套函数与类方法
算在它们所属的那个顶层定义里，不另计。

**扫描面** = 全仓一级 `.py`（与 `test_complexity_budget.py` 同口径），只排掉产物目录。
**含 `tests/`**：同一条理由——「任何地方新写的大模块都要红」，不给测试开后门
（`tests/test_workflows.py` 5784 行本身就是一例）。

**本闸不是"禁止长文件"，是"长文件不许再长"**。真需要它变大，就在同一个提交里
`--update` 并说明为什么 ⇒ 把隐式增长变成一次需要辩护的动作。

用法（cd 仓根，用仓的 venv）：
  .venv/bin/python tests/test_module_budget.py                  # 检查（run_all.py 这样调）
  .venv/bin/python tests/test_module_budget.py --update         # 重写台账
  .venv/bin/python tests/test_module_budget.py --lines-max 400 --defs-max 30  # 临时换阈值
退出码：0 全在预算内 / 1 有模块超预算、长大了，或台账过期。
"""
import argparse
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEDGER = ROOT / "eval" / "module_baseline.json"

# 预算。**阈值只要稳定就有意义**（同 `test_complexity_budget.py` 的口径）：本闸判的是
# ratchet（"它没变大"），不必是某个规范值。换阈值 = 全仓台账重写 ⇒ 必须同一次 `--update`。
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
    """扫描全仓，返回 `({模块: {"lines": n, "top_defs": m}}, 文件数)`。

    **解析失败不静默跳过**：一个读不进来的文件会让它整族从闸下溜走（"守卫静默跳过"
    正是本仓最贵的失效模式之一）⇒ 当场非零退出。
    """
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
        if m["lines"] > base["lines"]:
            bad.append(f"行数长大    {key}  {m['lines']} 行（台账 {base['lines']}，只许缩小）")
        if m["top_defs"] > base["top_defs"]:
            bad.append(f"定义变多    {key}  {m['top_defs']} 个（台账 {base['top_defs']}，只许缩小）")
    for key in sorted(ledger):
        if key not in current:
            bad.append(f"台账过期    {key}  源码里没有这个文件了 —— 删掉这一条")
    return bad


def rewrite(current: dict, lines_max: int, defs_max: int) -> dict:
    """`--update` 的落点：台账 = **恰好**当前超预算的那批模块。"""
    return {k: v for k, v in sorted(current.items())
            if v["lines"] > lines_max or v["top_defs"] > defs_max}


# ── 自检：读数 + 判据（没有这一段，「闸其实是空的」没人看着）──────────────────
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


def main() -> int:
    ap = argparse.ArgumentParser(description="模块体量预算闸")
    ap.add_argument("--update", action="store_true", help="按当前扫描重写台账")
    ap.add_argument("--lines-max", type=int, default=LINES_MAX)
    ap.add_argument("--defs-max", type=int, default=DEFS_MAX)
    args = ap.parse_args()

    _self_check()

    current, n_files = scan()
    if args.update:
        LEDGER.write_text(json.dumps(rewrite(current, args.lines_max, args.defs_max),
                                     ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        print(f"台账已重写：{LEDGER.relative_to(ROOT)}（{len(rewrite(current, args.lines_max, args.defs_max))} 条）")
        return 0

    ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {}
    bad = judge(current, ledger, args.lines_max, args.defs_max)
    print(f"扫描 {n_files} 个文件；台账 {len(ledger)} 条；"
          f"预算 lines≤{args.lines_max} / top_defs≤{args.defs_max}")
    if bad:
        print("\n".join("✗ " + b for b in bad))
        print(f"\n{len(bad)} 条违规。**别急着 --update**：先看能不能按主题拆。")
        return 1
    print("✓ 全部模块都在预算内（或台账内只减不增）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
