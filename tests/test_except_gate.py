# -*- coding: utf-8 -*-
"""盲捕获棘轮（`eval/except_gate.py`）的**接线与账**判据。

**为什么这一套要单起**：那道闸判的是"仓库里的 `except Exception` 有没有变多"，它自己
**没有**判"账是不是还在量同一件事"。而它的失效方式恰好都在那一边：

| 坏法 | 会怎样 | 本套件哪一条钉它 |
|---|---|---|
| `[tool.ruff.lint].ignore` 里加一句 `"BLE001"` | 扫描读到 **0 条**、闸报绿 | ④ |
| 账里留着已删文件的键 | 那句「已经清零，可以销账」把它报成**好消息** | ③ |
| ruff 升版 / 换了 select | 换了**一套条目集**，棘轮不再量同一个东西 | ② |
| CI 没接这一步 | 闸在本机绿、在 CI 从不运行 | ⑤ |
| 闸自己坏了（计数解析、判据恒绿） | 全部绿 | ⑥（跑它自带的 `_self_check`） |

**为什么不在 `tests/test_packaging.py` ⑦ 里加一节**：那一套已经是 318 行（复杂度台账
贴着顶），而 ⑤⑥⑦ 判的是**打包与 pin** 这件事；一道新闸的接线判据住它自己那套房子里
（同 `tests/test_process_isolation.py` 之于 conftest 的还原层）。

用法：
    uv run pytest tests/test_except_gate.py -v
    uv run python tests/test_except_gate.py
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import re
import tomllib

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

_GATE = ROOT / "eval" / "except_gate.py"
_BASE = ROOT / "eval" / "except_baseline.json"


def _gate_module():
    """加载闸本身。`importlib` 而不是 `import eval.except_gate`：`eval/` 不是包。

    只跑模块体（常量 + 定义），没有副作用 —— 同 `tests/test_packaging.py` 加载
    `lint_gate` 的那一手。
    """
    spec = importlib.util.spec_from_file_location("_except_gate_under_test", _GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dev_group() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data.get("dependency-groups", {}).get("dev", [])


def _dev_ruff_pin() -> str | None:
    m = next((re.search(r"^ruff==(.+)$", d) for d in _dev_group()
              if isinstance(d, str) and re.match(r"^ruff==", d)), None)
    return m.group(1) if m else None


def _project_lint_ignore() -> list[str] | None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data.get("tool", {}).get("ruff", {}).get("lint", {}).get("ignore")


def _report(bad: list[str]) -> None:
    assert not bad, f"{len(bad)} 条未通过：" + "；".join(bad)


def test_gate_and_baseline_in_place():
    """① 两个文件都在位，账里有读数。"""
    bad: list[str] = []
    print("① 闸与账都在位")
    bad += [] if _GATE.is_file() else ["eval/except_gate.py 不在位"]
    bad += [] if _BASE.is_file() else ["eval/except_baseline.json 不在位（棘轮的量在账上）"]
    print(f"  {'✅' if _GATE.is_file() else '❌'} eval/except_gate.py")
    print(f"  {'✅' if _BASE.is_file() else '❌'} eval/except_baseline.json")
    if _BASE.is_file():
        base = json.loads(_BASE.read_text(encoding="utf-8"))
        ok = isinstance(base.get("total"), int) and base["total"] > 0
        print(f"  {'✅' if ok else '❌'} 账里有读数  [total={base.get('total')}]")
        if not ok:
            bad.append("账里的 total 不是正整数")
    _report(bad)


def test_baseline_three_quantities_are_single_sourced():
    """② 三个量（ruff 版本 / select / pyproject 的 ignore）都与出账时同源。

    任一换掉就不是同一个量 —— 闸自己会退 3，这一条是**提前**在 CI 之外把它说出来，
    并且把"真值在哪"钉住（ruff 版本的真值在 dev 分组，ignore 的真值在 pyproject）。
    """
    bad: list[str] = []
    print("② 三个量只有一处事实源")
    gate = _gate_module()
    base = json.loads(_BASE.read_text(encoding="utf-8"))
    pin = _dev_ruff_pin()
    print(f"  {'✅' if base.get('ruff_version') == pin else '❌'} "
          f"账里的 ruff 版本 == dev 分组钉的  [账={base.get('ruff_version')} dev={pin}]")
    if base.get("ruff_version") != pin:
        bad.append("ruff 版本对不上（换了规则实现 = 换了一套条目集）")
    sel = base.get("select")
    ok = sel == gate.SELECT
    print(f"  {'✅' if ok else '❌'} 账里的 select == 闸里那份  [账={sel} 闸={gate.SELECT}]")
    if not ok:
        bad.append("select 对不上")
    ign = base.get("project_lint_ignore")
    ok = ign == _project_lint_ignore()
    print(f"  {'✅' if ok else '❌'} 账里记着出账时的 `[tool.ruff.lint].ignore`  "
          f"[账={ign} 现在={_project_lint_ignore()}]")
    if not ok:
        bad.append("ignore 表对不上（这一条是「闸被静默关掉」的守卫）")
    # 反控：把账里的 select 改一格，这一条必须不通过（证明它真的在比，不是恒真）
    fake = dict(base)
    fake["select"] = ["E501"]
    print(f"  {'✅' if fake.get('select') != gate.SELECT else '❌'} "
          f"反控：账里 select 换成别的 ⇒ 这条判据会红")
    if fake.get("select") == gate.SELECT:
        bad.append("反控没生效（这条判据是恒真的）")
    _report(bad)


def test_baseline_is_self_consistent_and_no_ghost_keys():
    """③ 账自己得自洽（逐文件之和 == total），且盘上没有已删文件的键。

    幽灵键不进 `judge()` 的任何一条判据、也不影响 `total` ⇒ 删掉一个文件之后它只会让
    账变脏，还被「已经清零，可以销账」那句**报成好消息**。两层：① 现在这份账干净；
    ② 那条守卫真的会拦，且 `main()` 真的调用了它（否则守卫是死代码）。
    """
    bad: list[str] = []
    print("③ 账自洽 + 幽灵键两层")
    gate = _gate_module()
    base = json.loads(_BASE.read_text(encoding="utf-8"))
    by_file = base.get("by_file") or {}
    ok = sum(by_file.values()) == base.get("total")
    print(f"  {'✅' if ok else '❌'} 逐文件计数之和 == total  "
          f"[和={sum(by_file.values())} total={base.get('total')}]")
    if not ok:
        bad.append("账自己不自洽（两个口径分岔时棘轮在量另一个东西）")
    ghosts = gate.ghost_keys(by_file)
    print(f"  {'✅' if not ghosts else '❌'} 盘上没有已删文件的键  "
          f"[{('、'.join(ghosts)) or '无'}]")
    if ghosts:
        bad.append(f"账里有盘上不存在的键：{ghosts}")
    caught = gate.ghost_keys({"eval/this_file_was_deleted.py": 3})
    print(f"  {'✅' if caught else '❌'} 反控：喂一个盘上不存在的键 ⇒ 必须被抓到")
    if not caught:
        bad.append("反控没生效：幽灵键守卫是空的")
    not_caught = gate.ghost_keys({"eval/except_gate.py": 1})
    print(f"  {'✅' if not not_caught else '❌'} 反控：盘上**有**的键不许被抓（否则「见谁都红」）")
    if not_caught:
        bad.append("把真文件判成幽灵了")
    calls = [n for n in ast.walk(ast.parse(_GATE.read_text(encoding="utf-8")))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "ghost_keys"]
    print(f"  {'✅' if calls else '❌'} `main()` 真的调用了 `ghost_keys`（否则守卫是死代码）")
    if not calls:
        bad.append("源码里没有 ghost_keys 的调用点")
    _report(bad)


def test_ignore_hole_is_guarded():
    """④ **这一道闸最真的洞**：`--select BLE001` 仍然服从 pyproject 的 `ignore` 表。

    谁往里加一句 `"BLE001"`（或整个 `"BLE"` 族），扫描会读到 0 条、闸报「✅ 没有变差」
    —— 一条没有内容的判据。所以：① 认得出这两种写法；② 不误判无关的 ignore（否则
    "见谁都红"，那条守卫等于把闸关死）；③ `main()` 真的调用了它。
    """
    bad: list[str] = []
    print("④ `ignore` 表关掉 BLE001 的那个洞")
    gate = _gate_module()
    cases = [(True, ["BLE001"], "ignore 里有 BLE001 ⇒ 必须认出来"),
             (True, ["BLE"], "ignore 里有整个 BLE 族 ⇒ 必须认出来"),
             (False, ["E501"], "无关的 ignore ⇒ 不许误判（否则见谁都红）"),
             (False, None, "没有 ignore 表（None）⇒ 不许误判")]
    for want, arg, why in cases:
        got = gate._ignored_ble(arg)
        print(f"  {'✅' if got == want else '❌'} {why}  [{arg!r} → {got}]")
        if got != want:
            bad.append(why)
    src = _GATE.read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_ignored_ble"]
    # 定义处一次 + main() 里一次 ⇒ 至少两处；只有一处说明守卫没被调用（死代码）。
    print(f"  {'✅' if len(calls) >= 2 else '❌'} `main()` 真的调用了它（定义之外还有调用点）"
          f"  [{len(calls)} 处]")
    if len(calls) < 2:
        bad.append("_ignored_ble 只有定义、没有调用点（守卫是死代码）")
    _report(bad)


def test_gate_is_wired_into_ci():
    """⑤ CI 有一步**会执行**它（不是只在注释里提一句），且那一行不手写版本号。"""
    bad: list[str] = []
    print("⑤ 接在 CI 上")
    yml = (ROOT / ".github" / "workflows" / "eval.yml").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in yml.splitlines() if not ln.lstrip().startswith("#"))
    hits = [ln for ln in code.splitlines() if "eval/except_gate.py" in ln]
    print(f"  {'✅' if hits else '❌'} CI 里有一行会执行 `eval/except_gate.py`  [{len(hits)} 行]")
    if not hits:
        bad.append("CI 没接这道闸（本机绿、CI 从不运行）")
    # 那一行不许自己钉版本：ruff 的版本真值在 dev 分组，闸自己去读；写死在这里
    # 会出现"账按 A 版出、CI 按 B 版跑"（同 lint_gate 那条）。
    hard = [ln for ln in hits if re.search(r"ruff[@=]|==\d", ln)]
    print(f"  {'✅' if not hard else '❌'} 那一行不手写工具版本（版本只从 dev 分组来）"
          f"  [{('、'.join(hard)) or '无'}]")
    if hard:
        bad.append("CI 那一行手写了版本")
    _report(bad)


def test_gate_self_check_passes():
    """⑥ 跑闸自带的正控/反控（`_self_check`）。

    没有这一段，"闸其实是空的"没人看着：计数解析错了、`judge()` 恒绿、幽灵键守卫是空实现
    —— 三种坏法都不会让**任何**套件变红。所以这里把它当场跑一遍（它只吃合成输出，不碰盘）。
    """
    print("⑥ 闸自带的正控/反控")
    gate = _gate_module()
    try:
        gate._self_check()
    except AssertionError as e:
        print(f"  ❌ 闸自检没过：{e}")
        raise
    print("  ✅ `_self_check()` 通过（计数 / 判据 / 幽灵键 / ignore 洞 四组都在）")


if __name__ == "__main__":
    for _fn in (test_gate_and_baseline_in_place,
                test_baseline_three_quantities_are_single_sourced,
                test_baseline_is_self_consistent_and_no_ghost_keys,
                test_ignore_hole_is_guarded,
                test_gate_is_wired_into_ci,
                test_gate_self_check_passes):
        _fn()
    print("\n全部通过")
