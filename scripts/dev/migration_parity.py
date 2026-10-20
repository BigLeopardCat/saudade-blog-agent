# -*- coding: utf-8 -*-
"""迁移对拍工装（20261020 批 1）：**唯一放行口**。

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

旧文件落到 `tests/_mig_old_<stem>.py` 再删 —— 以 `_` 开头，`run_all.suites()` 与
`test_tests_dir_shape` ③ 都不收它，跑完立刻删。

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

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
TESTS = ROOT / "tests"
MUTANT = "__MIGRATION_PARITY_MUTANT__"


def _pinned() -> dict[str, str]:
    """从 `run_all.py` 现取出厂档钉子（ast 取字面量，**不 import** —— 见 PR #38 同款注）。"""
    src = (TESTS / "run_all.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_PINNED" for t in n.targets):
            return ast.literal_eval(n.value)
    raise SystemExit("run_all.py 里找不到 _PINNED")


_PINNED = _pinned()


def _child_env() -> dict[str, str]:
    """两条轨跑在**同一个 import 视野**下（`run_all.py` 的钉子 + `tests/` + 仓根）。

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


def _check_calls(src: str) -> list[str]:
    """模块里每个 `check(...)` 的第一个实参（常量才有值，非常量记 `?`）—— 判据指纹。"""
    out: list[str] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ["<PARSE_FAIL>"]
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "check"):
            first = n.args[0] if n.args else None
            if isinstance(first, ast.Constant):
                out.append(repr(first.value))
            else:
                out.append("?")
    return out


def _target_def(src: str, stem: str) -> ast.FunctionDef | None:
    """迁移后的用例函数。**名字就是词干本身**（`test_write_facts.py` → `def test_write_facts()`），
    不是 `test_` + 词干 —— PR #38 的 10 个试点逐字如此，写反了这一层会全红。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    return next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == stem), None)


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

    # 新文件必须真的可被 pytest 收集到（否则"迁移"是空转）
    if _target_def(new_src, stem) is None:
        bad.append(f"① 新文件里没有模块级 `def {stem}()` —— pytest 收集不到")

    if skip_layer2:
        return bad, soft

    # ── ② 行为层 ──────────────────────────────────────────────────────────
    old_rc = None
    if old_src is not None:
        tmp = TESTS / f"_mig_old_{stem}.py"
        tmp.write_text(old_src, encoding="utf-8")
        try:
            old_rc = _run(tmp)
        finally:
            tmp.unlink(missing_ok=True)

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
    lines = new_src.splitlines()
    at = fn.body[0].lineno            # 函数体第一行之前插入
    mutated = "\n".join(
        lines[:at - 1] + [f"    check({MUTANT!r}, False)"] + lines[at - 1:]) + "\n"
    if not mutated.strip():
        return bad, soft
    backup = new_src
    try:
        path.write_text(mutated, encoding="utf-8")
        m_rc = _run(path)
        m_pt = _pytest_outcome(path)
        if m_rc == 0:
            bad.append("③ 反控：注入必假判据后直跑仍绿 —— 判据没被执行")
        if m_pt == "skip":
            soft.append("③ 反控未评估：注入必假判据后 pytest 轨 skip（不是绿、也不是红）")
        elif m_pt == "pass":
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

    results: dict[str, dict[str, list[str]]] = {}
    for p in paths:
        if p.name.startswith("_") or p.name == "run_all.py":
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
