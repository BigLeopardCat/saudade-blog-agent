# -*- coding: utf-8 -*-
"""工具/技能面重排的**机器判据**（20261013 批次 C）。

计划的 C1 立了五条（一个工具一条主路 / 没有死注册项 / 同名参数不许两套取值 /
两条通道同源 / 描述里不许出现过程脚本）。**哪条手术落地，哪条才在这里有实现**——
判据是给已经做完的事上锁的，不是给"打算做的事"写的。今天在位的是：

  ④ **两条通道同源**——同一件工具的说明，planner 文本菜单（`graph._tools_desc`）
     与函数 schema（`native_plan.build_tool_schema` 里 `content_query` 的两格参数描述）
     必须来自**同一个字符串**，不许各写一份。

为什么先立这条：本仓的病史写着，每一次"同一件事两份渲染器"最后都对不上
（`skills.py:526` 那条头注记的就是菜单与白名单不同源 ⇒ 同一工具四轮执行四次）。
批次 B 把文本菜单从 ReAct 路径上摘掉之后，`multi_step_search_then_read_top`
**4/4 → 0/4**（两臂交替各 4 遍）——原因正是"这件工具是干什么的"只剩文本菜单一处
住处，而函数 schema 里那两格枚举**只有裸名字**。切片 1 把说明搬进 schema，
于是"同一份说明"这件事第一次有了两个**活着的**消费方，也就第一次值得上锁。

派生的三条子判据（每条都配反控）：
  a. 只有**一份**表：`graph._TOOL_MENU_LINES is skills.TOOL_MENU_LINES`（同一个对象，
     不是相等的两份拷贝——拷贝会各自漂）；
  b. 两个消费方**渲染同一句话**：对白名单里每件有说明的工具，菜单行里的说明
     == `tool_purpose(name)`，而 schema 的参数描述里也含这句话；
  c. **schema 里不出现裸名字**：`content_query.tools/calls` 枚举里的每个名字，
     都要在那一格的描述里有对应的一句说明（"枚举可点但说不出是干什么的"就是本批
     要根治的那个洞）。反控：把描述清空后，本条必须转红。

用法：.venv/bin/python tests/test_surface_composition.py
"""
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))

import agent.graph as G  # noqa: E402
import agent.skills as S  # noqa: E402
from agent.native_plan import build_tool_schema  # noqa: E402


FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}: {detail}")
        print(f"  ✗ {name} {detail}")
    else:
        print(f"  ✓ {name}")


def _content_query(role):
    fn = [t["function"] for t in build_tool_schema(role)
          if t["function"]["name"] == "content_query"]
    assert fn, f"角色 {role!r} 的菜单里没有 content_query"
    return fn[0]


ROLES = ("admin", "user", None)


def test_one_table():
    """a. 只有一份说明表（`is` 不是 `==`）。"""
    check("`graph._TOOL_MENU_LINES` 就是 `skills.TOOL_MENU_LINES` 本身（不是拷贝）",
          G._TOOL_MENU_LINES is S.TOOL_MENU_LINES)
    check("表非空且名字都是字符串",
          bool(S.TOOL_MENU_LINES) and all(isinstance(k, str) for k in S.TOOL_MENU_LINES))
    # 反控：改名/新建一份会立刻被上面那条 `is` 抓到——这里另证"表确实在被用"，
    # 否则把表清空也能过上面两条（空表同样满足 `is` 与类型）。
    probe = "search_notes"
    check("反控：表里的 search_notes 真的被渲染出来了",
          probe in S.TOOL_MENU_LINES and S.tool_purpose(probe) == S.TOOL_MENU_LINES[probe])


def test_menu_line_uses_purpose():
    """b. 文本菜单那一行里的说明 == 同一个 `tool_purpose`（两处同一句话）。"""
    for role in ROLES:
        menu = G._tools_desc(role)
        bad = []
        for name in S.callable_query_tools(role):
            want = S.tool_purpose(name)
            line = [ln for ln in menu.splitlines() if ln.startswith(f"- {name}(")]
            if len(line) != 1:
                bad.append(f"{name}: 菜单里 {len(line)} 行")
                continue
            got = line[0].split("：", 1)[1]
            if got != want:
                bad.append(f"{name}: 菜单 {got[:24]!r} != tool_purpose {want[:24]!r}")
        check(f"菜单每行的说明都等于 tool_purpose（role={role}）", not bad,
              str(bad[:3]))
        check(f"白名单里每件工具都在菜单里出现（role={role}）",
              all(any(ln.startswith(f"- {n}(") for ln in menu.splitlines())
                  for n in S.callable_query_tools(role)))


def _naked(desc: str, names) -> list:
    """描述里说不出用途的那些名字（判据本体，正控与反控共用同一份实现）。"""
    return [n for n in names if f"{n}：" not in desc]


def _enum_names(props: dict, key: str) -> list:
    """`tools` 的枚举在 `items.enum`；`calls` 的挂在内层 `items.properties.tool.enum`。"""
    items = props.get(key, {}).get("items") or {}
    names = list(items.get("enum") or ())
    if not names:
        names = list((items.get("properties") or {}).get("tool", {}).get("enum") or ())
    return names


def test_schema_carries_purpose():
    """c. `content_query` 的两格参数描述里，枚举的每个名字都有说明（不许裸名字）。"""
    for role in ROLES:
        fn = _content_query(role)
        props = fn["parameters"]["properties"]
        for key in ("tools", "calls"):
            desc = str(props.get(key, {}).get("description") or "")
            names = _enum_names(props, key)
            check(f"{key} 有枚举（role={role}）", bool(names))
            check(f"{key} 枚举里没有裸名字（role={role}）", not _naked(desc, names),
                  str(_naked(desc, names)[:4]))
    # 反控（**同一份实现**，只换输入）：描述清空 ⇒ 这条判据必须响。
    # 不配它的话，"枚举为空 / `desc` 为 None" 这类退化都能让上面那条**假绿**。
    fn = _content_query("admin")
    props = fn["parameters"]["properties"]
    for key in ("tools", "calls"):
        names = _enum_names(props, key)
        check(f"反控：{key} 的描述清空后，裸名字判据转红", bool(_naked("", names)))


def test_search_family_reachable():
    """检索族三个名字必须带着**各自不同**的说明（这是切片 1 要修的那件事）。"""
    fn = _content_query("admin")
    desc = str(fn["parameters"]["properties"]["calls"].get("description") or "")
    for name in ("search_notes", "rag_search", "get_article_detail", "list_notes"):
        check(f"`{name}` 在 calls 描述里有说明", f"{name}：" in desc)
    check("两个检索器的说明**不是同一句**（同名不同用途才是要防的那种撞名）",
          S.tool_purpose("search_notes") != S.tool_purpose("rag_search"))
    check("取不到的工具返回空串，不编说明（宁可少说）",
          S.tool_purpose("__no_such_tool__") == "")
    check("说明取不到时 tool_menu_lines 只留名字（不崩、不补空话）",
          S.tool_menu_lines(["__no_such_tool__"]) == "__no_such_tool__")


def main():
    for fn in (test_one_table, test_menu_line_uses_purpose,
               test_schema_carries_purpose, test_search_family_reachable):
        fn()
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
