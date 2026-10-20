# -*- coding: utf-8 -*-
"""迁移对拍工装（20261020 批 1）：**唯一放行口**。

⚠️ **一次性工装**：20261020 那次迁移的放行口，今天 `tests/` 已是原生 pytest、旧 runner 已删。
它仍然可用（`--base <迁移前的 ref>` 就是"拿现在的 tests/ 跟任意旧版比"），但**只有配上
一个迁移前的 `--base` 才有意义** —— 拿 HEAD 当基线是自比、恒过。

`scripts/dev/pytest_migrate.py` 只负责搬位置；"搬对了没有"由这里三层判：

  ① **AST 层** —— 迁移前后同一文件的 `check(` 调用**条数**与每条**常量参数字面量**逐字相同。
     抓「判据被删 / 被改弱」（含"少 append 一条"这种最隐蔽的）。
     旧文本从 `git show <base>:<path>` 取。
  ② **行为层** —— 旧文件与**新文件直跑**的退出码必须相同（绿的仍绿、红的仍红）；
     且新文件的 pytest 轨结果 ⟺ 新文件直跑的退出码。抓「转写把语义改了」。
  ③ **反控层** —— 往新文件里注入一条必假判据，**两轨都必须红**。
     这一层不能省：①② 在"两轨一起坏"时都看不出来（同族教训见
     `docs/` 里"正控不红不算数"那条）。

**为什么旧文件要真的跑一遍**：①②只看文本与退出码的对应关系，如果转写恰好把"某条判据
变成永不执行"，①的条数还对得上（字还在），只有真跑旧文件才能发现"旧的红、新绿了"。

旧文本**不落进仓内**（`_run_source`：`exec` 配真 `__file__`）—— 早先写
`tests/_mig_old_<stem>.py`，`run_all.suites()` 确实不收它，但**扫仓的套件会看见**，
造出来的文件污染被测对象。

用法（**必须在提交前跑** —— 旧文本取自 `--base`，默认 `HEAD`；提交后 HEAD 就是新文本，
①② 两层退化成自比、恒过）：
    python3 scripts/dev/migration_parity.py --all
    python3 scripts/dev/migration_parity.py tests/test_x.py tests/test_y.py
    python3 scripts/dev/migration_parity.py --all --skip-layer2      # 只跑文本层（快）
    python3 scripts/dev/migration_parity.py --all --base origin/main

退出码：**0** 三层全过 / **1** 有红 / **3** 有"未评估"。
红与未评估分开是仓里既有约定：前提不成立时判据一次都没跑（跨语言守卫在父仓不在场时是
模块级 `pytest.skip`；`.env` 那类前提缺失同理），报绿是假的、报红是误伤。

⚠️ 从 **worktree** 跑要指一下父仓（跨语言守卫的 `SAUDADE_PARENT_REPO`，它默认找兄弟目录，
worktree 的兄弟目录里没有父仓 ⇒ 那一族会全 skip）：
    SAUDADE_PARENT_REPO=/home/ubuntu/Saudade-Blog python3 scripts/dev/migration_parity.py --all
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TESTS = ROOT / "tests"
MUTANT = "__MIGRATION_PARITY_MUTANT__"


def _pinned() -> dict[str, str]:
    """从 `conftest.py` 现取出厂档钉子（ast 取字面量，**不 import** —— 见 PR #38 同款注）。

    20261020 前这份表住在 `tests/run_all.py`（自研 runner，当天删除）⇒ 那时这条读的是它。
    读法没变：**ast 取字面量、不 import** —— import conftest 等于把整套环境摆一遍。
    """
    src = (TESTS / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_PINNED" for t in n.targets):
            return ast.literal_eval(n.value)
    raise SystemExit("conftest.py 里找不到 _PINNED")


_PINNED = _pinned()


def _child_env() -> dict[str, str]:
    """两条轨跑在**同一个 import 视野**下（`conftest.py` 的钉子 + `tests/` + 仓根）。

    仓根这一项**不是可选的**：本仓 `.venv` 是产线 venv，editable `.pth` 指向**主树**。
    从 worktree 跑时，直跑轨会顺着 `.pth` 去 import 主树那份 `config` / `server`，
    而 pytest 轨会往 `sys.path` 放仓根、import 的是 worktree 这份 —— 两轨于是"跑的是两份
    代码"，红绿对不上，看着像迁移把语义改了（`test_settings_surface` 实测：主树那份落后、
    没有 `stream_idle_timeout`，直跑轨当场 AttributeError，pytest 轨全绿）。
    真实环境里 `.pth` 指的就是当前这棵树、两边本来就一致，所以显式补仓根是**还原**它，
    不是给它开小灶。对 pytest 轨是空操作（它本来就放仓根）。
    """
    env = dict(os.environ)
    env.update(_PINNED)
    pp = os.pathsep.join([str(TESTS), str(ROOT)])
    if env.get("PYTHONPATH"):
        pp += os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = pp
    return env


def _run(path: pathlib.Path, timeout: float = 300.0) -> int:
    try:
        p = subprocess.run([sys.executable, str(path)], cwd=ROOT, env=_child_env(),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1
    return p.returncode


# 旧文本**不落进仓内**：跑法是 `python -c <这段> <临时文件> <真路径>`，用真路径当 `__file__`。
#
# ⚠️ 光把 `{'__name__': '__main__', '__file__': real}` 当 exec 的 globals **不够**：
# 那个字典不是 `sys.modules` 里的真模块，凡是**回头查模块全局**的库都会炸 ——
# `langgraph` 建 StateGraph 时 `get_type_hints(schema)` 去 `sys.modules[cls.__module__]`
# 拿全局，拿到空字典 ⇒ `NameError: name 'Annotated' is not defined`。表现是**旧文件假红**
# （`test_checkpoint_drop_turn` 实测：同一段文本当文件跑 rc=0、走 exec 跑 rc=1）。
# 正解是造一个真模块、**顶替 `sys.modules['__main__']`**（`python file.py` 就是这么干的），
# 这样 `__name__ == '__main__'` 与"全局可查"两件事同时成立。
#
# 同理 `sys.argv` 也要改：`python -c <runner> <tmp> <real>` 的 argv 是 `['-c', tmp, real]`，
# 凡是带 `argparse` 的套件（`test_complexity_budget` / `test_module_budget` 都有
# `--update`）会把 tmp 与 real 当成位置参数 ⇒ `error: unrecognized arguments`、**退 2**。
# 表现同样是旧文件假红（"旧 2 → 新 1"），看着像新旧不一致，其实是工装自己把参数递错了。
#
# 早先是写 `tests/_mig_old_<stem>.py` 再跑。两个问题，第二个是真缺陷：
#   · 以 `_` 开头 ⇒ `run_all.suites()` 不收（那半边想对了）；
#   · **但扫仓的套件会看见它** —— `test_complexity_budget` / `test_module_budget` /
#     `test_tests_dir_shape` / `test_parent_repo_paths` 是走 `ROOT.rglob` 的，那个临时文件
#     会进它们的账（`FileNotFoundError: tests/_mig_old_test_capability_truth.py` 实测：
#     扫描扫到一半文件被删了）。**造出来的文件污染被测对象**，这是工装最不该犯的错。
# 放 `/tmp` 也不行：`ROOT = Path(__file__).resolve().parent.parent` 会算成 `/`。
_OLD_RUNNER = (
    "import sys, types\n"
    "src = open(sys.argv[1], encoding='utf-8').read()\n"
    "real = sys.argv[2]\n"
    "mod = types.ModuleType('__main__')\n"
    "mod.__file__ = real\n"
    "sys.argv = [real]\n"
    "sys.modules['__main__'] = mod\n"
    "exec(compile(src, real, 'exec'), mod.__dict__)\n"
)


def _run_source(src: str, real: pathlib.Path, timeout: float = 300.0) -> int:
    """把一段源码当 `__main__` 跑一遍，`__file__` 指着**真路径**（`ROOT` 才算得对）。"""
    tmp = pathlib.Path(tempfile.gettempdir()) / f"_mig_old_{real.stem}.py"
    tmp.write_text(src, encoding="utf-8")
    try:
        p = subprocess.run([sys.executable, "-c", _OLD_RUNNER, str(tmp), str(real)],
                           cwd=ROOT, env=_child_env(), capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1
    finally:
        tmp.unlink(missing_ok=True)
    return p.returncode


_COUNT_RE = re.compile(r"(\d+) (passed|failed|skipped|error)")


def _pytest_outcome(path: pathlib.Path, timeout: float = 300.0) -> str:
    """pytest 轨跑单个文件，返回 `pass` / `fail` / `skip` 三态。

    **`skip` 必须与 `pass` 分开**，否则反控层会真空通过：跨语言守卫在父仓不在场时是
    **模块级 `pytest.skip`**（黄不是红），此时"注入一条必假判据"照样黄 —— 工装会报"反控通过"，
    而那一条判据根本一次都没跑（`test_board_dto_keys` 实测）。这正是仓里那条
    「正控不红不算数」的同一副面孔。
    """
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", str(path), "-q",
                            "-p", "no:cacheprovider", "--no-header", "-p", "no:randomly"],
                           cwd=ROOT, env=_child_env(), capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return "fail"
    counts = {k: int(v) for v, k in _COUNT_RE.findall(p.stdout)}
    if counts.get("failed") or counts.get("error"):
        return "fail"
    if counts.get("passed"):
        return "pass"
    return "skip"          # 全 skip / 一个都没收集到（rc 5）/ 收集期就崩了


def _old_text(path: pathlib.Path, base: str) -> str | None:
    rel = path.resolve().relative_to(ROOT)
    p = subprocess.run(["git", "show", f"{base}:{rel}"],
                       cwd=ROOT, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def _fail_name(tree: ast.Module) -> str | None:
    """判据收集器往哪个模块级名字里 append（`FAILS` / `FAILED`），现取。

    判据收集器**不都叫 `check`**（`test_zako_role.py` 那个叫 `check_`），所以按**前缀**认；
    认名字不认字面 —— 名字是习惯、append 目标才是契约。没有收集器的是形态 A′
    （`def main() -> int` 自己攒 `bad`、返回退出码），那种套件第 ③ 层不适用、报未评估。

    工装**自己抄一份、不 import codemod 的**：它是独立判官，共用一份实现的话，codemod 把
    这条规则改错会同时改掉尺子（同族教训见仓里「收窄判据会架空测试尺子」那条）。
    """
    found: set[str] = set()
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name.startswith("check"):
            for sub in ast.walk(n):
                if (isinstance(sub, ast.Attribute) and sub.attr == "append"
                        and isinstance(sub.value, ast.Name)):
                    found.add(sub.value.id)
    return found.pop() if len(found) == 1 else None


def _check_calls(src: str) -> list[str]:
    """模块里每个判据调用的第一个实参（常量才有值，非常量记 `?`）—— 判据指纹。

    **比的是「集合 + 重数」，不比顺序** —— 迁移的**全部内容**就是搬位置，所以判据的
    相对顺序必然变（模块级的判据进了函数尾，助手里的还在助手身上），而"哪几条判据、各几条"
    必须逐字不变。两把错的尺子都试过：

      · `ast.walk` 的产出顺序 —— 它是**广度优先**，判据从模块级搬进函数后嵌套深度
        1→2，遍历序整体错位，`test_admin_write` 报了 123 处假红；
      · 按源码位置排序 —— 好一点，但模块级判据挪到文件尾之后**仍然会变序**。

    收集器**不都叫 `check`**（`test_zako_role.py` 那个叫 `check_`），按前缀认。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ["<PARSE_FAIL>"]
    out: list[str] = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id.startswith("check")):
            continue
        first = n.args[0] if n.args else None
        out.append(repr(first.value) if isinstance(first, ast.Constant) else "?")
    return sorted(out)


def _target_def(src: str, stem: str) -> ast.FunctionDef | None:
    """迁移后的用例函数。**名字就是词干本身**（`test_write_facts.py` → `def test_write_facts()`），
    不是 `test_` + 词干 —— PR #38 的 10 个试点逐字如此，写反了这一层会全红。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    return next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == stem), None)


def _has_top_level_test(src: str) -> bool:
    """顶层有没有**任何** `def test_*` —— 「pytest 收得到吗」的真正判据。

    上面那条「函数名 == 词干」是 **codemod 的产物**（它转写的每个文件都长这样），不是
    pytest 的要求。手写的原生 pytest 文件（`test_process_isolation.py`：四个各管一事的
    `def test_*`，没有一个是词干名）会被那条规则误判成"收集不到" ⇒ **假红**。所以
    "收集不到"这条只在**一个 `def test_*` 都没有**时才成立。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    return any(isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
               for n in tree.body)


def check_one(path: pathlib.Path, skip_layer2: bool, base: str) -> tuple[list[str], list[str]]:
    """返回 `(红, 未评估)` 两份清单（都空 = 全过）。

    **红与未评估分开**是仓里的既有约定（[[退出码 3]]）：前提不成立时判据**一次都没跑**，
    报成"绿"是假的、报成"红"是误伤。未评估一律说出来、并让退出码退 3。
    """
    bad: list[str] = []
    soft: list[str] = []
    stem = path.stem
    new_src = path.read_text(encoding="utf-8")

    # ── ① AST 层 ──────────────────────────────────────────────────────────
    old_src = _old_text(path, base)
    if old_src is None:
        soft.append(f"① 旧文本取不到（{base} 里没这个文件）—— 这一层没评估")
    else:
        a, b = _check_calls(old_src), _check_calls(new_src)
        if len(a) != len(b):
            bad.append(f"① check() 条数变了：旧 {len(a)} → 新 {len(b)}")
        else:
            diff = [f"{i}: {x!r}→{y!r}" for i, (x, y) in enumerate(zip(a, b)) if x != y]
            if diff:
                bad.append(f"① check() 取值点变了 {len(diff)} 处：{diff[:3]}")

    # 新文件必须真的可被 pytest 收集到（否则"迁移"是空转）。
    # **但"没迁移"和"迁移错了"是两件事**：codemod 认不出的形状一律不动（MANUAL 清单），
    # 那些文件在盘上就是原样 —— 报红是误伤，报绿是假绿。判据：文本与旧文本逐字相同
    # ⇒ 这一条根本没动过 ⇒ 未评估（退 3），不进红名单。
    if _target_def(new_src, stem) is None:
        if old_src is not None and new_src == old_src:
            soft.append("① 这个文件**没有被迁移**（codemod 判了 MANUAL）—— 三层都没评估")
            return bad, soft
        if not _has_top_level_test(new_src):
            bad.append(f"① 新文件里没有模块级 `def {stem}()` —— pytest 收集不到")

    if skip_layer2:
        return bad, soft

    # ── ② 行为层 ──────────────────────────────────────────────────────────
    old_rc = None if old_src is None else _run_source(old_src, path.resolve())

    new_rc = _run(path)
    new_pt = _pytest_outcome(path)
    if old_rc is not None and old_rc != new_rc:
        bad.append(f"② 直跑退出码变了：旧 {old_rc} → 新 {new_rc}")
    if new_pt == "skip":
        soft.append(f"② pytest 轨全 skip（直跑 rc={new_rc}）—— 等价性这一层没评估")
    elif (new_rc == 0) != (new_pt == "pass"):
        bad.append(f"② 两轨不一致：直跑 rc={new_rc}，pytest {new_pt}")

    # ── ③ 反控层 ──────────────────────────────────────────────────────────
    fn = _target_def(new_src, stem)
    if fn is None:
        return bad, soft
    # 变异体**直接往收集器里塞一条**，不走 `check(...)`。两把错的尺子都试过：
    #   · `check(MUTANT, False)` —— `test_zako_role` 里 `check` 是 **`agent.authz.check`**
    #     （鉴权函数，`check(principal, tool)`），压根不是判据收集器（那个叫 `check_`）：
    #     注入的调用跑去问鉴权，不报错也不记账 ⇒ 反控"通过"而判据一次没跑。
    #   · 同一个形状在 `test_widget_pin_drift` 上栽在**实参顺序**：那里的收集器签名是
    #     `check(cond, name, extra="")`，`MUTANT` 落在 `cond` 位上恒真 ⇒ 同样假通过。
    # 直接 `FAILS.append(...)` 与收集器的名字、签名、实参顺序全都无关。
    fail_name = _fail_name(ast.parse(new_src))
    if fail_name is None:
        soft.append("③ 反控未评估：这个文件没有判据收集器（形态 A′），注入点不存在")
        return bad, soft
    lines = new_src.splitlines()
    at = fn.body[0].lineno            # 函数体第一行之前插入
    mutated = "\n".join(
        lines[:at - 1] + [f"    {fail_name}.append({MUTANT!r})"] + lines[at - 1:]) + "\n"
    if not mutated.strip():
        return bad, soft
    backup = new_src
    try:
        path.write_text(mutated, encoding="utf-8")
        m_rc = _run(path)
        m_pt = _pytest_outcome(path)
        if m_pt == "skip":
            # pytest 轨 skip ⇒ **前提不在场**（要活 DSN / 要父仓的那一族），判据一次都没跑。
            # 这时"直跑仍绿"不是"判据没被执行"，而是"根本没到判据"—— 报红是误伤。
            # 两个条件必须一起看：只看直跑绿会把 `test_pg_retention_live` 这类
            # 前提缺位的套件判成缺陷；只看 pytest skip 又会把"两轨都真空"放过去。
            soft.append("③ 反控未评估：注入必假判据后 pytest 轨 skip —— 前提不在场，判据没跑")
        else:
            if m_rc == 0:
                bad.append("③ 反控：注入必假判据后直跑仍绿 —— 判据没被执行")
            if m_pt == "pass":
                bad.append("③ 反控：注入必假判据后 pytest 仍绿 —— 判据没被执行")
    finally:
        path.write_text(backup, encoding="utf-8")
    return bad, soft


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", type=pathlib.Path)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--skip-layer2", action="store_true", help="只跑文本层（快，但不算过）")
    ap.add_argument("--base", default="HEAD",
                    help="旧文本取自哪个 ref（默认 HEAD）。**必须在提交前跑** —— "
                         "提交后 HEAD 就是新文本，①② 两层退化成自比、恒过。")
    ap.add_argument("--json", type=pathlib.Path, help="把结果写成 JSON")
    args = ap.parse_args()

    paths = sorted(TESTS.glob("test_*.py")) if args.all else args.paths
    if not paths:
        ap.error("给路径，或 --all")

    # 第 ③ 层是**就地改写再还原**，`finally` 兜不住 SIGKILL —— 中途被杀会把注入过变异体
    # 的文件留在盘上（实测踩到一次）。进来先自检：认出来就**拒绝跑**，而不是拿一份被改过
    # 的文件当基线接着跑（那种结果看着像"全绿"，其实测的是变异体）。
    dirty = [p for p in paths if MUTANT in p.read_text(encoding="utf-8")]
    if dirty:
        print("✗ 这些文件里还留着上一次注入的变异体（工装被杀过？）：")
        for p in dirty:
            print(f"    {p.relative_to(ROOT)}")
        print("  先 `git checkout -- tests/` 还原，再重跑。")
        return 2

    results: dict[str, dict[str, list[str]]] = {}
    for p in paths:
        if p.name.startswith("_") or p.name == "conftest.py":
            continue
        try:
            bad, soft = check_one(p, args.skip_layer2, args.base)
        except Exception as exc:                      # noqa: BLE001 —— 工装自己，报出来即可
            bad, soft = [f"工装异常：{type(exc).__name__}: {exc}"], []
        results[p.name] = {"red": bad, "unevaluated": soft}
        if bad:
            print(f"✗ {p.name}", flush=True)
            for b in bad:
                print(f"    {b}", flush=True)
        elif soft:
            print(f"◐ {p.name}（未评估）", flush=True)
            for s in soft:
                print(f"    {s}", flush=True)
        else:
            print(f"✓ {p.name}", flush=True)

    red = [k for k, v in results.items() if v["red"]]
    unev = [k for k, v in results.items() if not v["red"] and v["unevaluated"]]
    print(f"\n{len(results) - len(red) - len(unev)}/{len(results)} 三层全过；"
          f"红 {len(red)} 个；未评估 {len(unev)} 个")
    if args.json:
        args.json.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    if red:
        return 1
    return 3 if unev else 0


if __name__ == "__main__":
    sys.exit(main())
