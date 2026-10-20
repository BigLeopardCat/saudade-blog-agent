# -*- coding: utf-8 -*-
"""把 173 个套件各自手抄的 `def check` / `def check_` 换成 `_judge.collector(累加器)`。

**只搬位置、不改语义**：删掉本地那份定义，在累加器那一行后面补一句
`check = collector(FAILS)`，并在 import 块里补 `from _judge import collector`
（插在**开头那一段 import 的末尾**，理由见 `_import_plan`）。
调用点、累加器、尾部那句 `assert not FAILS, …` **一个字不动**。

认不出的形状一律**不动**（进 MANUAL 清单）：
  - 本地没有 `def check` / `def check_`；
  - 那份定义**不往本模块的累加器里记**（例如 `check` 其实是 `agent.authz.check`，
    或收集器只 print 不记）；
  - 没有模块级累加器（`FAILS: list[str] = []` 那种带注解的写法要认出来）；
  - `check` 有除 `(名字, 条件, 明细="")` 之外的签名形状（关键字实参、`*args`…）。

用法：
    .venv/bin/python scripts/dev/judge_collector_migrate.py --check tests/*.py   # 只看会改什么
    .venv/bin/python scripts/dev/judge_collector_migrate.py tests/*.py          # 就地改
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

_ACC = ("FAILS", "FAILED")           # 累加器的名字（全仓就这两个）
_COLLECTORS = ("check", "check_")    # 收集器的名字（`test_zako_role.py` 用 `check_`）
_IMPORT_LINE = "from _judge import collector\n"


def _accumulator(tree: ast.Module) -> str | None:
    """模块级累加器（`FAILS = []` 与 `FAILS: list[str] = []` 两种写法）。"""
    for n in tree.body:
        targets = n.targets if isinstance(n, ast.Assign) else (
            [n.target] if isinstance(n, ast.AnnAssign) else [])
        for t in targets:
            if isinstance(t, ast.Name) and t.id in _ACC:
                if isinstance(n.value, (ast.List, ast.Call)) or n.value is None:
                    return t.id
    return None


def _collector_def(tree: ast.Module, acc: str) -> ast.FunctionDef | None:
    """那份**往 `acc` 里记**的 `def check`（认不出 ⇒ None）。"""
    for n in tree.body:
        if not (isinstance(n, ast.FunctionDef) and n.name in _COLLECTORS):
            continue
        args = [a.arg for a in n.args.args]
        if n.args.kwonlyargs or n.args.vararg or n.args.kwarg or len(args) != 3:
            continue
        if args[2] not in ("detail", "extra"):
            continue
        records = any(isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
                      and x.func.attr == "append" and isinstance(x.func.value, ast.Name)
                      and x.func.value.id == acc
                      for x in ast.walk(n))
        if records:
            return n
    return None


def _import_plan(tree: ast.Module) -> tuple[int, str]:
    """(插在第几行之后, 不动的理由)。

    **插在文件开头那一段 import 的末尾**，不是插在最前面、也不是插在 `FAILS` 上面：

    - **不能插在最前面**：`tests/*.py` 头一句是模块 docstring，插到它上面会把 docstring
      变成普通表达式语句（第一版 codemod 就栽在这里，6 个试点文件全中）。
    - **插在开头那一段的末尾**而不是末尾那一段：这些套件开头是 stdlib（`import sys` /
      `from pathlib import Path`），随后 `sys.path.insert(...)` 把 `agent/` 那族接上，
      于是后面那一段 import 全带 E402 豁免。`_judge` 是**测试助手**（不是 stdlib、不是
      `agent` 那族），按 ruff 的默认分节它落在 stdlib 之后 ⇒ 放在开头那一段的末尾，
      分节是对的、也不用沾 E402。插在末尾那一段还可能和同在那里的 `_ctx_src` 撞排序。
    - **开头那段 import 前面已经有别的语句**（除了 docstring）⇒ 这一处插进去会撞 E402，
      而"顺手加一行豁免注释"是**换了一套账**（豁免计数是另一本账，见 `eval/lint_gate.py`
      同族的纪律）⇒ 这种文件**不动**，交给人工。实测 173 个里一个都没有。
    """
    body = tree.body
    doc = body[0] if (body and isinstance(body[0], ast.Expr)
                      and isinstance(body[0].value, ast.Constant)
                      and isinstance(body[0].value.value, str)) else None
    idxs = [i for i, n in enumerate(body) if isinstance(n, (ast.Import, ast.ImportFrom))]
    if not idxs:
        return ((doc.end_lineno or doc.lineno) if doc else 0), ""
    first = last = idxs[0]
    while last + 1 in idxs:          # 连着的一段（空行不算断，语句才算）
        last += 1
    at = body[last].end_lineno or body[last].lineno
    # docstring 不算"代码"（E402 不为它报），其余任何语句都算
    if any(n is not doc and not isinstance(n, (ast.Import, ast.ImportFrom)) for n in body[:first]):
        return at, "开头那段 import 前面已经有别的语句（插进去要 E402 豁免，不动）"
    return at, ""


def plan(path: pathlib.Path) -> tuple[str, str] | None:
    """(为什么不动, 改后的源码)。能改 ⇒ 前者是空串。"""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return f"语法错（{e.msg}）", ""
    if _IMPORT_LINE in src:          # 先认这句：改过之后本地那份 `def check` 已经没了
        return "已经改过了", ""
    acc = _accumulator(tree)
    if acc is None:
        return "没有模块级累加器", ""
    fn = _collector_def(tree, acc)
    if fn is None:
        return f"没有往 {acc} 里记的 `def check`", ""

    lines = src.splitlines(keepends=True)
    # 1) 删掉那份定义（连同它上面紧邻的空行不留双份）
    start = fn.lineno - 1
    while start > 0 and lines[start - 1].strip() == "":
        start -= 1
    out = lines[:start] + lines[fn.end_lineno:]
    # 2) 累加器那一行后面补一句
    acc_line = None
    for i, ln in enumerate(out):
        if re.match(rf"^{acc}\s*(:\s*[^=]+)?=", ln):
            acc_line = i
            out.insert(i + 1, f"{fn.name} = collector({acc})\n")
            break
    else:
        return f"找不到 {acc} 的定义行", ""
    # 3) 补 import（插在开头那段 import 的末尾，见 `_import_plan`）
    at, why = _import_plan(tree)
    if why:
        return why, ""
    if at >= start or at >= acc_line:   # `at < start` ⇒ 删定义不影响 `at` 这个下标
        return f"import 段落在累加器/收集器之后（at={at}），插进去会先用到后 import", ""
    # 前面那个 import 是 stdlib、`_judge` 是第三方 ⇒ ruff 的 isort 要**两节之间空一行**
    # （实测：不空行 ⇒ 6 个试点文件全报 I001）。真正落到哪一节由 `_isort` 收口。
    out.insert(at, "\n" + _IMPORT_LINE)
    return "", "".join(out)


def _isort(paths: list[pathlib.Path]) -> None:
    """把那句 import 摆到 isort 认可的位置 —— **交给 ruff，不自己算**。

    它在 stdlib / 第三方 / 第一方之间的落点取决于**那个文件开头本来有哪几节**：
    `test_cancel.py` 开头就有一节 `langchain_core`（第三方）⇒ `_judge` 得并进那一节、
    还按字母排在它前面；别的文件开头只有 stdlib ⇒ `_judge` 自成第三节、节间空一行。
    手抄这套规则只会抄出第二个 isort。
    只 `--select I001`：基线里 **I001 一条都没有**（`grep -c I001 eval/lint_baseline.json`
    = 0）⇒ 这条只可能动刚插进去的那一行。
    """
    cmd = [sys.executable, "-m", "ruff", "check", "--fix", "--no-cache", "--select", "I001",
           *[str(p) for p in paths]]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    left = [ln for ln in (r.stdout or "").splitlines() if ": I001" in ln]
    if r.returncode not in (0, 1) or left:
        print(f"  ⚠️ ruff --select I001 没清干净（rc={r.returncode}）：\n    "
              + "\n    ".join((left or (r.stderr or "").splitlines())[:5]))


def main() -> int:
    ap = argparse.ArgumentParser(description="判据收集器归一（只搬位置，不改语义）")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--check", action="store_true", help="只报告，不写盘")
    args = ap.parse_args()
    todo, manual = [], []
    for p in sorted({pathlib.Path(x) for x in args.paths}):
        why, new = plan(p)
        if why:
            manual.append((p, why))
        else:
            todo.append((p, new))
    for p, why in manual:
        print(f"  跳过 {p.relative_to(ROOT) if ROOT in p.parents else p}：{why}")
    print(f"\n可自动改 {len(todo)} 个 / 不动 {len(manual)} 个")
    if args.check:
        for p, _ in todo:
            print(f"  会改 {p.name}")
        return 0
    for p, new in todo:
        p.write_text(new, encoding="utf-8")
    if todo:
        _isort([p for p, _ in todo])
    print(f"已改 {len(todo)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
