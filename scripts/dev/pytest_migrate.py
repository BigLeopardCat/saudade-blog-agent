#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裸脚本套件 → 原生 pytest 的 codemod（20261020 批 1 工装）。

**它只搬位置，不改判据。** 放行口不是这个脚本，是 `scripts/dev/migration_parity.py` 的三层对拍
（AST 判据条数 / 两轨红绿等价 / 注入必假判据两轨都红）。这个脚本认不出的形状**一律不动**，
列进 `--check` 的 MANUAL 清单交人工 —— "改错了但看着像改对了"是这件事最贵的失效。

## 搬什么、留什么（规则来自 PR #38 已迁移的 10 个试点，逐条比对出来的）

留（模块级）：
  · 模块 docstring；
  · 全部 `Import` / `ImportFrom`；
  · 全部 `FunctionDef` / `AsyncFunctionDef` / `ClassDef` —— 包括助手；
  · **被留下的语句读到的模块级常量**（如 `TOOL_NAMES`、`READY_KEYS`）：搬进函数会让它变
    局部名，助手读全局当场 `NameError`。**迭代到不动点**，只看一层会漏
    （`Y = BIG + 1` 留了、`BIG` 没留）；
  · **最后一个 import 之前的 setup**（`ROOT = ...` / `sys.path.insert(0, ROOT)`）——
    **但判据调用与 `print` 横幅除外**：本仓有一族套件的 import 是散着写的
    （`test_account_freeze.py` 到 593 行还在 import），`last_import` 落在很后面，
    "前导段整段留"会把大量 `check(...)` 留在模块级、而它引用的量被搬走 ⇒ `NameError`。

搬（进驱动函数）：
  · 其余一切可执行语句，**保持原有相对顺序**。

改名：**按 AST 位置改，不做文本替换**（理由见 `_rename_ids`）
  · 顶层 `def test_*` ⇒ `_<原名>`：它们是助手不是用例，被 pytest 单独收集会**恒绿**
    （它们调 `check()` 往模块级 FAILS 里塞，断言在模块尾，不在自己身上）；
  · 形态 D 的 `__main__` 里 `for fn in (test_a, test_b): fn()` 是**裸名引用**，
    文本替换的 `name()` 形式扫不到 —— 这是必须走 AST 的那一半理由。

收尾：**驱动函数体 = 搬来的模块级语句 + `__main__` 块的正文**（顺序即今天的执行顺序）。
`__main__` 正文只做一件事 —— 处理收尾：

  · `sys.exit(<模块级零参函数>())` ⇒ **拆成裸调用**（`sys.exit(main())` → `main()`）；
    入口函数本体**原样留着**，不搬进来。搬进来会让它那套 `return 1` / `return 0` 老退出
    路径把尾部的 assert 变成**不可达代码** ⇒ pytest 恒绿（工装抓出来的第二个 bug）。
  · 其余收尾（`sys.exit(1)` / `raise SystemExit(...)`）⇒ 顶层整条丢；**嵌套的不动**
    （实测 pytest 在任意嵌套深度都把 `SystemExit` 判为失败，行为等价）。

最后尾补 `assert not FAILED`（`FAILED` 名字从判据收集器的 append 目标现取，不写死），
末尾补 `if __name__ == "__main__": <stem>()` ⇒ **直跑语义原样保留**。

**用例函数名 = 文件词干本身**（`test_write_facts.py` → `def test_write_facts()`），
不是 `test_` + 词干 —— PR #38 的 10 个试点逐字如此。

顶层已经有 `def <stem>()` 的文件直接判 MANUAL —— 那是 PR #38 已迁移的形态，不重做。

用法：
    python3 scripts/dev/pytest_migrate.py --check --all
    python3 scripts/dev/pytest_migrate.py tests/test_x.py            # 就地改写
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
import textwrap

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TESTS = ROOT / "tests"


class Manual(Exception):
    """认不出的形状 —— 不动它，交人工。理由写在消息里。"""


def _fail_name(tree: ast.Module) -> str | None:
    """判据收集器往哪个模块级名字里 append（`FAILS` / `FAILED` 两种都有，现取）。

    收集器**不都叫 `check`**：`test_zako_role.py` 那个叫 `check_`。所以按**前缀**认
    （`check` / `check_` / `checker`…），认名字不认字面 —— 名字是习惯、append 目标才是契约。

    没有收集器不算错：还有一族是**形态 A′** —— `def main() -> int` 自己攒 `bad` 列表、
    返回退出码，收口在 `sys.exit(main())` 上（`test_complexity_budget` / `test_module_budget` /
    `test_parent_repo_paths` / `test_judge_offline` 四个都是）。那种套件的收口写成
    `assert main() == 0` 就是它的直译，返回 None 让 `transform` 走那条路。
    """
    found: set[str] = set()
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name.startswith("check"):
            for sub in ast.walk(n):
                if (isinstance(sub, ast.Attribute) and sub.attr == "append"
                        and isinstance(sub.value, ast.Name)):
                    found.add(sub.value.id)
    if not found:
        return None
    if len(found) > 1:
        raise Manual(f"check() 同时往 {sorted(found)} 里 append，判不出收口用哪个")
    return found.pop()


def _zero_arg_fn(tree: ast.Module, name: str) -> bool:
    d = next((x for x in tree.body
              if isinstance(x, ast.FunctionDef) and x.name == name), None)
    if d is None:
        return False
    a = d.args
    return not (a.args or a.posonlyargs or a.kwonlyargs or a.vararg or a.kwarg)


def _exit_inner(node: ast.AST) -> ast.expr | None:
    """收尾语句里"真正要跑的那件事"：`sys.exit(main())` → `main()` 这个 Call。"""
    call = None
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call = node.value
    elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
        call = node.exc
    if call is None or len(call.args) != 1 or call.keywords:
        return None
    return call.args[0]


def _main_body(src: str, tree: ast.Module, node: ast.If, drop_entry: bool = False) -> str:
    """`if __name__ == "__main__":` 块的**正文**（去缩进前的原文）。

    **驱动函数体就是它**，所以这里要做的只有一件事：处理收尾。

      · `sys.exit(<模块级零参函数>())` ⇒ **拆成裸调用**（`sys.exit(main())` → `main()`）；
        入口函数的 `return 0/1` 因此原样留着，不用搬它的函数体（搬了尾部的 assert 会
        变成不可达代码 ⇒ 恒绿）。
      · 其余收尾（`sys.exit(1)` / `raise SystemExit(...)`）⇒ **整条丢**。
        只丢**顶层**的：嵌套在 `if FAILS:` 里的 `sys.exit(1)` 原样留着 —— 实测 pytest
        在任意嵌套深度都把 `SystemExit` 判为失败（`/tmp` 探针），行为等价，不动更安全。
    """
    if not node.body:
        return ""
    if node.body[0].lineno == node.lineno:
        raise Manual('`__main__` 块的语句跟 `if` 同行，认不出')
    out: list[str] = []
    for st in node.body:
        if _is_exit(st):
            inner = _exit_inner(st)
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                    and _zero_arg_fn(tree, inner.func.id)):
                if not drop_entry:            # 形态 A′ 由收口那句 `assert F() == 0` 调它
                    out.append(f"{inner.func.id}()")
            continue
        out.append(_seg(src, st))
    # 这些语句在原文里带 4 空格缩进（`__main__` 块的体），去平了再交给统一的 +4 ——
    # 否则它们比搬来的模块级语句多缩一层，`unindent does not match`。
    return textwrap.dedent("\n".join(out))


def _seg(src: str, node: ast.AST) -> str:
    """按行号取回一段源码原文（含装饰器）。"""
    lines = src.splitlines()
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    end = node.end_lineno or node.lineno
    return "\n".join(lines[start - 1:end])


def _is_main_block(node: ast.AST) -> bool:
    return (isinstance(node, ast.If)
            and any(isinstance(s, ast.Constant) and s.value == "__main__"
                    for s in ast.walk(node.test)))


def _is_exit(node: ast.AST) -> bool:
    """`sys.exit(...)` / `raise SystemExit(...)` —— 搬迁块里要删掉的收尾。"""
    if isinstance(node, ast.Raise) and node.exc is not None:
        exc = node.exc
        return ((isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name)
                 and exc.func.id == "SystemExit")
                or (isinstance(exc, ast.Name) and exc.id == "SystemExit"))
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        f = node.value.func
        if isinstance(f, ast.Attribute) and f.attr == "exit" and isinstance(f.value, ast.Name):
            return f.value.id == "sys"
        if isinstance(f, ast.Name) and f.id == "exit":
            return True
    return False


def _target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def _bound_names(node: ast.AST) -> list[str]:
    """一个**非定义**语句往模块级绑定的名字（含 import 的绑定名）。

    `_target_names` 只认赋值，看不出 `import a.b as c` 绑的是 `c`、`from m import n` 绑的是 `n`
    —— 而"import 重绑一个已有名字"正是 `test_account_freeze.py` 踩的那个坑。
    """
    if isinstance(node, ast.Import):
        return [a.asname or a.name.split(".", 1)[0] for a in node.names]
    if isinstance(node, ast.ImportFrom):
        return [a.asname or a.name for a in node.names]
    if isinstance(node, ast.With):
        return [n.id for it in node.items if isinstance(it.optional_vars, ast.Name)
                for n in [it.optional_vars]]
    if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return _target_names(node)


def _late_rebind_hazard(tree: ast.Module, keep_idx: set[int], move_idx: list[int]) -> list[str]:
    """被搬走的代码读到的名字，有没有被某条**更晚的**留模块级非定义语句重新绑定？

    这是**顺序**问题，不是重名问题，`clash` 那条抓不到：原文件里
    `import agent.adminops_confirm as _confirm`（27 行）、545 行用、**711 行又
    `from agent import confirm as _confirm`** —— 三次都在模块级、按源码顺序跑，所以 545 行
    看到的是前一个。迁移把 545 行那条判据搬到了**末尾** ⇒ 它只能看到最后一次绑定，
    读到 `agent.confirm` 上没有的属性（`test_account_freeze` / `test_account_role` 实测：
    `AttributeError: module 'agent.confirm' has no attribute 'render_confirm_question'`）。

    定义（def/class）不算：它们在模块加载时全部就位，驱动跑之前就都是最终值，没有顺序问题。
    """
    moved_reads = set()
    for i in move_idx:
        moved_reads |= _read_names(tree.body[i])
    hazards: list[str] = []
    for i in sorted(keep_idx):
        n = tree.body[i]
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for name in _bound_names(n):
            if name not in moved_reads:
                continue
            earlier = any(tree.body[j].lineno < n.lineno and name in _read_names(tree.body[j])
                          for j in move_idx)
            if earlier:
                hazards.append(f"{name}@L{n.lineno}")
    return sorted(set(hazards))


def _rename_ids(src: str, tree: ast.Module, renames: dict[str, str]) -> str:
    """助手改名（`test_x` → `_test_x`）：**按 AST 位置改标识符，不做文本替换**。

    两个坑把文本替换否掉了：

      · `def test_x():` 里含子串 `test_x()`，所以"只改调用点"的 `str.replace` 会连定义行
        一起改 —— 看着正好，但先改定义行再全局替换就会把 `_test_x()` 再改一次成 `__test_x()`；
      · 形态 D 的 `__main__` 里是 `for fn in (test_a, test_b): fn()` —— 那是对**裸名**的
        引用，不是调用点，`name()` 形式的替换根本扫不到。

    位置取自**原文**（未重新分段的源码），所以行号/列号都有效；改名不引入换行，行号不变，
    下游 `_seg` 照旧可用。列号是 **UTF-8 字节偏移**，本仓满屏中文，所以逐行 encode 再切。
    """
    if not renames:
        return src
    lines = src.splitlines()
    spans: list[tuple[int, int, int, str, str]] = []      # lineno, c0, c1, old, new
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in renames:
            spans.append((n.lineno, n.col_offset, n.end_col_offset, n.id, renames[n.id]))
        elif isinstance(n, ast.FunctionDef) and n.name in renames:
            c0 = n.col_offset + 4                          # `def ` 之后
            spans.append((n.lineno, c0, c0 + len(n.name), n.name, renames[n.name]))
    for lineno, c0, c1, old, new in sorted(spans, reverse=True):
        raw = lines[lineno - 1].encode("utf-8")
        got = raw[c0:c1].decode("utf-8")
        if got != old:
            raise Manual(f"L{lineno} 第 {c0} 列应是 {old!r}，实际 {got!r}")
        lines[lineno - 1] = (raw[:c0] + new.encode("utf-8") + raw[c1:]).decode("utf-8")
    return "\n".join(lines)


def _read_names(node: ast.AST) -> set[str]:
    """一个语句里读到的裸名（`Load` 上下文）。"""
    return {s.id for s in ast.walk(node)
            if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Load)}


def _free_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """函数体真正依赖的**模块级**名字 = 读到的名字 − 它自己的参数与局部绑定。

    ⚠️ 直接用 `_read_names(def)` 是错的：它会把参数和局部变量一起算成"依赖"，于是
    `def _use(post): return post.foo` 里的 `post` 会让模块级的 `post = _Post(...)`
    被"救"回模块级 —— 那个 `post` 本来该跟着流水线搬进驱动（`test_admin_write` 实测
    救出一片假"重绑"）。局部绑定按 AST 粗算：Store/Del 的 Name、内层 def/class 名、
    内层 import 的绑定名、`except ... as e` 的 `e`，以及**内层函数的参数**
    （`def _run(): def inner(out): return out.foo` 里的 `out` 是 `inner` 的参数，
    不算进来就会漏成"模块依赖" —— `test_todo_schedule` 实测：模块级
    `out = expand(...)` 整条流水线被救回模块级，于是又跟驱动撞成"重绑"）。
    """
    reads = _read_names(node)
    bound: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(sub.name)
            a = sub.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
                bound.add(arg.arg)
            for extra in (a.vararg, a.kwarg):
                if extra is not None:
                    bound.add(extra.arg)
        elif isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            bound.add(sub.id)
        elif isinstance(sub, ast.ClassDef):
            bound.add(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            bound |= set(_bound_names(sub))
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            bound.add(sub.name)
    return reads - bound


def _module_deps(node: ast.AST) -> set[str]:
    """一条**定义**（def / class）真正依赖的模块级名字。

    `class` 只走类体那一层、并且**方法用 `_free_names`** —— 早先图省事用
    `_read_names(classdef)`，把方法里的局部名也当成了模块依赖（同一族的漏）。
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _free_names(node)
    if not isinstance(node, ast.ClassDef):
        return set()
    deps: set[str] = set()
    bound: set[str] = set()
    for st in node.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            deps |= _free_names(st)
        elif isinstance(st, ast.ClassDef):
            deps |= _module_deps(st)
        else:
            deps |= _read_names(st)
            bound |= set(_bound_names(st))
    return deps - bound


def _dotted(node: ast.AST) -> str:
    """`a.b.c` 形态的点号路径（认不出返回空串）。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_import_setup(node: ast.AST) -> bool:
    """**import 期 setup** —— 只有这三种必须留在模块级，否则本模块自己的 `import` 会失败。

      · `sys.path.insert/append(...)`（212 处，把仓根与 `eval/` 挂进 import 路径）；
      · `os.environ.setdefault(...)`（后续 import 在导入期读环境）；
      · `sys.modules[...] = ...`（桩一个模块）。

    ⚠️ **这一条必须窄**。早先写的是"最后一个 import 之前的整段都留"，实测是错的：
    本仓有一族套件的 import 是**散着写**的（`test_account_freeze.py` 到 593 行还在
    `import agent.authz`），于是"前导段"把**整段测试体**都留下了 —— 普查出 1649 个
    `Assign` 加一批 `With`/`For`/`Try`/`If`。两个后果都实测到了：
      · 判据调用留在模块级、它引用的量被搬进函数 ⇒ `NameError: name '_nxt' is not defined`；
      · 前导段绑一个 `_payload`、驱动函数里又绑一个同名的 ⇒ 驱动里**局部遮蔽全局**，
        原来读全局的那句变成 `UnboundLocalError`（`test_chat_quota.py` 实测）。
    """
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        t = node.targets[0]
        return isinstance(t, ast.Subscript) and _dotted(t.value).startswith("sys.modules")
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
        return False
    p = _dotted(node.value.func)
    return p.startswith("sys.path.") or p.startswith("os.environ.")


def _classify(tree: ast.Module) -> tuple[set[int], list[int]]:
    """把顶层语句分成「留模块级」与「搬进驱动」，返回 `(keep_idx, move_idx)`。

    三条规则，缺一条都会踩到实测过的坑：

    **① 定义留原地** —— docstring / `import` / `def` / `class`。

    **② import 期 setup 留原地** —— `sys.path.insert(0, str(ROOT))` 这类，搬走会让本模块
    自己的 `import` 当场失败；它读的 `ROOT = ...` 由 ③ 兜住。判据口径见 `_is_import_setup`
    （**必须窄**，写成"最后一个 import 之前的整段都留"会踩两个实测到的坑）。

    **③ 名字依赖闭包（不动点）** —— 被"留下的语句"读到的模块级常量必须一起留下
    （`TOOL_NAMES` / `READY_KEYS` 那一族：搬进函数它就变局部名，助手读全局当场
    `NameError`）。**必须迭代到不动点**：`Y = BIG + 1` 被留（助手读 `Y`）时 `BIG` 也得留，
    只看一层会漏。

    `__main__` 块两边都不进 —— 它由 `_main_body` 单独取正文。
    """
    keep_idx: set[int] = set()
    move_idx: list[int] = []
    for i, n in enumerate(tree.body):
        if _is_main_block(n):
            continue
        if i == 0 and isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, str):
            keep_idx.add(i)                       # 模块 docstring
            continue
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            keep_idx.add(i)
            continue
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            keep_idx.add(i)
            continue
        if _is_import_setup(n):
            keep_idx.add(i)                       # ② import 期 setup
            continue
        move_idx.append(i)

    # ③ 名字依赖闭包，到不动点。**种子只取两类读者**：
    #    · 留模块级的 def/class 体（`TOOL_NAMES` / `READY_KEYS` 那一族 —— 搬进函数它就变
    #      局部名，助手读全局当场 `NameError`）；
    #    · import setup 语句（`sys.path.insert(0, str(ROOT))` 要 `ROOT`）。
    #    ⚠️ **不能拿"所有留下的语句"当种子**：`post = _Post(...)` 只要被任何一条留下的语句
    #    读到就会被救回模块级，而它本来是模块级流水线上的一环 —— 救回来之后又跟驱动里的
    #    同名绑定撞上（`test_admin_write` 实测出一片假"重绑"）。
    seed_reads: set[str] = set()
    for i in keep_idx:
        n = tree.body[i]
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seed_reads |= _module_deps(n)
        elif _is_import_setup(n):
            seed_reads |= _read_names(n)
    while True:
        rescued = [i for i in move_idx
                   if _target_names(tree.body[i])
                   and all(t in seed_reads for t in _target_names(tree.body[i]))]
        if not rescued:
            break
        for i in rescued:
            keep_idx.add(i)
            move_idx.remove(i)
            seed_reads |= _read_names(tree.body[i])

    # ③′ **反向规则**（③ 的对偶）：被搬走的代码读到的名字，如果它的绑定语句是"留模块级的
    #     非定义语句"（import / 赋值），而**没有任何留模块级的 def/class 或 import-setup 读它**
    #     ⇒ 那条绑定语句**也搬进驱动**。
    #
    #     驱动必须自己走一遍原文件那条时间线。留在模块级的绑定会在**驱动之前**跑完，驱动
    #     只能看到**最后一次**绑定，而原文件里那些读可能夹在两次绑定中间：
    #       · `import agent.adminops_confirm as _confirm`（27 行）→ 545 行用 → **711 行又
    #         `from agent import confirm as _confirm`** ⇒ 迁移后 545 行那条判据读到
    #         `agent.confirm`，报 `AttributeError: module 'agent.confirm' has no attribute
    #         'render_confirm_question'`（test_account_freeze / test_account_role /
    #         test_user_notice 实测）；
    #       · `D = mkdirs(...)` → `r = tr.reconcile(D, ...)` → 判据读 `r`，然后 `D`/`r`
    #         各重绑 10 次、判据夹在中间（test_reconcile_offline 实测）。
    #
    #     有 def/class 读它时**不能**搬下去（那是 ③ 要保的常量），交人工。
    protected: set[str] = set()
    for i in keep_idx:
        n = tree.body[i]
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            protected |= _module_deps(n)
        elif _is_import_setup(n):
            protected |= _read_names(n)
    binders: dict[str, int] = {}
    for i in keep_idx:
        for b in _bound_names(tree.body[i]):
            binders[b] = binders.get(b, 0) + 1

    def _movable_down(i: int) -> bool:
        n = tree.body[i]
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return False
        if _is_import_setup(n):
            return False
        names = _bound_names(n)
        if not names or set(names) & protected:
            return False
        if not all(b in moved_reads for b in names):
            return False
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            # import 只在**真的重绑**时才搬下去（同名有 ≥2 处留模块级的绑定）。
            # 普通 import 留在原地 —— 全搬进函数会让每一份迁移 diff 都变味，而它没有
            # 任何正确性收益（只绑一次的名字，模块级那份与驱动看到的是同一个值）。
            return all(binders.get(b, 0) >= 2 for b in names)
        return True

    while True:
        moved_reads = set()
        for i in move_idx:
            moved_reads |= _read_names(tree.body[i])
        down = [i for i in keep_idx if _movable_down(i)]
        if not down:
            break
        for i in down:
            keep_idx.discard(i)
            move_idx.append(i)
        move_idx.sort()

    # 同名两处绑定 ⇒ 驱动函数里的那个会把模块级的**遮蔽**掉，原来读全局的语句变成
    # `UnboundLocalError`。认得出就救（模块级那处没人读时搬进驱动、只剩一个作用域），
    # 认不出就交人工 —— 猜错了是静默改语义。
    moved_names = {t for i in move_idx for t in _target_names(tree.body[i])}
    kept_names = {t for i in keep_idx for t in _target_names(tree.body[i])}
    clash = moved_names & kept_names
    if clash:
        readers = set()
        for i in keep_idx:
            readers |= _read_names(tree.body[i])
        unrescuable = sorted(n for n in clash
                             if any(n in _target_names(tree.body[i]) for i in keep_idx
                                    if _is_import_setup(tree.body[i])) or n in readers)
        if unrescuable:
            raise Manual(f"同名两处绑定且模块级那处被读到：{unrescuable} —— 交人工")
        for i in sorted(keep_idx):
            if set(_target_names(tree.body[i])) & clash:
                keep_idx.discard(i)
                move_idx.append(i)
        move_idx.sort()

    late = _late_rebind_hazard(tree, keep_idx, move_idx)
    if late:
        raise Manual(f"被搬的代码读到的名字在更晚处被重绑：{'、'.join(late)} —— 交人工")
    return keep_idx, move_idx


def _rename_map(tree: ast.Module, stem: str) -> dict[str, str]:
    """顶层 `def test_*` 一律变助手（`_` 前缀）。

    它们调判据收集器往**模块级** FAILS 里塞，断言在模块尾、不在自己身上 —— 被 pytest
    单独收集会**恒绿**（正是 PR #38 那条注说的假绿）。`stem` 自己排除掉：那是驱动函数的
    名字，改了会自递归。
    """
    return {n.name: "_" + n.name for n in tree.body
            if isinstance(n, ast.FunctionDef)
            and n.name.startswith("test_") and n.name != stem}


def _exit_code_tail(tree: ast.Module, main_node: ast.If | None) -> str:
    """形态 A′ 的收口：`assert <入口>() == 0`。

    认不出就抛 Manual —— 猜错了是**静默把红判成绿**（`assert` 写错成恒真，或者干脆漏掉
    整个收口），那是这件事最贵的失效。
    """
    names: list[str] = []
    for st in (main_node.body if main_node is not None else []):
        inner = _exit_inner(st) if _is_exit(st) else (
            st.value if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) else None)
        if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                and _zero_arg_fn(tree, inner.func.id)):
            names.append(inner.func.id)
    if len(names) != 1:
        raise Manual(f"没有判据收集器，且认不出唯一的入口函数调用（{names}）—— 交人工")
    return (f"    assert {names[0]}() == 0, "
            f"f\"{names[0]}() 非零：上面那些 ❌ 就是原因\"")


def _indent(text: str) -> str:
    """整块 +4 空格（空行不动），再补一个尾空行。"""
    if not text.strip():
        return ""
    return "\n".join("    " + ln if ln.strip() else ln
                     for ln in text.splitlines()) + "\n\n"


def transform(path: pathlib.Path) -> str:
    """返回改写后的全文；认不出就抛 Manual。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    stem = path.stem

    fail_name = _fail_name(tree)
    main_node = next((n for n in tree.body if _is_main_block(n)), None)
    keep_idx, move_idx = _classify(tree)

    # 顶层已经有 `def <stem>()` ⇒ 这个文件已经是迁移后的形态（PR #38 的 10 个试点）。
    # 不拦的话下面的驱动会跟它重名 ⇒ 自递归。
    if any(isinstance(n, ast.FunctionDef) and n.name == stem for n in tree.body):
        raise Manual(f"顶层已有 def {stem}()（已经是原生形态）")

    if not move_idx and main_node is None:
        raise Manual("没有可搬的模块级语句、也没有 __main__ 块")

    # ── 改名 ───────────────────────────────────────────────────────────────
    # **在分段之前**按 AST 位置做，这样 `__main__` 里对它们的裸名引用（形态 D 的 for
    # 循环）也跟着改。
    src = _rename_ids(src, tree, _rename_map(tree, stem))

    # ── 留下的部分 ─────────────────────────────────────────────────────────
    out = [_seg(src, n) for i, n in enumerate(tree.body) if i in keep_idx]

    # ── 驱动函数体 = 搬来的模块级语句 + `__main__` 块正文 ────────────────────
    # 顺序正是今天的执行顺序（模块级先跑，`__main__` 块最后）。
    moved = [_seg(src, n) for n in (tree.body[i] for i in move_idx) if not _is_exit(n)]
    if main_node is not None:
        moved.append(_main_body(src, tree, main_node, drop_entry=fail_name is None))
    head = "\n".join(s for s in moved if s.strip())
    # 形态 A′ 允许 head 为空（整个套件都在入口函数里，收口那句自己调它）。
    if not head.strip() and (main_node is None or fail_name is not None):
        raise Manual("搬迁块 + __main__ 正文合起来是空的")

    if fail_name is None:
        # 形态 A′：没有判据收集器，收口在入口函数返回的退出码上。
        tail = _exit_code_tail(tree, main_node)
    else:
        tail = (f"    assert not {fail_name}, (\n"
                f"        f\"{{len({fail_name})}} 条未通过：\" + \"；\".join({fail_name}))")
    out.append(f"def {stem}():\n{_indent(head)}{tail}")

    text = "\n\n\n".join(out).rstrip() + "\n\n\n"
    text += f'if __name__ == "__main__":\n    {stem}()\n'

    try:
        ast.parse(text)
    except SyntaxError as exc:
        raise Manual(f"生成结果语法不合法：{exc}") from exc
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", type=pathlib.Path)
    ap.add_argument("--all", action="store_true", help="体检 tests/ 下全部 test_*.py")
    ap.add_argument("--check", action="store_true", help="只报告，不写回")
    args = ap.parse_args()

    paths = sorted(TESTS.glob("test_*.py")) if args.all else args.paths
    if not paths:
        ap.error("给路径，或 --all")

    ok = manual = 0
    for p in paths:
        try:
            text = transform(p)
        except Manual as exc:
            manual += 1
            print(f"MANUAL  {p.name}  ← {exc}")
            continue
        except SyntaxError as exc:
            manual += 1
            print(f"MANUAL  {p.name}  ← 解析失败 {exc}")
            continue
        ok += 1
        print(f"OK      {p.name}")
        if not args.check:
            p.write_text(text, encoding="utf-8")
    print(f"\n可自动转写 {ok} 个；需人工 {manual} 个（共 {len(paths)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
