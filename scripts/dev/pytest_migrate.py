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

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TESTS = ROOT / "tests"


class Manual(Exception):
    """认不出的形状 —— 不动它，交人工。理由写在消息里。"""


_FORCE = False
"""`--force`：**值依赖运行时状态**那几条 hazard 也照转，把 hazard 打成警告。

为什么留这个口子（20261020）：那几条 hazard 的处置是"交人工"，而人工的起点**不是**
从零写驱动函数（那是一千多行的活），而是**拿 codemod 的成品逐条核**。`--force` 就是
那个"给我一份九成对、把可疑点标出来的草稿"的入口 —— 出来的文件**必须**再跑三层对拍，
不能因为它是"人工过了一遍"就免检。
"""


def _hazard(msg: str) -> None:
    """值依赖运行时状态的 hazard：默认交人工，`--force` 下只警告。"""
    if _FORCE:
        print(f"  ⚠️ --force 照转，但这一条要人工核：{msg}")
        return
    raise Manual(msg)


_SINK_DEFS: set[str] = set()
"""`--sink-def <名字>`：把这几条**模块级 def 也搬进驱动**（人工判定，默认空）。

它是 `_late_rebind_hazard` 那个死结的出口：`test_reconcile_offline.py` 实测 ——
`ids = {… for c in r["flow_clues"]}` 被规则 ③ 救回模块级（模块级 `def stages_of(cid)`
读它），于是 `r` 进种子、`r = tr.reconcile(…)` 那十条**全部**被救回模块级，而驱动里
那些读 `r` 的判据跑在**最后一次**赋值之后 ⇒ 27 条红。真正的病灶是 `stages_of` 读的
根本不是"模块级常量"，而是**场景变量** `ids`。

搬 `stages_of` 进驱动 ⇒ `ids` 不再被救、`r`/`D` 整条链跟着下去、原文件那条时间线保住。
**为什么不做成自动规则**：静态分不出"这条 def 读的是场景变量还是常量"，猜错的方向是
**静默改语义**（正是这件事要防的）；所以留成人工点名，且成品照样必须过三层对拍。
"""


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


def _main_body(src: str, tree: ast.Module, node: ast.If,
               drop_entry: bool = False) -> list[tuple[int, str]]:
    """`if __name__ == "__main__":` 块的**正文**，`[(原始行号, 原文)]`。

    **驱动函数体就是它**，所以这里要做的只有一件事：处理收尾。

      · `sys.exit(<模块级零参函数>())` ⇒ **拆成裸调用**（`sys.exit(main())` → `main()`）；
        入口函数的 `return 0/1` 因此原样留着，不用搬它的函数体（搬了尾部的 assert 会
        变成不可达代码 ⇒ 恒绿）。
      · 其余收尾（`sys.exit(1)` / `raise SystemExit(...)`）⇒ **整条丢**。
        只丢**顶层**的：嵌套在 `if FAILS:` 里的 `sys.exit(1)` 原样留着 —— 实测 pytest
        在任意嵌套深度都把 `SystemExit` 判为失败（`/tmp` 探针），行为等价，不动更安全。

    **正文保持原文缩进、不再去平**：它本来就在 `if` 块里、已经缩了 4 格，正好是驱动
    函数体要的层级，跟搬来的模块级语句（+4）对得上。早先去掉平再统一 +4，既多此一举，
    也会顺手把多行字符串的内容改坏（同 `_indent_block` 那条注）。
    """
    if not node.body:
        return []
    if node.body[0].lineno == node.lineno:
        raise Manual('`__main__` 块的语句跟 `if` 同行，认不出')
    out: list[tuple[int, str]] = []
    for st in node.body:
        if _is_exit(st):
            inner = _exit_inner(st)
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                    and _zero_arg_fn(tree, inner.func.id)):
                if not drop_entry:            # 形态 A′ 由收口那句 `assert F() == 0` 调它
                    # 拆出来的裸调用要补上 `__main__` 体那一层的缩进（它不再是 `sys.exit(…)`
                    # 那种"表达式的一部分"，而是一条独立语句）。
                    out.append((st.lineno,
                                " " * node.body[0].col_offset + f"{inner.func.id}()"))
            continue
        out.extend(_seg_pairs(src, st))
    return out


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


def _flatten_target(t: ast.AST) -> list[str]:
    """赋值目标里的裸名，**元组/列表要展开**。`Attribute` / `Subscript` 不算模块级裸名。"""
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, (ast.Tuple, ast.List)):
        return [n for e in t.elts for n in _flatten_target(e)]
    return []


def _target_names(node: ast.AST) -> list[str]:
    """一条语句在**模块级**绑定的裸名。

    ⚠️ 目标必须展开元组：`_W0, _W1 = "add_favorite", "toggle_effect"` 是本仓的日常写法，
    只认顶层 `Name` 会把它读成"不绑定任何名字"，于是留模块级的
    `def _run(..., names=(_W0, _W1))` 依赖不到东西、`_W0`/`_W1` 被搬进驱动 ⇒ 模块级
    执行那条 `def` 时默认参数求值当场 `NameError: name '_W0' is not defined`
    （`test_write_idempotency` 实测）。
    """
    if isinstance(node, ast.Assign):
        return [n for t in node.targets for n in _flatten_target(t)]
    if isinstance(node, ast.AnnAssign) and node.target is not None:
        return _flatten_target(node.target)
    return []


def _all_bound_names(node: ast.AST) -> set[str]:
    """一条**顶层语句**在模块级绑定的所有名字，**含嵌套块里的**。

    ⚠️ 只认顶层 `Assign` 是不够的：本仓有一族把绑定写在模块级 `try:` / `with:` / `if:`
    里（`test_pg_retention_live.py` 的 `CONN = psycopg.connect(...)` 就在一个 `try:` 里）。
    那种语句 `_target_names` 返回空 ⇒ 规则 ③ 看不见它 ⇒ 整块被搬进驱动，而留模块级的
    `def _count()` / `def _wipe()` 还在读 `CONN` ⇒ **F821 Undefined name `CONN`**
    （`test_undefined_names` 抓到，两个套件共 19 条）。三层对拍**看不见这一类**：
    那两个套件前提不在场、`_skip()` 提前退 0，两轨退出码一样，第 ①② 层都判绿。

    「有没有人读」是 ③ 的问法，「绑了哪些」要问到底 —— 整块搬走还是整块留下，中间没有
    第三种切法。
    """
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
            for t in targets:
                if t is not None:
                    out.update(_flatten_target(t))
        elif isinstance(sub, ast.For) and isinstance(sub.target, ast.Name):
            out.add(sub.target.id)
        elif isinstance(sub, ast.withitem) and isinstance(sub.optional_vars, ast.Name):
            out.add(sub.optional_vars.id)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            out.add(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            out.update(_bound_names(sub))
    return out


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


_MUTATORS = {"append", "add", "update", "extend", "insert", "pop", "setdefault",
             "clear", "remove", "discard", "sort", "reverse", "popitem"}


def _value_reads(node: ast.AST) -> set[str]:
    """这条语句里被**当值取**的模块级名（`X.attr` / `X[k]` / `X.f()` 的那个 `X`）。

    与 `_read_names`（裸名也算）的区别见 `_container_effect_hazard` 里那条注。
    """
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Attribute, ast.Subscript)) and isinstance(sub.value, ast.Name):
            out.add(sub.value.id)
    return out


def _mutated_names(nodes: list[ast.AST]) -> set[str]:
    """这批语句**改到**的模块级容器名（`X[k] = ...` / `X.append(...)` 那一族）。

    只看"改"不看"读"：读一个容器不会让它的内容在 import 期与驱动里不一致。
    """
    out: set[str] = set()
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = (sub.targets if isinstance(sub, ast.Assign)
                           else [sub.target])
                for t in targets:
                    # **只认下标**（`X[k] = ...`），不认属性（`X.y = ...`）。
                    # 属性赋值在本仓多半是**猴补丁模块**（`base._client = ...` /
                    # `report_archive.open = ...`），而"读这个模块的某个函数"跟那份补丁
                    # 无关 —— 认了会一片假阳性（`test_account_freeze` / `test_account_role` /
                    # `test_chat_quota` / `test_user_notice` 四个实测）。
                    # 下标赋值才是"往一个容器里塞东西"，那才会让 import 期算出来的值过期。
                    if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):
                        out.add(t.value.id)
            elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                  and sub.func.attr in _MUTATORS
                  and isinstance(sub.func.value, ast.Name)):
                out.add(sub.func.value.id)
    return out


def _container_effect_hazard(tree: ast.Module, keep_idx: set[int],
                             move_idx: list[int], rescued_idx: set[int]) -> list[str]:
    """③ 救回模块级的语句读了「被驱动改过的容器」⇒ 交人工。

    实测（`test_cross_language_tables.py`）：`sites` 是模块级 dict，由驱动里的
    `sites[label] = read_site(...)` 逐条填；而 `UNIVERSE = frozenset().union(*sites.values())`
    因为留模块级的助手读它被 ③ 救回模块级 ⇒ **在驱动填之前**就算完了，取到空集，
    整族判据变成空跑（旧文件绿、新文件红）。

    这是"值依赖运行时状态"那一族里**能静态认出来**的一支（另一支是猴补丁，见
    `test_todo_schedule`：静态分不出 `A.datetime = ...` 会不会影响 `_todo.normalize_due_date`，
    那支交给对拍第 ② 层，认不出就不动）。
    """
    mutated = _mutated_names([tree.body[i] for i in move_idx])
    if not mutated:
        return []
    out: list[str] = []
    for i in sorted(rescued_idx):
        # 只看**把它当值取**的用法（`X.attr` / `X[k]` / `X.f()`），不看裸名。
        # 裸名多半是 `try: ... base ... except:` 那种"引到名字就行"的块 —— 它对
        # `base._client` 的改动**不敏感**（`test_account_freeze` / `test_account_role` /
        # `test_chat_quota` / `test_user_notice` 四个实测是假阳性）。
        hit = sorted(_value_reads(tree.body[i]) & mutated)
        if hit:
            out.append(f"{'、'.join(_target_names(tree.body[i]))}@L{tree.body[i].lineno}"
                       f"（读 {'、'.join(hit)}）")
    return out


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


def _comp_targets(node: ast.AST) -> set[str]:
    """comprehension / 生成器表达式的**目标名**（作用域限于那条 comprehension）。

    不排除掉，规则 ③ 会认出一片**假的**模块级依赖（`test_chat_quota` 实测）：
    `DIRD = {r['id']: r for r in DIR}` 里的 `r` 是 comprehension 的局部，被算成
    "这条语句读模块级的 `r`" ⇒ `r` 进 `seed_reads` ⇒ 856 行那个
    `try: … r = _run_exec(…)` 被整块**救回模块级**（它绑 `r`）⇒ 它内部的
    `_payload = _confirm.inspect(…)`（原文件 866 行）也就留在模块级，而驱动里
    297 行那条读 `_payload` 的判据跑在它**之后** ⇒ 读到的是字典不是字符串，
    `_payload[:90]` 当场 `KeyError: slice`。

    ⚠️ 海象 `[y := f(x) for x in z]` 的 `y` **会**泄漏到外层作用域，所以不在这里减。
    已知的过度收窄：`[x for x in x]`（iterable 里那个 `x` 是外层那份）会被一起减掉 ——
    代价是模块级绑定被搬进驱动、留模块级的 def 读它 ⇒ **F821 当场响**（响亮失败，
    不是静默改语义），而本仓的验证清单里 F821 是必跑项。
    """
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for gen in sub.generators:
                out |= set(_flatten_target(gen.target))
    return out


def _read_names(node: ast.AST) -> set[str]:
    """一个语句里读到的裸名（`Load` 上下文）——**减掉 comprehension 自己的目标名**。"""
    return {s.id for s in ast.walk(node)
            if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Load)} - _comp_targets(node)


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
    declared_global: set[str] = set()
    for sub in ast.walk(node):
        # `global X` / `nonlocal X` 声明的名字**不是**这个函数的局部 —— 它读写的正是
        # 模块级那份。不排除掉就会被当成"局部绑定"从依赖里减掉，于是模块级
        # `_SEQ = 0` 被搬进驱动，而留模块级的 `def _probe()` 里 `global _SEQ; _SEQ += 1`
        # 当场 `NameError: name '_SEQ' is not defined`（`test_task_ledger_probe` 实测）。
        if isinstance(sub, (ast.Global, ast.Nonlocal)):
            declared_global |= set(sub.names)
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
    return reads - (bound - declared_global)


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


def _sys_modules_key(node: ast.AST) -> str | None:
    """这条语句写的是 `sys.modules[哪个键]`（不写就是 None）。

    用途见 `_seed_split`：同一个键被**写两次**的，是**场景步骤**不是 import 期 setup。
    """
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        t = node.targets[0]
        if isinstance(t, ast.Subscript) and _dotted(t.value).startswith("sys.modules"):
            try:
                return ast.unparse(t.slice)
            except Exception:                       # noqa: BLE001 —— 解不出来就当没看见
                return None
    if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and _dotted(node.value.func) == "sys.modules.setdefault" and node.value.args):
        try:
            return ast.unparse(node.value.args[0])
        except Exception:                           # noqa: BLE001
            return None
    return None


def _is_import_setup(node: ast.AST) -> bool:
    """**import 期 setup** —— 只有这三种必须留在模块级，否则本模块自己的 `import` 会失败。

      · `sys.path.insert/append(...)`（212 处，把仓根与 `eval/` 挂进 import 路径）；
      · `os.environ.setdefault(...)`（后续 import 在导入期读环境）；
      · `sys.modules[...] = ...` **与 `sys.modules.setdefault(...)`**（桩一个模块）。

    ⚠️ `setdefault` 那个形状是**实测补上的**：`test_word_graph_build.py` 用
    `sys.modules.setdefault("numpy", types.ModuleType("numpy"))` 给没装 numpy 的本机打桩，
    然后才 `import build_word_graph`。只认 `sys.modules[...] = ...` 会让这条桩被搬进驱动、
    而 import 留在模块级 ⇒ 导入期 `ModuleNotFoundError: No module named 'numpy'`
    （旧文件绿、新文件红）。

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
    # `sys.modules.` 只认 **`setdefault`**：`sys.modules.pop("agent", None)` 是**收尾清理**，
    # 不是 setup。写成 `p.startswith("sys.modules.")` 会把它留在模块级 ⇒ 它在**驱动之前**
    # 就把桩摘了，驱动里 `sys.modules["agent"]` 当场 `KeyError`（`test_golden_arm` 实测：
    # 那句 `pop` 在原文里是文件最后一行）。
    return (p.startswith("sys.path.") or p.startswith("os.environ.")
            or p == "sys.modules.setdefault")


def _classify(tree: ast.Module) -> tuple[set[int], list[int]]:
    """把顶层语句分成「留模块级」与「搬进驱动」，返回 `(keep_idx, move_idx)`。

    三条规则 + 两条救火规则，每条都对应一个**实测踩过的坑**（细则在各自函数里）：

    ① 定义留原地（docstring / `import` / `def` / `class`）  —— `_seed_split`
    ② import 期 setup 留原地（`sys.path.insert(...)` 那一族）—— `_seed_split`
    ③ 名字依赖闭包（不动点）：留下的代码读到的模块级常量必须一起留下 —— `_close_over_names`
    ③′ 反向规则：被搬代码读到的「留模块级的非定义绑定」也搬进驱动 —— `_close_over_names`
    同名遮蔽救火 —— `_rescue_clash`；顺序重绑认不出就交人工 —— `_late_rebind_hazard`

    拆成四个函数不是为好看：本仓 `tests/test_complexity_budget.py` 对函数圈复杂度有
    上限（C≤30），合在一起这一个函数是 C=46，自己就先撞上闸。
    """
    keep_idx, move_idx = _seed_split(tree)
    rescued = _close_over_names(tree, keep_idx, move_idx)
    _rescue_clash(tree, keep_idx, move_idx)
    cont = _container_effect_hazard(tree, keep_idx, move_idx, rescued)
    if cont:
        _hazard(f"救回模块级的语句读了被驱动改过的容器：{'、'.join(cont)} —— 交人工")
    late = _late_rebind_hazard(tree, keep_idx, move_idx)
    if late:
        _hazard(f"被搬的代码读到的名字在更晚处被重绑：{'、'.join(late)} —— 交人工")
    return keep_idx, move_idx


def _seed_split(tree: ast.Module) -> tuple[set[int], list[int]]:
    """规则 ①②：**定义**与 **import 期 setup** 留原地。

    ① 定义留原地 —— docstring / `import` / `def` / `class`。

    ② import 期 setup 留原地 —— `sys.path.insert(0, str(ROOT))` 这类，搬走会让本模块
    自己的 `import` 当场失败；它读的 `ROOT = ...` 由 ③ 兜住。判据口径见 `_is_import_setup`
    （**必须窄**，写成「最后一个 import 之前的整段都留」会踩两个实测到的坑）。

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
            if getattr(n, "name", None) in _SINK_DEFS:
                move_idx.append(i)                # `--sink-def`：人工点名的 def 也搬下去
                continue
            keep_idx.add(i)
            continue
        if _is_import_setup(n):
            keep_idx.add(i)                       # ② import 期 setup
            continue
        move_idx.append(i)

    # ② 的例外：**同一个 `sys.modules[键]` 被写两次**的那些，是场景步骤而不是 import 期
    #    setup，必须跟着驱动走、保住它们的先后。`test_silent_degradation.py` 实测：它把
    #    `sys.modules["rag.search"]` 先桩成"抛异常的索引"、后桩成"空索引"，两次之间夹着
    #    判据。两条都当 import 期 setup 留在模块级 ⇒ **两条都在驱动之前跑完**，驱动里只
    #    剩"空索引"那一份 ⇒ `_corpus_ready()` 不再抛、不再留 WARNING，判据红
    #    （旧文件绿、新文件红）。写一次的照旧留在模块级（`sys.path.insert` 那 212 处不受影响）。
    _mod_writes: dict[str, int] = {}
    for n in tree.body:
        k = _sys_modules_key(n)
        if k is not None:
            _mod_writes[k] = _mod_writes.get(k, 0) + 1
    for i, n in enumerate(tree.body):
        if i in keep_idx and _is_import_setup(n):
            k = _sys_modules_key(n)
            if k is not None and _mod_writes.get(k, 0) >= 2:
                keep_idx.discard(i)
                move_idx.append(i)
    move_idx.sort()


    return keep_idx, move_idx

def _close_over_names(tree: ast.Module, keep_idx: set[int],
                      move_idx: list[int]) -> set[int]:
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
    rescued_idx: set[int] = set()
    while True:
        # 「**任何一个**绑定的名字被留模块级的代码读到 ⇒ 整条留下」。**不能写成 all**：
        #  · 元组赋值 `_chunks, _box = asyncio.run(...)` 只被读到一半（`_chunks` 被读、
        #    `_box` 没人读）时整条还是得留 —— 切不开（`test_chat_quota` 实测 F821
        #    `Undefined name '_chunks'`）；
        #  · 绑定写在 `try:` / `with:` 里的（`try: CONN = psycopg.connect(...)`），
        #    `_target_names` 根本看不见 ⇒ 整块被搬走，而模块级 `def _count()` 还在读
        #    `CONN`（`test_pg_retention_live` / `test_checkpoint_setup_concurrent` 共 19 条 F821）。
        rescued = [i for i in move_idx
                   if _all_bound_names(tree.body[i]) & seed_reads]
        if not rescued:
            break
        for i in rescued:
            keep_idx.add(i)
            move_idx.remove(i)
            rescued_idx.add(i)
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
    # `protected` = 「留模块级的东西读到的名字」，③′ **不许**把这些名字的绑定搬下去。
    # 三类读者：
    #   · 留模块级的 def/class 体；
    #   · import 期 setup；
    #   · **③ 自己救回来的语句**（`rescued_idx`）—— 这一类比看起来像"所有留下的语句"，
    #     但不是：它是 ③ 的**闭包产物**。漏掉它，③ 与 ③′ 会互相打架：③ 把 `DIR` 救回
    #     模块级给 `POL = {r["id"]: r for r in DIR}` 读，③′ 紧接着又因为"没有 def 读
    #     `DIR`"把它搬回驱动 ⇒ 模块级那行读一个驱动里的局部名，`NameError: name 'DIR'
    #     is not defined`（`test_account_freeze` 实测）。
    protected: set[str] = set()
    for i in keep_idx:
        n = tree.body[i]
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            protected |= _module_deps(n)
        elif _is_import_setup(n):
            protected |= _read_names(n)
        elif i in rescued_idx:
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
            # import 只在**真的重绑**时才搬下去（同名有 >=2 处留模块级的绑定）。
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

    return rescued_idx


def _rescue_clash(tree: ast.Module, keep_idx: set[int], move_idx: list[int]) -> None:
    # 同名两处绑定 ⇒ 驱动函数里的那个会把模块级的**遮蔽**掉，原来读全局的语句变成
    # `UnboundLocalError`。认得出就救（模块级那处没人读时搬进驱动、只剩一个作用域），
    # 认不出就交人工 —— 猜错了是静默改语义。
    # 用 `_bound_names` 而不是 `_target_names`：`from m import x` 也绑名字，而"同名两处
    # 绑定"最刁的一副面孔正是**两次 import 同一个名字**（`test_reports` 实测：模块级那份
    # 多行 import 留在原地，605 行那份被 ③′ 搬进驱动 ⇒ 驱动里 `_EXPLICIT_TOOLS` 变成
    # 局部名，而 525 行的判据在它之前用 ⇒ `cannot access free variable '_EXPLICIT_TOOLS'`）。
    moved_names = {t for i in move_idx for t in _bound_names(tree.body[i])}
    kept_names = {t for i in keep_idx for t in _bound_names(tree.body[i])}
    clash = moved_names & kept_names
    if clash:
        readers = set()
        for i in keep_idx:
            readers |= _read_names(tree.body[i])
        unrescuable = sorted(n for n in clash
                             if any(n in _target_names(tree.body[i]) for i in keep_idx
                                    if _is_import_setup(tree.body[i])) or n in readers)
        if unrescuable:
            _hazard(f"同名两处绑定且模块级那处被读到：{unrescuable} —— 交人工")
        for i in sorted(keep_idx):
            if set(_bound_names(tree.body[i])) & clash:
                keep_idx.discard(i)
                move_idx.append(i)
        move_idx.sort()


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
    # `sys.argv` 必须**收窄成只剩程序名**再调入口：这几个套件的 `main()` 里有
    # `argparse`（`--update` / `--c-max` 那一族），而 pytest 轨的 `sys.argv` 是 pytest 的
    # 命令行 ⇒ `error: unrecognized arguments: -q -p no:cacheprovider …`、SystemExit(2)
    # ⇒ 整个用例失败。直跑轨的 argv 就是 `['tests/test_x.py']`，收窄成 `[:1]` 正是**还原**
    # 它（`test_complexity_budget` / `test_module_budget` 实测：对拍报"直跑 rc=0，pytest fail"）。
    # ⚠️ **收窄必须只在 pytest 轨做**：这几个套件的 `--update` 是**人**在直跑时用的，
    # 无条件收窄会把 `python tests/test_module_budget.py --update` 的参数一起吃掉、
    # 静默退化成"只检查不重写"（第一版就是这么写的，`--update` 当场退 1）。
    return ("    _argv = sys.argv\n"
            "    if \"pytest\" in sys.modules:      # 只在 pytest 轨收窄（直跑轨的 CLI 照旧可用）\n"
            "        sys.argv = _argv[:1]\n"
            "    try:\n"
            f"        _rc = {names[0]}()\n"
            "    finally:\n"
            "        sys.argv = _argv\n"
            # 收口那句**不能用 f-string**：它一个占位符都没有，`f"…"` 会被 ruff 判
            # `F541 f-string without any placeholders`（实测三处：`test_complexity_budget`
            # / `test_module_budget` / `test_parent_repo_paths` 各一条，把三个套件从
            # "零 error" 顶成"新文件带 error"）。
            f"    assert _rc == 0, \"{names[0]}() 非零：上面那些 ❌ 就是原因\"")


def _seg_pairs(src: str, node: ast.AST) -> list[tuple[int, str]]:
    """`_seg` 的带行号版：`[(原始行号, 原文)]`，供**按行选择性加缩进**用。"""
    lines = src.splitlines()
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    end = node.end_lineno or node.lineno
    return [(n, lines[n - 1]) for n in range(start, end + 1)]


def _protected_lines(nodes: list[ast.AST]) -> set[int]:
    """**不许动**的行号：多行字符串字面量的内容行。

    搬迁块整体 +4 缩进，而多行字符串字面量的**内容**也是"行"—— 改了就是改被测数据。
    实测 `test_async_blocking_join.py`：它持有一段被测源码的字面量
    （`_OLD` 是一个装着整段被测源码的三引号字符串），加缩进后那段字符串变形，运行到
    `ast.parse` 报 `IndentationError: unexpected indent`，报错点在 `<unknown>`，与迁移
    本身看不出关系。

    保护区间取 `[lineno+1, end_lineno]`：**首行不保护**（那一行是语句本身，缩进必须有；
    字符串从行内某处才开始）。反过来，"只缩进语句起始行"是**另一个**错法：`ast.ExceptHandler`
    不是 `ast.stmt`，它的行不在语句集里 ⇒ `try:` 缩进了、`except` 没缩进 ⇒
    `expected 'except' or 'finally' block`（实测 40+ 个文件同时红）。所以改成
    "整段缩进、只把字符串内容挖出来"，两侧都堵上。
    """
    out: set[int] = set()
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                    and sub.end_lineno and sub.end_lineno > sub.lineno:
                out.update(range(sub.lineno + 1, sub.end_lineno + 1))
    return out


def _indent_block(pairs: list[tuple[int, str]], protected: set[int]) -> str:
    """给搬迁块 +4 缩进；`protected` 里的行（字符串内容、`__main__` 正文）原样保留。

    `__main__` 正文本来就在 `if` 块里、已经缩了 4 格，正好是驱动函数体要的层级 —— 再 +4
    就多一层（`unindent does not match any outer indentation level`，实测）。
    """
    out: list[str] = []
    for ln, text in pairs:
        if ln in protected:
            out.append(text)
        elif not text.strip():
            out.append("")
        else:
            out.append("    " + text)
    return "\n".join(out) + "\n\n"


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
    # 缩进**只加在语句起始行**上（`_indent_block`），所以这里必须带着原始行号走。
    moved_nodes = [tree.body[i] for i in move_idx if not _is_exit(tree.body[i])]
    pairs = [p for n in moved_nodes for p in _seg_pairs(src, n)]
    main_pairs = ([] if main_node is None
                  else _main_body(src, tree, main_node, drop_entry=fail_name is None))
    pairs.extend(main_pairs)
    # `__main__` 正文的行号全部保护起来 —— 它已经带着要的那层缩进（见 `_indent_block`）。
    head = _indent_block(pairs, _protected_lines(moved_nodes) | {ln for ln, _ in main_pairs})
    # 形态 A′ 允许 head 为空（整个套件都在入口函数里，收口那句自己调它）。
    if not head.strip() and (main_node is None or fail_name is not None):
        raise Manual("搬迁块 + __main__ 正文合起来是空的")

    if fail_name is None:
        # 形态 A′：没有判据收集器，收口在入口函数返回的退出码上。
        tail = _exit_code_tail(tree, main_node)
    else:
        tail = (f"    assert not {fail_name}, (\n"
                f"        f\"{{len({fail_name})}} 条未通过：\" + \"；\".join({fail_name}))")
    out.append(f"def {stem}():\n{head}{tail}")

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
    ap.add_argument("--force", action="store_true",
                    help="值依赖运行时状态的 hazard 也照转（只警告）——成品必须再过三层对拍")
    ap.add_argument("--sink-def", action="append", default=[], metavar="名字",
                    help="把这条模块级 def 也搬进驱动（人工判定：它读的是场景变量不是常量）")
    args = ap.parse_args()

    global _FORCE
    _FORCE = args.force
    _SINK_DEFS.update(args.sink_def)

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
