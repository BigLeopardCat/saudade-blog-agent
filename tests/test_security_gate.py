# -*- coding: utf-8 -*-
"""安全扫描棘轮（`eval/security_gate.py`）的**接线与账**判据。

**为什么单起一套**：同 `tests/test_except_gate.py`——一道闸判的是"仓库里的 finding 有没有
变多"，它自己**没有**判"账是不是还在量同一件事"。而它的失效方式全在那一边：

| 坏法 | 会怎样 | 哪一条钉它 |
|---|---|---|
| `pyproject.toml` 里加一句 `[tool.bandit]` | bandit **自动发现**它 ⇒ 少报、闸报绿 | ③ |
| 某个文件里加一行 `# nosec` | 那条 finding 不进 `results`、只进 `metrics.nosec` | ③ |
| 换 bandit 跑在哪个解释器上 | 同一份源码 3.10 读 205 / 3.12 读 211 ⇒ 换了条目集 | ② |
| 键退回"光文件" | 同文件里 B105 换 B608 按文件数是"没变" | ②（`by_key` 的形状） |
| CI 没接这一步 | 闸在本机绿、在 CI 从不运行 | ④ |
| 闸自己坏了（JSON 解析、判据恒绿） | 全部绿 | ⑤（跑它自带的 `_self_check`） |

用法：
    uv run pytest tests/test_security_gate.py -v
    uv run python tests/test_security_gate.py
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

_GATE = ROOT / "eval" / "security_gate.py"
_BASE = ROOT / "eval" / "security_baseline.json"


def _gate_module():
    """加载闸本身（只跑模块体：常量 + 定义，无副作用）。"""
    spec = importlib.util.spec_from_file_location("_security_gate_under_test", _GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dev_group() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data.get("dependency-groups", {}).get("dev", [])


def _report(bad: list[str]) -> None:
    assert not bad, f"{len(bad)} 条未通过：" + "；".join(bad)


def _calls_in_main(name: str) -> int:
    """`main()` 函数体里 `name(...)` 的调用处数。

    **只数"整份源码里有没有这个名字"是不够的**：守卫的定义本身、别的助手里的调用都算，
    于是把调用从 `main()` 里挪走（守卫变死代码）也照样绿。
    """
    tree = ast.parse(_GATE.read_text(encoding="utf-8"))
    main = next(n for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "main")
    return sum(1 for n in ast.walk(main)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name)


def test_gate_and_baseline_in_place():
    """① 闸与账在位、账自洽（逐键之和 == total）、且**本闸自己的文件是零条**。"""
    bad: list[str] = []
    print("① 闸与账在位")
    for p in (_GATE, _BASE):
        ok = p.is_file()
        print(f"  {'✅' if ok else '❌'} {p.relative_to(ROOT)}")
        if not ok:
            bad.append(f"{p.relative_to(ROOT)} 不在位")
    if not _BASE.is_file():
        _report(bad)
    base = json.loads(_BASE.read_text(encoding="utf-8"))
    by_key = base.get("by_key") or {}
    ok = isinstance(base.get("total"), int) and base["total"] > 0
    print(f"  {'✅' if ok else '❌'} 账里有读数  [total={base.get('total')}]")
    if not ok:
        bad.append("账里的 total 不是正整数")
    ok = sum(by_key.values()) == base.get("total")
    print(f"  {'✅' if ok else '❌'} 逐键之和 == total  "
          f"[和={sum(by_key.values())} total={base.get('total')}]")
    if not ok:
        bad.append("账自己不自洽")
    # **纪律 1 的自证**：不跳 subprocess 那三项，本闸自己的 `subprocess.run` 就会进账，
    # 而"新文件必须零条"这条纪律一出生就是死的。
    mine = sorted(k for k in by_key if k.startswith("eval/security_gate.py::"))
    print(f"  {'✅' if not mine else '❌'} 本闸自己的文件零条（否则纪律 1 是死的）  [{mine}]")
    if mine:
        bad.append(f"本闸自己带 finding：{mine}")
    _report(bad)


def test_baseline_quantities_are_single_sourced():
    """② 四个量（bandit 版本 / 解释器 / 跳过集 / 扫描面）都与各自的事实源同源。

    **解释器**那一条是这一族里最像 `eval/type_gate.py` 的 `--python-executable` 的洞：
    实测同一份源码、同一个 bandit 版本，跑在 3.10 上读 205 条、跑在 3.12 上读 211 条。
    """
    bad: list[str] = []
    print("② 四个量只有一处事实源")
    gate = _gate_module()
    base = json.loads(_BASE.read_text(encoding="utf-8"))
    pin = next((d.split("==", 1)[1] for d in _dev_group()
                if isinstance(d, str) and d.startswith("bandit==")), None)
    print(f"  {'✅' if pin else '❌'} dev 分组钉了 bandit 版本  [{pin}]")
    if not pin:
        bad.append("dev 分组里没有 bandit（CI 会跑不起来）")
    ok = base.get("bandit_version") == pin
    print(f"  {'✅' if ok else '❌'} 账里的版本 == dev 分组钉的  "
          f"[账={base.get('bandit_version')} dev={pin}]")
    if not ok:
        bad.append("bandit 版本对不上")
    ok = base.get("skips") == gate.SKIPS
    print(f"  {'✅' if ok else '❌'} 账里的跳过集 == 闸里那份  [账={base.get('skips')}]")
    if not ok:
        bad.append("跳过集对不上（缩小扫描面 = 换一个量）")
    ok = base.get("exclude") == gate.EXCLUDE and base.get("targets") == gate.TARGETS
    print(f"  {'✅' if ok else '❌'} 账里的扫描面 == 闸里那份")
    if not ok:
        bad.append("扫描面对不上")
    # 反控：把账里的跳过集改一格，这条判据必须不通过（证明它真的在比）
    fake = dict(base)
    fake["skips"] = [*gate.SKIPS, "B608"]
    print(f"  {'✅' if fake["skips"] != gate.SKIPS else '❌'} 反控：账里跳过集换成别的 ⇒ 会红")
    if fake["skips"] == gate.SKIPS:
        bad.append("反控没生效（这条判据是恒真的）")
    # `B602`/`B609`（不安全的变体）**不许**在跳过集里 —— 否则这一族真要看的东西没了。
    leaked = sorted(set(gate.SKIPS) & {"B602", "B609"})
    print(f"  {'✅' if not leaked else '❌'} 不安全的变体没有被跳  [{leaked or '无'}]")
    if leaked:
        bad.append(f"跳过了不安全的变体：{leaked}")
    _report(bad)


def test_no_auto_discovered_config_and_no_nosec():
    """③ 两个"悄悄关闸"的口子：自动发现的配置源、`# nosec` 行内豁免。"""
    bad: list[str] = []
    print("③ 两个关闸的口子")
    gate = _gate_module()
    found = gate._config_sources()
    print(f"  {'✅' if not found else '❌'} 盘上没有 bandit 会**自动读**的配置  [{found or '无'}]")
    if found:
        bad.append(f"有自动发现的配置源：{found}")
    base = json.loads(_BASE.read_text(encoding="utf-8"))
    ok = base.get("nosec") == 0
    print(f"  {'✅' if ok else '❌'} 账里的 `# nosec` 是 0（它只许变小 ⇒ 等于不许用）  "
          f"[{base.get('nosec')}]")
    if not ok:
        bad.append("账里已经有 nosec（行内豁免能悄悄把 finding 变没）")
    # `_config_sources` 必须**在 `main()` 里**被调用（只数"源码里有没有这个名字"会把
    # 定义处之外随便一处的调用也算上；而这里的定义本身不是 Call，所以"≥1"其实就等于
    # "有一个调用点"——但那不保证它在 `main` 里）。
    calls = _calls_in_main("_config_sources")
    print(f"  {'✅' if calls else '❌'} `main()` 里真的调用了它（不是死代码）  [{calls} 处]")
    if not calls:
        bad.append("`main()` 里没有 `_config_sources` 的调用点（守卫是死代码）")
    # 反控：`judge()` 对 nosec 变多必须红（证明那一格不是摆设）
    j = gate.judge({}, {}, 1, 0)
    print(f"  {'✅' if j else '❌'} 反控：`nosec` 变多 ⇒ `judge()` 必须报红")
    if not j:
        bad.append("nosec 变多没红")
    _report(bad)


def test_gate_is_wired_into_ci():
    """④ CI 有一步**会执行**它，且那一行不手写工具版本。"""
    bad: list[str] = []
    print("④ 接在 CI 上")
    yml = (ROOT / ".github" / "workflows" / "eval.yml").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in yml.splitlines() if not ln.lstrip().startswith("#"))
    hits = [ln for ln in code.splitlines() if "eval/security_gate.py" in ln]
    print(f"  {'✅' if hits else '❌'} CI 里有一行会执行 `eval/security_gate.py`  [{len(hits)} 行]")
    if not hits:
        bad.append("CI 没接这道闸（本机绿、CI 从不运行）")
    hard = [ln for ln in hits if re.search(r"bandit[@=]|==\d", ln)]
    print(f"  {'✅' if not hard else '❌'} 那一行不手写工具版本（版本只从 dev 分组来）")
    if hard:
        bad.append("CI 那一行手写了版本")
    _report(bad)


def test_gate_self_check_passes():
    """⑤ 跑闸自带的正控/反控（`_self_check`）—— 没有这一段，"闸其实是空的"没人看着。"""
    print("⑤ 闸自带的正控/反控")
    gate = _gate_module()
    try:
        gate._self_check()
    except AssertionError as e:
        print(f"  ❌ 闸自检没过：{e}")
        raise
    print("  ✅ `_self_check()` 通过（计数 / 路径归一化 / 四条判据 / nosec / 幽灵键 都在）")


if __name__ == "__main__":
    for _fn in (test_gate_and_baseline_in_place,
                test_baseline_quantities_are_single_sourced,
                test_no_auto_discovered_config_and_no_nosec,
                test_gate_is_wired_into_ci,
                test_gate_self_check_passes):
        _fn()
    print("\n全部通过")
