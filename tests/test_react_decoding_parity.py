# -*- coding: utf-8 -*-
"""两臂的**解码参数同源锁**（20261019）：react 臂的决策 LLM 必须与生产 planner 读同一组设置。

## 为什么这条测试存在

`agent/react_arm.py` 的 `_llm()` docstring 一直写着"与生产 planner 同一组解码参数"，
但**温度那一格是硬写的常数**，而生产 planner 读的是 `settings.planner_temperature`
（今天 0.0）。于是两臂的 A/B 一直在比**解码参数**，而不是在比"循环形状"——同一个坑
20261004 已经踩过一次（那次是整组裸 `get_llm()`、thinking 开着，跑出"17× 成本 /
33% 撞上限"的假读数，当天纠正后 B′ 31/48 对现网 31/48 打平）。

教训是：**注释、docstring、"看起来一样"都不是判据**。写死的常数与读设置的那一行长得
几乎一样，只有把"两臂必须读同一个 settings 属性"写成断言，它才不会再漂回去。

## 锁的六条

  ① **形状**（AST）：`_llm` 里那一次 `get_llm(...)` 的每个关键字实参都必须是
     `settings.<name>` 形式——**零字面量**。只查某个字面量文本是不够的：把常数从
     0.2 改成 0.7 照样过；
  ② **同源**（跨文件 AST）：把 `agent/graph.py` 里**生产 planner** 那一处 `get_llm(...)`
     认出来（用 `planner_native_max_tokens` 这个只属于它的关键字），两边的
     关键字名 → settings 属性名**逐格比对**。这一层才是"同一组"的本体——① 只说得上
     "react 读了设置"，两臂各读一组**不同**的设置同样能过 ①；
  ③ **运行时**：把生产那四格 settings 各设一个哨兵值，断言 `_llm()` 原样把四个都传下去
     （**期望值从 ② 那份生产实参表派生**——生产哪天加第五个旋钮，这里当场红）；
  ④ **narrator 那份同样同源**：`_narrator_llm()` 对生产 `model_node` 的那一处；
  ⑤ **反控 · AST**：一段硬写字面量的假源码必须被判红（证明 ① 不是恒绿）；
  ⑥ **反控 · 运行时**：一个硬写温度的 `_llm` 替身必须让 ③ 塌掉（证明 ③ 不是恒绿）。

无网络 / 无 LLM：`agent/react_arm.get_llm` 是模块级名字，直接换成记录实参的替身。
用法：.venv/bin/python tests/test_react_decoding_parity.py
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

import agent.react_arm as R  # noqa: E402
from config.settings import settings  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc + (f"  [{detail}]" if detail else ""))


# ── AST 侧的两个小工具 ────────────────────────────────────────────────────────
def _classify(v: ast.expr) -> tuple[str, str]:
    """实参表达式 → `("setting", 属性名)` / `("literal", repr)` / `("other", 源码)`。"""
    if (isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name)
            and v.value.id == "settings"):
        return ("setting", v.attr)
    try:
        return ("literal", repr(ast.literal_eval(v)))
    except Exception:
        return ("other", ast.unparse(v))


def _iter_get_llm(node: ast.AST):
    """本函数体里的 `get_llm(...)` 调用，**不下钻嵌套 def/lambda**。

    不下钻是刻意的：`planner_node` 里将来若包一个局部工厂，它属于另一件事，
    混进来会让"生产 planner 读哪四格"这件事变得说不清。
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if (isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                and child.func.id == "get_llm"):
            yield child
        yield from _iter_get_llm(child)


def _calls_by_func(path: Path) -> dict[str, list[dict[str, tuple[str, str]]]]:
    """`{顶层函数名: [ {关键字名: 分类结果}, ... ]}`（只收调过 `get_llm` 的函数）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, list[dict[str, tuple[str, str]]]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [{kw.arg: _classify(kw.value) for kw in c.keywords}
                 for c in _iter_get_llm(node)]
        if calls:
            out[node.name] = calls
    return out


def _setting_map(kwargs: dict[str, tuple[str, str]]) -> dict[str, str] | None:
    """全是 `settings.<name>` 就返回 `{关键字名: 属性名}`，否则 None。"""
    out: dict[str, str] = {}
    for kw, (kind, val) in kwargs.items():
        if kind != "setting":
            return None
        out[kw] = val
    return out


def _solo(calls: list[dict[str, tuple[str, str]]], where: str) -> dict[str, tuple[str, str]]:
    check(f"{where} 恰好一处 `get_llm(...)`", len(calls) == 1, f"{len(calls)} 处")
    return calls[0] if calls else {}


def _prod_planner_kwargs(prod_calls: dict) -> dict[str, tuple[str, str]]:
    """生产 planner 那一处 `get_llm(...)` 的实参表。

    认它用的是**只属于它的关键字** `planner_native_max_tokens`，不写函数名也不写行号
    ——函数名会改名（实测那一处住在 `_planner_setup` 里、不叫 `planner_node`）、行号会
    被上面几行注释顶走，两者都不是身份。**恰好一处**是判据的一部分：两处就说明
    planner 的 LLM 客户端有两个产地，而"两臂同源"这句话就无从说起。
    """
    hits = [(fn, c) for fn, cs in prod_calls.items() for c in cs
            if c.get("max_tokens") == ("setting", "planner_native_max_tokens")]
    print("\n① 认出生产 planner 那一处 `get_llm(...)`")
    check("全仓 graph.py 里恰好一处带 `planner_native_max_tokens` 的调用",
          len(hits) == 1, str([fn for fn, _ in hits]))
    return hits[0][1] if len(hits) == 1 else {}


def _check_shape(arm_calls: dict) -> dict[str, tuple[str, str]]:
    """② 形状：`_llm` 的每个关键字实参都是 `settings.<name>`（零字面量）。"""
    print("\n② 形状：`_llm` 的每个关键字实参都是 `settings.<name>`（零字面量）")
    arm = _solo(arm_calls.get("_llm", []), "react `_llm`")
    bad = {kw: v for kw, v in arm.items() if v[0] != "setting"}
    check("没有一格是字面量或别的表达式", not bad, str(bad))
    check("四个旋钮都在（漏一个就是把那一格还给了默认值）",
          set(arm) == {"temperature", "max_tokens", "timeout", "enable_thinking"},
          str(sorted(arm)))
    return arm


def _check_same_source(prod: dict, arm: dict) -> dict[str, str]:
    """③ 同源：与生产 planner 逐格比对（关键字名 → settings 属性名）。"""
    print("\n③ 同源：与生产 planner 逐格比对（关键字名 → settings 属性名）")
    pm, am = _setting_map(prod), _setting_map(arm)
    check("生产那一处也是全 `settings.<name>`", pm is not None, str(prod))
    check("**关键字名集合相同**", pm is not None and am is not None and set(pm) == set(am),
          f"prod={sorted(pm or {})} arm={sorted(am or {})}")
    diff = {k: (pm.get(k), am.get(k)) for k in set(pm or {}) | set(am or {})
            if pm.get(k) != am.get(k)}
    check("**逐格指向同一个 settings 属性**", not diff, str(diff))
    for k in sorted(pm or {}):
        print(f"      {k:<16} ← settings.{pm[k]}")
    return pm or {}


def _spy_llm() -> tuple[object, dict]:
    """记录实参的 `get_llm` 替身 → `(替身, 它会填的那只表)`。"""
    seen: dict[str, object] = {}

    def _spy(**kw):
        seen.clear()
        seen.update(kw)
        return object()

    return _spy, seen


def _check_runtime(pm: dict[str, str]) -> tuple[dict[str, object], dict[str, object]]:
    """④ 运行时：四格 settings 各设哨兵值，`_llm()` 必须原样传下去。"""
    print("\n④ 运行时：四格 settings 各设一个哨兵值，`_llm()` 原样传下去")
    # 期望值**从生产实参表派生**：生产哪天加第五个旋钮，这条当场红。
    sentinels = {"planner_temperature": 0.37, "planner_native_max_tokens": 4321,
                 "planner_native_timeout": 77, "planner_native_thinking": True}
    expect = {kw: sentinels[attr] for kw, attr in pm.items() if attr in sentinels}
    check("四格哨兵都落在了生产的实参表里（映射没漏）", len(expect) == len(pm) == 4,
          f"expect={sorted(expect)}")
    spy, seen = _spy_llm()
    old_settings = {k: getattr(settings, k) for k in sentinels}
    old_get_llm = R.get_llm
    R.get_llm = spy
    try:
        for k, v in sentinels.items():
            setattr(settings, k, v)
        R._llm()
    finally:
        R.get_llm = old_get_llm
        for k, v in old_settings.items():
            setattr(settings, k, v)
    check("`_llm()` 传下去的实参表 == 生产那张表（名字与值都对上）",
          seen == expect, f"seen={seen} expect={expect}")
    return seen, expect


def _check_narrator(prod_calls: dict, arm_calls: dict) -> None:
    """⑤ narrator 那份也同源（`_narrator_llm()` 对生产 `model_node`）。"""
    print("\n⑤ narrator 那份也同源（`_narrator_llm()` 对生产 `model_node`）")
    prod_nar = _solo(prod_calls.get("model_node", []), "生产 `model_node`")
    arm_nar = _solo(arm_calls.get("_narrator_llm", []), "react `_narrator_llm`")
    check("两边关键字名与实参逐格相同", prod_nar == arm_nar,
          f"prod={prod_nar} arm={arm_nar}")


def _check_controls(pm: dict[str, str]) -> None:
    """⑥⑦ 反控：两条判据都必须能红（否则上面那一整套是装饰）。"""
    print("\n⑥ 反控 · AST：一段硬写字面量的源码必须被判红")
    fake = ast.parse(
        "def _llm():\n"
        "    return get_llm(temperature=0.2, max_tokens=settings.planner_native_max_tokens)\n"
    )
    fake_kwargs = [{kw.arg: _classify(kw.value) for kw in c.keywords}
                   for c in _iter_get_llm(fake.body[0])]
    check("那一格被认成字面量（不是 setting）",
          bool(fake_kwargs) and fake_kwargs[0].get("temperature") == ("literal", "0.2"),
          str(fake_kwargs))
    check("⇒ ② 的判据在它身上会红",
          bool(fake_kwargs) and _setting_map(fake_kwargs[0]) is None)

    print("\n⑦ 反控 · 运行时：硬写温度的 `_llm` 必须让 ④ 塌掉")
    sentinels = {"planner_temperature": 0.37, "planner_native_max_tokens": 4321,
                 "planner_native_timeout": 77, "planner_native_thinking": True}
    expect = {kw: sentinels[attr] for kw, attr in pm.items() if attr in sentinels}
    spy, seen = _spy_llm()
    old_settings = {k: getattr(settings, k) for k in sentinels}
    old_llm, old_get_llm = R._llm, R.get_llm
    R.get_llm = spy
    try:
        R._llm = lambda: R.get_llm(temperature=0.2, max_tokens=4321,
                                   timeout=77, enable_thinking=True)
        for k, v in sentinels.items():
            setattr(settings, k, v)
        R._llm()
    finally:
        R._llm, R.get_llm = old_llm, old_get_llm
        for k, v in old_settings.items():
            setattr(settings, k, v)
    check("它传下去的温度与哨兵不符 ⇒ ④ 当场红", seen != expect, f"seen={seen}")


def main() -> int:
    prod_calls = _calls_by_func(ROOT / "agent" / "graph.py")
    arm_calls = _calls_by_func(ROOT / "agent" / "react_arm.py")
    pm = _check_same_source(_prod_planner_kwargs(prod_calls), _check_shape(arm_calls))
    _check_runtime(pm)
    _check_narrator(prod_calls, arm_calls)
    _check_controls(pm)

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
