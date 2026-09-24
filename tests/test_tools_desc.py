# -*- coding: utf-8 -*-
"""planner 菜单的参数签名（20260925）：必填/类型/默认值必须**从 args_schema 派生**。

"参数 schema 化"缺的两半里，这一半是**展示**：在此之前菜单只给参数名
（`update_tag(name, new_title, color, …)`），必填、类型、默认值一概不显示，planner 只能
靠常识猜；漏了必填参数会一路走到工具层抛异常、烧掉一个 `__ERROR__` 帧再重规划一轮
——而这些信息本来就在工具签名里，缺的只是"没给它看"。

本套件锁五条（全部离线、秒级，不碰网络与 LLM）：

  ① **必填标记双向一致**：全量 48 个工具逐个反射，渲染里带 `*` 的参数集合必须**恰好等于**
     `args_schema.required`——该标的没标（planner 敢省必填）和把可选的标成必填（planner
     白填一堆）都算红；
  ② **不是另一份表**：渲染出的参数名集合 == `tool.args` 的键集合，默认值标记集合 ==
     schema 里"非必填且有非 None 默认值"的集合；
  ③ **取不到 schema 时不猜必填**：桩工具（有 args、args_schema 抛异常 / 为 None）只渲染
     `名字:类型`，**一个标记都不许加**，且不许把菜单整体弄崩（菜单崩 = planner 没工具可点）；
  ④ **行形状不变**：无参工具渲染成 `()`，菜单一行一个工具——`_menu_names` 这类解析侧
     依赖 `- 名字(…)：说明` 的形状；
  ⑤ **图例在位**：`*` 与 `=值` 的含义必须写在菜单里给它看，且图例行不被当成工具行。

为什么单起一套而不并进 test_skills：那条已经 900+ 行，而"菜单签名的形状"是一条独立契约
（将来做执行前 schema 校验时，那条判据也要对着同一个 args_schema，见 graph._menu_arg_signature 注）。

用法：.venv/bin/python tests/test_tools_desc.py
"""
import re
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))

import agent.graph as _G  # noqa: E402
from agent.skills import callable_query_tools  # noqa: E402


FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}: {detail}")
        print(f"  ✗ {name} {detail}")
    else:
        print(f"  ✓ {name}")


_SEG = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(.+?)(\*)?(?:=(\S+))?$")


def _parse_sig(sig: str) -> dict:
    """`name:str*, key:int=8` → {name: (star, has_default)}。解析不了直接抛（渲染格式跑偏要响）。"""
    out = {}
    for seg in [s.strip() for s in sig.split(",") if s.strip()]:
        m = _SEG.match(seg)
        if not m:
            raise AssertionError(f"参数片段解析不了：{seg!r}（整串 {sig!r}）")
        out[m.group(1)] = (bool(m.group(3)), m.group(4) is not None)
    return out


class _BrokenSchema:
    """args_schema 存在但取 schema 就炸（模拟将来某个工具用奇怪的 schema）。"""

    @staticmethod
    def model_json_schema():
        raise RuntimeError("boom")


class _StubTool:
    name = "stub_tool"
    args = {"alpha": {"type": "string"}, "beta": {"type": "integer", "default": 3}}
    args_schema = _BrokenSchema


class _StubNoSchema:
    name = "stub_no_schema"
    args = {"gamma": {"anyOf": [{"type": "string"}, {"type": "null"}]}}
    args_schema = None


def test_required_marker_matches_schema():
    """① + ②：全量工具逐个对 args_schema 核（期望值在测试里**独立算**，不调被测私有函数）。"""
    print("[required_marker] 48 个工具逐个反射：`*`  ↔  args_schema.required")
    bad = []
    n_tools = n_with_args = n_required = 0
    for name, tool in sorted(_G._TOOL_MAP.items()):
        n_tools += 1
        props = getattr(tool, "args", None) or {}
        sig = _G._menu_arg_signature(tool)
        if not props:
            check(f"{name} 无参工具渲染成空串", sig == "", repr(sig))
            continue
        n_with_args += 1
        try:
            got = _parse_sig(sig)
        except AssertionError as e:
            bad.append(f"{name}: {e}")
            continue
        schema = getattr(tool, "args_schema", None)
        js = schema.model_json_schema() if schema is not None else {}
        required = set(js.get("required") or ())
        if required:
            n_required += 1
        star = {k for k, v in got.items() if v[0]}
        want_dflt = {k for k, sp in props.items()
                     if k not in required and isinstance(sp, dict) and sp.get("default") is not None}
        got_dflt = {k for k, v in got.items() if v[1]}
        why = []
        if set(got) != set(props):
            why.append(f"参数名不一致 got={sorted(got)} want={sorted(props)}")
        if star != required:
            why.append(f"必填标记不一致 多标={sorted(star - required)} 漏标={sorted(required - star)}")
        if got_dflt != want_dflt:
            why.append(f"默认值标记不一致 多标={sorted(got_dflt - want_dflt)} 漏标={sorted(want_dflt - got_dflt)}")
        # 类型标记不许落到 `?`（类型取自 args、与 required 同源，取不到才是 `?`）
        for k in got:
            if f"{k}:?" in sig:
                why.append(f"{k} 类型取不到")
        if why:
            bad.append(f"{name}: " + "；".join(why))
    check(f"全量 {n_tools} 个工具（有参 {n_with_args} / 带必填 {n_required}）签名与 schema 逐一一致",
          not bad, " | ".join(bad[:4]))

    # 明写一个真实的多参例子，防止上面的循环被整体写坏还"全绿"
    ut = _G._TOOL_MAP.get("update_tag")
    if ut is not None:
        sig = _G._menu_arg_signature(ut)
        check("update_tag：6 参里只有 name 带 `*`（真实多参样例）",
              "name:str*" in sig and "new_title:str*" not in sig, sig)
    uc = _G._TOOL_MAP.get("update_category")
    if uc is not None:
        check("update_category：同样只 name 必填", "name:str*" in _G._menu_arg_signature(uc),
              _G._menu_arg_signature(uc))


def test_broken_schema_never_guesses():
    """③：拿不到 schema 时只渲染名字:类型，**不许标必填、不许标默认值**、不许崩。"""
    print("[broken_schema] 取不到 args_schema ⇒ 不猜必填")
    for stub in (_StubTool, _StubNoSchema):
        try:
            sig = _G._menu_arg_signature(stub())
        except Exception as e:  # noqa: BLE001
            check(f"{stub.name} 渲染不抛异常", False, f"{type(e).__name__}: {e}")
            continue
        check(f"{stub.name} 渲染出参数名", "alpha:str" in sig or "gamma:str" in sig, sig)
        check(f"{stub.name} 一个标记都不加（不猜必填、不猜默认值）",
              "*" not in sig and "=" not in sig, sig)


def test_menu_shape():
    """④ + ⑤：行形状（`- 名字(…)：说明`）、图例、缓存一致、无参渲染 `()`。"""
    print("[menu_shape] 行形状 / 图例 / 缓存")
    for role in (None, "admin"):
        menu = _G._tools_desc(role)
        lines = menu.splitlines()
        check(f"菜单首行是图例且说明 `*` 与 `=值`（role={role}）",
              lines[0].startswith("（") and "*" in lines[0] and "=值" in lines[0], lines[0])
        tool_lines = [ln for ln in lines if ln.startswith("- ")]
        names = [ln[2:].split("(")[0].strip() for ln in tool_lines]
        check(f"菜单一行一个工具、无重复（role={role}）",
              len(names) == len(set(names)) and len(names) == len(callable_query_tools(role)),
              f"{len(names)} 行 / 白名单 {len(callable_query_tools(role))} 个")
        check(f"图例行没被当成工具行（role={role}）", not any(ln.startswith("- （") for ln in lines),
              lines[0])
        check(f"每行都是 `- 名字(…)：` 形状（role={role}）",
              all(re.match(r"^- [a-z_]+\(.*\)：.+", ln) for ln in tool_lines),
              str([ln for ln in tool_lines if not re.match(r"^- [a-z_]+\(.*\)：.+", ln)][:2]))
        check(f"缓存与现算一致（role={role}）", _G._tools_desc_cached(role) == menu)
    menu = _G._tools_desc(None)
    check("无参工具渲染成 `()`（解析侧依赖这个形状）",
          "- list_tags()：" in menu and "- get_blog_info()：" in menu,
          str([ln for ln in menu.splitlines() if "list_tags" in ln or "get_blog_info" in ln]))
    # 可选且无默认值的参数（今天唯一一个：管理员的 status=ai_passed|...）——裸 `:str` 也是合法渲染
    ma = _G._tools_desc("admin")
    check("可选且无默认值的参数渲染成裸 `名字:类型`（不臆造 default）",
          bool(re.search(r"- get_moderation_status\(status:str\)：", ma)),
          str([ln for ln in ma.splitlines() if ln.startswith("- get_moderation_status")]))


def main():
    for fn in (test_required_marker_matches_schema, test_broken_schema_never_guesses, test_menu_shape):
        fn()
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
